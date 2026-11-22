"""
file: hours.py
importable: hours_text, releases_text

hours_text() verilen anda açık olan büyük borsaları, sıradaki iki açılışı ve
varsa önümüzdeki günlerdeki tatil ve erken kapanışları Opus'a gidecek kısa
bir metin olarak döndürür. releases_text() ABD'nin ekonomik verilerinin ve
Fed kararlarının Türkiye saatiyle açıklanma saatlerini verir, talimata girer.
Bütün saatler Türkiye saatidir.

description: Opus'a giden piyasa saatleri bölümü.
"""

# NOTE: Takvimin kendisi domain.sessions'tadır, burada yalnızca metne çevrilir.
# ABD'nin büyük ekonomik verileri ve Fed kararları New York saatiyle sabit
# saatlerde açıklanır. Hiç değişmeyen bilgi olduğu için istemde değil
# talimattadır, talimat/istem.md. Türkiye saatine çevrilmiş hali yalnızca yaz
# saati geçişinde değişir.

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from ...domain import sessions
from ...domain.sessions import DayKind
from .texts import DAYS, MONTHS, TZ

_US_RELEASES = (("08:30", "ekonomik verilerin çoğu"), ("10:00", "bir kısmı"), ("14:00", "Fed faiz kararları"))
_NY = ZoneInfo("America/New_York")

# Kaç gün sonrasına kadarki tatil ve erken kapanışların bildirileceği.
_LOOK_AHEAD_DAYS = 7


def _hm(t: datetime) -> str:
    return t.astimezone(TZ).strftime("%H:%M")


def _day_name(d: date) -> str:
    return f"{d.day} {MONTHS[d.month - 1]} {DAYS[d.weekday()]}"


# Aynı gün içindeki an yalnızca saatle, ertesi gün yarınla, daha uzaktaki gün
# adıyla yazılır.
def _moment(t: datetime, now: datetime) -> str:
    day, today = t.astimezone(TZ).date(), now.astimezone(TZ).date()
    if day == today:
        return _hm(t)
    if day == today + timedelta(days=1):
        return f"yarın {_hm(t)}"
    return f"{_day_name(day)} {_hm(t)}"


def releases_text(now: datetime) -> str:
    """ABD verilerinin ve Fed kararlarının o günkü Türkiye saatleriyle açıklanma saatlerini tek cümlede verir."""
    ny_day = now.astimezone(_NY).date()
    return ", ".join(f"{what} {_hm(datetime.combine(ny_day, time(*map(int, at.split(':'))), _NY))}"
                     for at, what in _US_RELEASES)


# Açık borsalar kapanış saatleriyle, kapalılardan en yakın iki açılış. Tatil
# ve erken kapanışlar yalnızca varsa yazılır.
def hours_text(now: datetime) -> str:
    """Verilen anda piyasaların durumunu Markdown başlıklı kısa bir metin olarak döndürür."""
    open_, coming = [], []
    for ex in sessions.EXCHANGES:
        change = sessions.next_change(ex, now)
        if sessions.is_open(ex, now):
            open_.append(f"{ex.name}, kapanış {_moment(change, now)}" if change else ex.name)
        elif change:
            coming.append((change, f"{ex.name} {_moment(change, now)}"))
    coming.sort(key=lambda x: x[0])
    line = (f"Açık: {'; '.join(open_)}." if open_ else "Büyük borsaların hepsi kapalı.")
    if coming:
        line += f" Sıradaki açılışlar: {', '.join(t for _, t in coming[:2])}."
    notes = []
    for ex in sessions.EXCHANGES:
        first = now.astimezone(ZoneInfo(ex.tz)).date()
        for d, kind in sessions.special_days(ex, first, _LOOK_AHEAD_DAYS):
            if kind is DayKind.HOLIDAY:
                notes.append((d, f"{_day_name(d)}: {ex.name} tatil nedeniyle kapalı."))
            else:
                close = datetime.combine(d, time(*map(int, ex.half_close.split(":"))), ZoneInfo(ex.tz))
                notes.append((d, f"{_day_name(d)}: {ex.name} erken kapanacak, kapanış {_hm(close)}."))
    notes.sort(key=lambda x: x[0])
    if notes:
        line += " " + " ".join(t for _, t in notes)
    return "# Piyasa saatleri\n\n" + line
