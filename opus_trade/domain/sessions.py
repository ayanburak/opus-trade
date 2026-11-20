"""
file: sessions.py
importable: DayKind, Exchange, EXCHANGES, exchange, day_kind, sessions_on, sessions_around, is_open, next_change,
            special_days

Büyük borsaların ve vadeli piyasaların işlem seanslarını hesaplar. Bütün
anlar saat dilimi taşıyan datetime'lardır, sonuç sunucunun saat ayarından
bağımsızdır. is_open() bir borsanın verilen anda açık olup olmadığını,
next_change() bir sonraki açılış ya da kapanışı söyler. Stratejiler seansa
bağlı bir kural için, LLM stratejisi de Opus'a giden piyasa saatleri metni
için bunları kullanır. Yeni bir borsa EXCHANGES listesine bir satırla eklenir.

description: Borsa seansları, tatiller ve erken kapanışlar.
"""

# NOTE: Saatler her borsanın kendi saat diliminde tanımlıdır. Yaz ve kış saati
# geçişleri böylece kendiliğinden doğru olur. Türkiye yaz saati uygulamadığı
# için New York açılışı Türkiye saatiyle yazın 16:30, kışın 17:30'dur.
#
# NOTE: Tatiller ve yarım günler holidays kütüphanesinin borsa takvimlerinden
# gelir. Kütüphane takvimleri borsaların duyurularına göre günceller, her yıl
# başlamadan sürümü yükseltilmelidir. Yarım günün kapanış saati kütüphanede
# yalnızca açıklama metninde yazdığı için her borsada half_close olarak ayrıca
# tutulur.
#
# XXX: CME'nin seansı bir önceki günün akşamı açılır. Başlangıcı bitişinden
# büyük yazılmış bir seans bir önceki takvim gününde başlar. Tatil ve yarım
# gün kararı seansın bittiği güne göre verilir, CME'nin takvimi de böyledir.

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from functools import lru_cache
from zoneinfo import ZoneInfo

import holidays  # Ülke ve borsa tatil takvimleri, saf Python

Session = tuple[datetime, datetime]


class DayKind(StrEnum):
    OPEN = "open"
    HALF = "half"
    HOLIDAY = "holiday"
    WEEKEND = "weekend"


@dataclass(frozen=True)
class Exchange:
    """Bir borsanın ya da vadeli piyasanın işlem saatleri.

    Args:
        code: Programın içinde kullanılan kısa ad, örneğin nyse.
        name: Türkçe görünen ad.
        calendar: holidays kütüphanesindeki borsa takviminin kodu.
        tz: Saatlerin yazıldığı saat dilimi.
        sessions: Yerel saatle başlangıç ve bitiş çiftleri. Öğle arası olan
            borsalarda iki parça olur.
        half_close: Yarım günlerde yerel kapanış saati. Takvimde yarım gün yoksa None.
    """

    code: str
    name: str
    calendar: str
    tz: str
    sessions: tuple[tuple[str, str], ...]
    half_close: str | None = None


# Sıra en çok etkisi olandan başlar, metinlerde de bu sırayla görünür.
EXCHANGES = (
    Exchange("nyse", "New York borsası, NYSE ve Nasdaq", "XNYS", "America/New_York", (("09:30", "16:00"),), "13:00"),
    Exchange("cme", "CME vadelileri, altın ve bitcoin vadelileri dahil", "XCME", "America/Chicago",
             (("17:00", "16:00"),), "12:00"),
    Exchange("lse", "Londra borsası", "XLON", "Europe/London", (("08:00", "16:30"),), "12:30"),
    Exchange("xetra", "Frankfurt borsası, Xetra", "XETR", "Europe/Berlin", (("09:00", "17:30"),)),
    Exchange("tse", "Tokyo borsası", "XJPX", "Asia/Tokyo", (("09:00", "11:30"), ("12:30", "15:30"))),
    Exchange("hkex", "Hong Kong borsası", "XHKG", "Asia/Hong_Kong", (("09:30", "12:00"), ("13:00", "16:00")),
             "12:00"),
    Exchange("sse", "Şanghay borsası", "XSHG", "Asia/Shanghai", (("09:30", "11:30"), ("13:00", "15:00"))),
)

