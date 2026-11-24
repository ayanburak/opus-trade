"""
file: test_asof.py
importable: yok

Geçmiş anın piyasasının testleri. Sınamada gelecekten tek bir mum ya da
fonlama sızarsa Opus o anda bilinmeyen bir şeyi bilir ve sınama anlamsızlaşır.

description: market.asof için birim testleri.
"""

import asyncio

import pytest

from opus_trade.market.asof import AsOfMarket
from opus_trade.market.models import Candle, Funding
from opus_trade.market.source import MarketData, MarketDataError

H = 3600
AS_OF = 100 * H + 30 * 60 + 30


def _c(t, o, h, l, c, step):
    return Candle("BTCUSDT", t, o, h, l, c, 1, 10, 1, 0.5)


class Inner(MarketData):
    """Her aralıkta düzgün mumlar ve dakikalar veren sahte piyasa. Asıl piyasa gibi end'den önce açılanları verir."""

    def __init__(self):
        self.seen = []

    async def candles(self, symbol, interval, limit=500, start=None, end=None):
        self.seen.append((interval, start, end))
        step = {"1m": 60, "1h": H}[interval]
        first = start if start is not None else (end // step - limit + 1) * step
        out = []
        for t in range(first, end, step):
            spike = 999 if t + step > AS_OF else 0
            out.append(_c(t, 100, 101 + spike, 99, 100, step))
        return out[-limit:] if start is None else out

    async def mark_candles(self, symbol, start, end):
        return [_c(t, 100, 100, 100, 100.5, 60) for t in range(start, end, 60)]

    async def funding_history(self, symbol, start=None, end=None, limit=1000):
        return [Funding(t, symbol, 0.0001 * (t // H), 100) for t in (90 * H, 98 * H, 106 * H) if t < (end or 10**12)]

    async def daily_stats(self, symbol):
        raise NotImplementedError

    async def all_daily_stats(self):
        raise NotImplementedError

    async def premium(self, symbol=None):
        raise NotImplementedError

    async def open_interest(self, symbol):
        raise NotImplementedError

    async def open_interest_history(self, symbol, period, limit=500, start=None, end=None):
        return [end]

    async def long_short(self, kind, symbol, period, limit=500, start=None, end=None):
        return [end]

    async def taker_volume(self, symbol, period, limit=500, start=None, end=None):
        return [end]

    async def order_book(self, symbol, depth=20):
        raise NotImplementedError


def run(coro):
    return asyncio.run(coro)


def test_unclosed_last_candle_is_rebuilt_from_minutes_up_to_the_moment():
    """O anda kapanmamış saatlik mum yalnızca o ana kadarki dakikalardan kurulur, sonradan gelen tepe sızmaz."""
    m = AsOfMarket(Inner(), AS_OF)
    found = run(m.candles("BTCUSDT", "1h", 3))
    assert [c.time for c in found] == [98 * H, 99 * H, 100 * H]
    assert found[-1].high == 101 and found[-1].quote_volume == 30 * 10
    minutes = run(m.candles("BTCUSDT", "1m", 3))
    assert minutes[-1].time + 60 <= AS_OF, "Kapanmamış dakikanın kapanışı o andan sonraki fiyattır."


def test_every_history_request_is_cut_at_the_moment():
    """Fonlama, açık pozisyon, long short ve agresif alış satış o anda kesilir."""
    inner = Inner()
    m = AsOfMarket(inner, AS_OF)
    assert [f.time for f in run(m.funding_history("BTCUSDT"))] == [90 * H, 98 * H]
    assert run(m.funding_history("BTCUSDT", end=200 * H))[-1].time == 98 * H
    assert run(m.open_interest_history("BTCUSDT", "1h")) == [AS_OF]
    assert run(m.long_short(None, "BTCUSDT", "1h", end=AS_OF - 60)) == [AS_OF - 60]
    assert run(m.taker_volume("BTCUSDT", "1h")) == [AS_OF]


def test_premium_and_daily_stats_come_from_that_moment():
    """Mark fiyatı o anın son dakikasından, tahmini fonlama o ana kadarki son fonlamadan gelir."""
    m = AsOfMarket(Inner(), AS_OF)
    [p] = run(m.premium("BTCUSDT"))
    assert (p.mark, p.est_funding, p.next_funding) == (100.5, 0.0001 * 98, 104 * H)
    day = run(m.daily_stats("BTCUSDT"))
    assert day.high == 101 and day.quote_volume == 23 * 10 + 30 * 10
    assert run(m.mark_candles("BTCUSDT", AS_OF - 300, AS_OF))[-1].time + 60 <= AS_OF


def test_order_book_has_no_history():
    """Emir defterinin geçmişi yoktur, uydurulmaz."""
    with pytest.raises(MarketDataError, match="emir defteri yoktur"):
        run(AsOfMarket(Inner(), AS_OF).order_book("BTCUSDT"))
