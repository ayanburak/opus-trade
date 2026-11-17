"""
file: hours.py
importable: MARKETS, Market, status_text

Büyük borsaların ve vadeli piyasaların işlem saatlerini Türkiye saatine
çevirir. status_text() verilen anda her piyasanın açık mı kapalı mı
olduğunu, bir sonraki açılış ya da kapanışı, o günün seanslarını ve
önümüzdeki günlerdeki tatilleri ve erken kapanışları Opus'a gidecek bir
metin olarak döndürür. Yeni bir piyasa MARKETS listesine bir satır
eklenerek tanıtılır.

description: Borsa seanslarını, tatilleri ve erken kapanışları Türkiye saatiyle anlatan modül.
"""

# NOTE: Saatler her piyasanın kendi saat diliminde tanımlıdır ve önce UTC'ye,
# sonra settings.TIMEZONE'a çevrilir. Böylece yaz ve kış saati geçişleri
# kendiliğinden doğru olur ve sunucunun kendi saat ayarı sonucu etkilemez.
# Türkiye yaz saati uygulamadığı için ABD açılışı yazın 16:30, kışın 17:30'dur.
#
# NOTE: Tatiller ve yarım günler holidays kütüphanesinin borsa takvimlerinden
# gelir. Kütüphane bu takvimleri borsaların resmi duyurularına göre günceller,
# yıllar geçtikçe kütüphane sürümü yükseltilmelidir. Yarım günün kapanış saati
# kütüphanede yalnızca açıklama metninde yazdığı için her piyasada half_close
# olarak ayrıca tutulur.
#
# XXX: CME'nin seansı bir önceki günün akşamı açılır. Bir seansın başlangıcı
# bitişinden büyük yazılmışsa başlangıç bir önceki takvim gününe düşer. Tatil
# ve yarım gün kararı seansın bittiği güne göre verilir, CME'nin takvimi de
# bu şekildedir.

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

import holidays  # Ülke ve borsa tatil takvimleri, saf Python

from .. import settings

# --- Piyasalar ---


@dataclass(frozen=True)
class Market:
    """Bir borsanın ya da vadeli piyasanın işlem saatleri.

    Args:
        name: Opus'a görünen ad.
        calendar: holidays kütüphanesindeki borsa takviminin kodu.
        tz: Saatlerin yazıldığı saat dilimi.
        sessions: Yerel saatle başlangıç ve bitiş çiftleri. Öğle arası olan
            borsalarda iki parça olur.
        half_close: Yarım günlerde yerel kapanış saati. Takvimde yarım gün
            yoksa None.
    """

    name: str
    calendar: str
    tz: str
    sessions: tuple[tuple[str, str], ...]
    half_close: str | None = None


# Sıra Opus'a giden tablodaki sıradır, en çok etkisi olandan başlar.
MARKETS = (
    Market("New York borsası, NYSE ve Nasdaq", "XNYS", "America/New_York", (("09:30", "16:00"),), "13:00"),
    Market("CME vadelileri, altın ve bitcoin vadelileri dahil", "XCME", "America/Chicago",
           (("17:00", "16:00"),), "12:00"),
    Market("Londra borsası", "XLON", "Europe/London", (("08:00", "16:30"),), "12:30"),
    Market("Frankfurt borsası, Xetra", "XETR", "Europe/Berlin", (("09:00", "17:30"),)),
    Market("Tokyo borsası", "XJPX", "Asia/Tokyo", (("09:00", "11:30"), ("12:30", "15:30"))),
    Market("Hong Kong borsası", "XHKG", "Asia/Hong_Kong", (("09:30", "12:00"), ("13:00", "16:00")), "12:00"),
    Market("Şanghay borsası", "XSHG", "Asia/Shanghai", (("09:30", "11:30"), ("13:00", "15:00"))),
)

# ABD'nin büyük ekonomik verileri ve Fed kararları New York saatiyle bu
# saatlerde açıklanır. Her gün bir açıklama olmaz, saatler yalnızca bilgi
# olarak çevrilir.
_US_RELEASES = (("08:30", "ekonomik verilerin çoğu"), ("10:00", "bir kısmı"), ("14:00", "Fed faiz kararları"))

# Kaç gün sonrasına kadarki tatil ve erken kapanışların bildirileceği.
_LOOK_AHEAD_DAYS = 7

_HOME = ZoneInfo(settings.TIMEZONE)


# --- Takvim ---

def _clock(text: str) -> time:
    h, m = text.split(":")
    return time(int(h), int(m))


# Kütüphane takvimi yıl yıl kurar. Aynı yıl bir çağrıda defalarca sorulduğu
# için takvim bir kez kurulup saklanır. Bir piyasanın takviminde yarım gün
# kategorisi yoksa boş bir küme döner.
@lru_cache(maxsize=None)
def _days(code: str, year: int, category: str) -> frozenset[date]:
    probe = holidays.financial_holidays(code, years=year)
    if category not in probe.supported_categories:
        return frozenset()
    return frozenset(holidays.financial_holidays(code, years=year, categories=(category,)))


def _day_kind(m: Market, d: date) -> str:
    if d.weekday() >= 5:
        return "weekend"
    if d in _days(m.calendar, d.year, "public"):
        return "holiday"
    if m.half_close and d in _days(m.calendar, d.year, "half_day"):
        return "half"
    return "open"