_BY_CODE = {e.code: e for e in EXCHANGES}

# Bir anın açık bir seansa denk gelip gelmediğine bakarken o anın birkaç gün
# öncesi ve sonrası taranır. Hafta sonuna denk gelen uzun bir tatilde sıradaki
# açılış bir haftadan uzakta olabilir.
_SCAN_BACK, _SCAN_AHEAD = 2, 10


def exchange(code: str) -> Exchange:
    """Kısa adı verilen borsayı döndürür.

    Raises:
        KeyError: Bu adla bir borsa yoksa.
    """
    return _BY_CODE[code]


# --- Takvim ---

def _clock(text: str) -> time:
    h, m = text.split(":")
    return time(int(h), int(m))


# Kütüphane takvimi yıl yıl kurar ve aynı yıl defalarca sorulur, bu yüzden
# bir kez kurulup saklanır. Bir borsanın takviminde istenen kategori yoksa,
# örneğin yarım gün, boş küme döner.
@lru_cache(maxsize=None)
def _days(calendar: str, year: int, category: str) -> frozenset[date]:
    probe = holidays.financial_holidays(calendar, years=year)
    if category not in probe.supported_categories:
        return frozenset()
    return frozenset(holidays.financial_holidays(calendar, years=year, categories=(category,)))


def day_kind(ex: Exchange, d: date) -> DayKind:
    if d.weekday() >= 5:
        return DayKind.WEEKEND
    if d in _days(ex.calendar, d.year, "public"):
        return DayKind.HOLIDAY
    if ex.half_close and d in _days(ex.calendar, d.year, "half_day"):
        return DayKind.HALF
    return DayKind.OPEN


# Yarım günde kapanıştan sonra başlayan parçalar düşer, kapanışı kesen parça
# kısalır.
def sessions_on(ex: Exchange, d: date) -> list[Session]:
    """Borsanın yerel takvimdeki bir gününün seanslarını döndürür.

    Args:
        ex: Borsa.
        d: Borsanın kendi saat dilimindeki gün. Gece başlayan seansta bitişin günüdür.

    Returns:
        Başlangıç ve bitiş çiftleri, saat dilimi borsanınki. Kapalı günde boş liste.
    """
    kind = day_kind(ex, d)
    if kind in (DayKind.WEEKEND, DayKind.HOLIDAY):
        return []
    tz = ZoneInfo(ex.tz)
    out = []
    for a, b in ex.sessions:
        start = datetime.combine(d - timedelta(days=1) if a > b else d, _clock(a), tz)
        end = datetime.combine(d, _clock(b), tz)
        if kind is DayKind.HALF:
            cut = datetime.combine(d, _clock(ex.half_close), tz)
            if start >= cut:
                continue
            end = min(end, cut)
        out.append((start, end))
    return out


def sessions_around(ex: Exchange, at: datetime, back: int = _SCAN_BACK, ahead: int = _SCAN_AHEAD) -> list[Session]:
    """Verilen anın back gün öncesinden ahead gün sonrasına kadarki seansları sırayla döndürür."""
    today = at.astimezone(ZoneInfo(ex.tz)).date()
    found = []
    for k in range(-back, ahead + 1):
        found += sessions_on(ex, today + timedelta(days=k))
    return found


def is_open(ex: Exchange, at: datetime) -> bool:
    return any(a <= at < b for a, b in sessions_around(ex, at))


def next_change(ex: Exchange, at: datetime) -> datetime | None:
    """Borsa açıksa kapanış, kapalıysa sıradaki açılış anını döndürür.

    Returns:
        Saat dilimi borsanınki olan an. Taranan aralıkta açılış yoksa None.
    """
    window = sessions_around(ex, at)
    current = next((s for s in window if s[0] <= at < s[1]), None)
    if current:
        return current[1]
    return next((a for a, _ in window if a > at), None)


def special_days(ex: Exchange, start: date, days: int) -> list[tuple[date, DayKind]]:
    """Başlangıç gününden itibaren verilen gün sayısı içindeki tatil ve yarım günleri döndürür."""
    found = []
    for k in range(days):
        d = start + timedelta(days=k)
        kind = day_kind(ex, d)
        if kind in (DayKind.HOLIDAY, DayKind.HALF):
            found.append((d, kind))
    return found
