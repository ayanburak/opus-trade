"""
file: test_screen.py
importable: yok

İzleme listesi kaynağının testleri. Sıralama yanlışsa strateji yanlış
paritelerin akışını dinler, aradığı iğneleri hiç görmez.

description: market.screen için birim testleri.
"""

import asyncio

from opus_trade.market.models import DailyStats
from opus_trade.market.screen import MarketScreen
from tests.fakes import FakeClock, FakeMarket


class Market(FakeMarket):
    async def all_daily_stats(self):
        return [DailyStats("AUSDT", 2, 110, 100, 1e6),       # aralık %10, değişim %2
                DailyStats("BUSDT", -30, 13, 10, 1e6),       # aralık %30, değişim %30
                DailyStats("CUSDT", 25, 105, 100, 1e6),      # aralık %5, değişim %25
                DailyStats("DUSDT", 1, 150, 100, 1e6),       # aralık %50, değişim %1
                DailyStats("EUSDT", 90, 300, 100, 1e6)]      # evrende yok


def screen() -> MarketScreen:
    s = MarketScreen(Market(), FakeClock(1_795_000_000))
    asyncio.run(s.refresh())
    return s


UNIVERSE = {"AUSDT", "BUSDT", "CUSDT", "DUSDT"}


def test_rankings_order_by_range_and_by_absolute_change():
    """Çöken bir parite de iğne adayıdır, değişim mutlak değerle sıralanmalı. Evrende olmayan parite hiç girmemeli,
    katalogda olmayan paritede işlem açılamaz."""
    s = screen()
    assert s.top(2, "range", UNIVERSE) == ["DUSDT", "BUSDT"]
    assert s.top(2, "change", UNIVERSE) == ["BUSDT", "CUSDT"]


def test_both_is_the_union_without_repeats():
    """Strateji metni iki sıralamayı birlikte anıyor. İki listede de olan parite bir kez dinlenmeli."""
    s = screen()
    assert s.top(2, "both", UNIVERSE) == ["DUSDT", "BUSDT", "CUSDT"]


def test_every_refresh_bumps_the_version():
    """Tetikleyici listenin değiştiğini sürümden anlar. Sürüm artmazsa saat başı yeni liste hiç dinlenmez."""
    s = screen()
    asyncio.run(s.refresh())
    assert s.version == 2 and s.updated == 1_795_000_000
    assert s.describe("BUSDT") == {"symbol": "BUSDT", "range_pct": 30.0, "change_pct": -30, "quote_volume": 1e6}
