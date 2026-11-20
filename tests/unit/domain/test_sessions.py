"""
file: test_sessions.py
importable: yok

Borsa seanslarının testleri. Seansa bağlı bir strateji kuralı ya da Opus'a
giden saat bilgisi yanlış olursa işlem yanlış saatte açılır. Testler saat
dilimi geçişini, tatili, yarım günü ve önceki akşam açılan seansı korur.

description: domain.sessions için birim testleri.
"""

from datetime import datetime, timezone

from opus_trade.domain import sessions
from opus_trade.domain.sessions import DayKind

NYSE, CME = sessions.exchange("nyse"), sessions.exchange("cme")


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def test_nyse_open_follows_us_daylight_saving():
    """New York 09:30 açılışı yazın 13:30, kışın 14:30 UTC'dir. Sabit saat kullanan kod yılın yarısında yanılırdı."""
    assert sessions.is_open(NYSE, utc(2026, 7, 15, 13, 45))
    assert not sessions.is_open(NYSE, utc(2026, 12, 15, 13, 45))
    assert sessions.next_change(NYSE, utc(2026, 12, 15, 13, 45)) == utc(2026, 12, 15, 14, 30)


def test_nyse_holiday_is_closed_and_next_open_skips_it():
    """Şükran Günü 26.11.2026 kapalıdır, sıradaki açılış ertesi gündür."""
    at = utc(2026, 11, 26, 15)
    assert sessions.day_kind(NYSE, at.date()) is DayKind.HOLIDAY
    assert not sessions.is_open(NYSE, at)
    assert sessions.next_change(NYSE, at) == utc(2026, 11, 27, 14, 30)


def test_nyse_half_day_closes_early():
    """Şükran Günü'nün ertesi New York 13:00'te kapanır. Normal kapanışı bekleyen kural üç saat boyunca yanılırdı."""
    assert sessions.next_change(NYSE, utc(2026, 11, 27, 15)) == utc(2026, 11, 27, 18)
    assert not sessions.is_open(NYSE, utc(2026, 11, 27, 19))


def test_cme_week_opens_on_sunday_evening():
    """CME'nin pazartesi seansı pazar 17:00 Chicago'da açılır. Pazar hafta sonu sayılsa da piyasa açıktır."""
    assert not sessions.is_open(CME, utc(2026, 11, 22, 22, 30))
    assert sessions.is_open(CME, utc(2026, 11, 22, 23, 30))
