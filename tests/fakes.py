"""
file: fakes.py
importable: FakeClock, FakeMarket, catalog

Testlerin ortak sahte parçaları. FakeClock elle ilerletilen bir saattir.
FakeMarket MarketData'nın ağa çıkmayan bir uygulamasıdır, beş dakikalık
istatistikleri istenen aralık için üretir ve her isteği calls listesine
yazar. Bir sembolü fail kümesine eklemek o sembolün isteklerini
MarketDataError ile düşürür. oldest verilirse ondan eski bir başlangıç da
Binance'teki gibi MarketDataError alır. minutes paritelerin dakikalık
mumlarıdır, candles() aralıktakileri döndürür. catalog() masanın paritelerini
tests/fixtures'daki exchangeInfo kopyasından ve paketin kademe tablosundan kurar,
testler Binance'e çıkmaz.

description: Testlerde kullanılan sahte saat ve sahte piyasa verisi.
"""

import json
from datetime import datetime, timezone
from functools import cache
from pathlib import Path

from opus_trade.domain.catalog import Catalog
from opus_trade.domain.clock import Clock
from opus_trade.market.binance.specs import load_brackets, specs_from
from opus_trade.market.models import Candle, LongShortRatio, OpenInterest, Premium, TakerVolume
from opus_trade.market.source import MarketData, MarketDataError


# exchangeInfo kopyası 21.11.2026'da alındı. Testlerin rakamları bu kopyanın
# adımlarıyla elle hesaplandı, kopya yenilenirse rakamlar da yeniden hesaplanır.
@cache
def catalog() -> Catalog:
    info = json.loads((Path(__file__).parent / "fixtures" / "exchange_info.json").read_text())
    return Catalog(specs_from(info, load_brackets()))


class FakeClock(Clock):
    def __init__(self, t: int) -> None:
        self.t = t

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.t, timezone.utc)


# Binance gibi aralığın içindeki beş dakikalık anları döndürür, sonu dahil değil.
def _points(start: int, end: int) -> list[int]:
    first = -(-start // 300) * 300
    return list(range(first, end, 300))


class FakeMarket(MarketData):
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.fail: set[str] = set()
        self.premiums: list[Premium] = []
        self.oldest: int | None = None
        self.minutes: dict[str, list[Candle]] = {}

    def _record(self, name: str, symbol: str, start: int | None, end: int | None) -> None:
        self.calls.append((name, symbol, start, end))
        if symbol in self.fail:
            raise MarketDataError(f"{symbol} için sahte hata")
        if self.oldest is not None and start is not None and start < self.oldest:
            raise MarketDataError("parameter 'startTime' is invalid.")

    async def open_interest_history(self, symbol, period, limit=500, start=None, end=None):
        self._record("open_interest", symbol, start, end)
        return [OpenInterest(t, symbol, 1.0, 2.0) for t in _points(start, end)]

    async def long_short(self, kind, symbol, period, limit=500, start=None, end=None):
        self._record(f"long_short/{kind}", symbol, start, end)
        return [LongShortRatio(t, symbol, 60.0, 1.5) for t in _points(start, end)]

    async def taker_volume(self, symbol, period, limit=500, start=None, end=None):
        self._record("taker_volume", symbol, start, end)
        return [TakerVolume(t, symbol, 1.0, 5.0, 5.0) for t in _points(start, end)]

    # Tek parite istenince fail kümesi Binance'in tanımadığı pariteler gibi davranır.
    async def premium(self, symbol=None):
        if symbol is not None:
            self.calls.append(("premium", symbol, None, None))
            if symbol in self.fail:
                raise MarketDataError(f"Binance /fapi/v1/premiumIndex 400 döndürdü: Invalid symbol {symbol}")
        return list(self.premiums)

    # Yalnızca kapalı süre işlenirken istenen dakikalık mumlar verilir.
    async def candles(self, symbol, interval, limit=500, start=None, end=None):
        return [c for c in self.minutes.get(symbol, []) if start <= c.time < end]

    async def funding_history(self, symbol, start=None, end=None, limit=1000):
        return []

    async def daily_stats(self, *a, **k):
        raise NotImplementedError

    async def open_interest(self, *a, **k):
        raise NotImplementedError

    async def order_book(self, *a, **k):
        raise NotImplementedError
