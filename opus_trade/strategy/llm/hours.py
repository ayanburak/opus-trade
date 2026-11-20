"""
file: hours.py
importable: hours_text

hours_text() verilen anda büyük borsaların açık mı kapalı mı olduğunu, bir
sonraki açılış ya da kapanışı, o günün seanslarını ve önümüzdeki günlerdeki
tatil ve erken kapanışları Opus'a gidecek bir metin olarak döndürür. Bütün
saatler Türkiye saatidir.

description: Opus'a giden piyasa saatleri bölümü.
"""

# NOTE: Takvimin kendisi domain.sessions'tadır, burada yalnızca metne çevrilir.
# ABD'nin büyük ekonomik verileri ve Fed kararları New York saatiyle sabit
# saatlerde açıklanır. Her gün bir açıklama olmaz, saatler bilgi olarak verilir.

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


# Türkiye saatiyle bugünün içine düşen seans parçaları. Gece yarısını aşan
# parça 24:00'te ya da 00:00'da kesilir.
def _today(window: list, now: datetime) -> str:
    begin = datetime.combine(now.astimezone(TZ).date(), time(0), TZ)
    finish = begin + timedelta(days=1)
    parts = [f"{_hm(max(a, begin))}-{'24:00' if b >= finish else _hm(b)}" for a, b in window
             if b > begin and a < finish]
    return ", ".join(parts) or "bugün seans yok"


def hours_text(now: datetime) -> str:
    """Verilen anda büyük piyasaların durumunu Markdown başlıklı bir metin olarak döndürür."""
    rows = ["piyasa | şu an | bugünkü seans"]
    for ex in sessions.EXCHANGES:
        change = sessions.next_change(ex, now)
        state = "açık, kapanış" if sessions.is_open(ex, now) else "kapalı, açılış"
        status = f"{state} {_moment(change, now)}" if change else "kapalı"
        rows.append(f"{ex.name} | {status} | {_today(sessions.sessions_around(ex, now), now)}")

    ny_day = now.astimezone(_NY).date()
    releases = ", ".join(f"{what} {_hm(datetime.combine(ny_day, time(*map(int, at.split(':'))), _NY))}"
                         for at, what in _US_RELEASES)
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
    out = ["# Piyasa saatleri",
           "Saatler Türkiye saatidir. Kripto piyasası hiç kapanmaz, bu tablo diğer piyasalar içindir.",
           "\n".join(rows),
           f"ABD'nin ekonomik verileri ve Fed kararları açıklandığı günlerde şu saatlerde gelir: {releases}."]
    out.append(f"Önümüzdeki {_LOOK_AHEAD_DAYS} gündeki tatiller ve erken kapanışlar: " + " ".join(t for _, t in notes)
               if notes else f"Önümüzdeki {_LOOK_AHEAD_DAYS} günde tatil ya da erken kapanış yok.")
    return "\n\n".join(out)