# Bir işlem gününün seanslarını UTC'ye bağlı datetime çiftleri olarak
# döndürür. Yarım günde kapanıştan sonra başlayan parçalar düşer, kapanışı
# kesen parça kısalır.
def _sessions(m: Market, d: date) -> list[tuple[datetime, datetime]]:
    kind = _day_kind(m, d)
    if kind in ("weekend", "holiday"):
        return []
    tz = ZoneInfo(m.tz)
    out = []
    for a, b in m.sessions:
        start = datetime.combine(d - timedelta(days=1) if a > b else d, _clock(a), tz)
        end = datetime.combine(d, _clock(b), tz)
        if kind == "half":
            cut = datetime.combine(d, _clock(m.half_close), tz)
            if start >= cut:
                continue
            end = min(end, cut)
        out.append((start, end))
    return out


def _window(m: Market, now: datetime) -> list[tuple[datetime, datetime]]:
    today = now.astimezone(ZoneInfo(m.tz)).date()
    found = []
    for k in range(-2, _LOOK_AHEAD_DAYS + 3):
        found += _sessions(m, today + timedelta(days=k))
    return found


# --- Metin ---

def _hm(t: datetime) -> str:
    return t.astimezone(_HOME).strftime("%H:%M")


def _day_name(d: date) -> str:
    return f"{d.day} {settings.MONTHS[d.month - 1]} {settings.DAYS[d.weekday()]}"


# Aynı gün içindeki bir an yalnızca saatle, başka bir gündeki an gün adıyla yazılır.
def _moment(t: datetime, now: datetime) -> str:
    local, today = t.astimezone(_HOME), now.astimezone(_HOME).date()
    if local.date() == today:
        return _hm(t)
    if local.date() == today + timedelta(days=1):
        return f"yarın {_hm(t)}"
    return f"{_day_name(local.date())} {_hm(t)}"


def _status(window: list[tuple[datetime, datetime]], now: datetime) -> str:
    current = next((s for s in window if s[0] <= now < s[1]), None)
    if current:
        return f"açık, kapanış {_moment(current[1], now)}"
    upcoming = next((s for s in window if s[0] > now), None)
    return f"kapalı, açılış {_moment(upcoming[0], now)}" if upcoming else "kapalı"


# Türkiye saatiyle bugünün içine düşen seans parçaları. Gece yarısını aşan
# bir parça 24:00'te ya da 00:00'da kesilir.
def _today(window: list[tuple[datetime, datetime]], now: datetime) -> str:
    day = now.astimezone(_HOME).date()
    begin = datetime.combine(day, time(0), _HOME)
    finish = begin + timedelta(days=1)
    parts = []
    for a, b in window:
        if b <= begin or a >= finish:
            continue
        end = "24:00" if b >= finish else _hm(b)
        parts.append(f"{_hm(max(a, begin))}-{end}")
    return ", ".join(parts) or "bugün seans yok"


def _exceptions(now: datetime) -> list[str]:
    notes: list[tuple[date, str]] = []
    for m in MARKETS:
        first = now.astimezone(ZoneInfo(m.tz)).date()
        for k in range(_LOOK_AHEAD_DAYS):
            d = first + timedelta(days=k)
            kind = _day_kind(m, d)
            if kind == "holiday":
                notes.append((d, f"{_day_name(d)}: {m.name} tatil nedeniyle kapalı."))
            elif kind == "half":
                close = datetime.combine(d, _clock(m.half_close), ZoneInfo(m.tz))
                notes.append((d, f"{_day_name(d)}: {m.name} erken kapanacak, kapanış {_hm(close)}."))
    return [text for _, text in sorted(notes, key=lambda x: x[0])]


def status_text(now: datetime) -> str:
    """Verilen anda büyük piyasaların durumunu Opus'a gidecek metin olarak döndürür.

    Args:
        now: Saat dilimi bilgisi taşıyan an.

    Returns:
        Markdown başlıklı, tablolu bir metin. Bütün saatler settings.TIMEZONE'dadır.
    """
    rows = ["piyasa | şu an | bugünkü seans"]
    for m in MARKETS:
        window = _window(m, now)
        rows.append(f"{m.name} | {_status(window, now)} | {_today(window, now)}")

    ny_day = now.astimezone(ZoneInfo("America/New_York")).date()
    releases = ", ".join(
        f"{what} {_hm(datetime.combine(ny_day, _clock(at), ZoneInfo('America/New_York')))}"
        for at, what in _US_RELEASES
    )
    out = ["# Piyasa saatleri", "Saatler Türkiye saatidir. Kripto piyasası hiç kapanmaz, bu tablo diğer piyasalar "
           "içindir.", "\n".join(rows),
           f"ABD'nin ekonomik verileri ve Fed kararları açıklandığı günlerde şu saatlerde gelir: {releases}."]
    exceptions = _exceptions(now)
    out.append(f"Önümüzdeki {_LOOK_AHEAD_DAYS} gündeki tatiller ve erken kapanışlar: " + " ".join(exceptions)
               if exceptions else f"Önümüzdeki {_LOOK_AHEAD_DAYS} günde tatil ya da erken kapanış yok.")
    return "\n\n".join(out)
