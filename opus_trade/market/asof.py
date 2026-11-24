"""
file: asof.py
importable: AsOfMarket

AsOfMarket bir MarketData'yı geçmiş bir ana kadarki veriyle sınırlar. Bütün
geçmiş istekleri o anda kesilir. O anda henüz kapanmamış son mum, o ana kadarki
dakikalık mumlardan yeniden kurulur, böylece mumun sonradan görülen tepesi ya
da dibi sızmaz. Günlük özet ve premium o anın mumlarından ve fonlamasından
hesaplanır. Emir defterinin geçmişi yoktur, MarketDataError verir.

description: Piyasa verisini geçmiş bir ana kadarki veriyle sınırlayan sarmalayıcı, sınama için.
"""

# NOTE: Sınama Opus'u geçmiş bir anda, o ana kadarki veriyle yeniden çağırır.
# Opus'un araçları da bunu kullanır, OPUS_AS_OF ortam
# değişkeni doluysa. Gelecekten tek bir mum sızarsa sınama anlamsızlaşır.
#
# XXX: Binance açık pozisyon, long short ve agresif alış satış geçmişini
# yalnızca son otuz gün için verir. Daha eski bir an sınanırsa bu araçlar
# hata döndürür, kart onları veri yok diye geçer.

from ..market.models import Candle, DailyStats, OpenInterest, Premium
from .source import MarketData, MarketDataError

_SECONDS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "2h": 7200, "4h": 14400,
            "6h": 21600, "8h": 28800, "12h": 43200, "1d": 86400, "3d": 259200, "1w": 604800}

# Binance'te fonlama bu aralıkla, gece yarısından başlayarak ödenir.
_FUNDING_S = 8 * 3600


def _cut(end: int | None, as_of: int) -> int:
    return as_of if end is None else min(end, as_of)


class AsOfMarket(MarketData):
    """Verilen ana kadarki veriyi gösteren piyasa.

    Args:
        inner: Asıl piyasa verisi.
        as_of: Görülebilecek son an, unix saniye.
    """

    def __init__(self, inner: MarketData, as_of: int) -> None:
        self.inner = inner
        self.as_of = as_of

    # Son mum as_of anında henüz kapanmadıysa o ana kadarki kapanmış
    # dakikalardan yeniden kurulur. Kapanmamış dakikanın kendisi atılır, onun
    # kapanışı o andan sonraki fiyattır. Aylık mumun süresi değişken olduğu için
    # desteklenmez.
    async def candles(self, symbol: str, interval: str, limit: int = 500, start: int | None = None,
                      end: int | None = None) -> list[Candle]:
        if interval not in _SECONDS:
            raise MarketDataError(f"Geçmiş anda {interval} aralığı desteklenmiyor.")
        found = await self.inner.candles(symbol, interval, limit, start, _cut(end, self.as_of))
        if not found or found[-1].time + _SECONDS[interval] <= self.as_of:
            return found
        if interval == "1m":
            return found[:-1]
        last = found[-1]
        minutes = [m for m in await self.inner.candles(symbol, "1m", start=last.time, end=self.as_of)
                   if m.time + 60 <= self.as_of]
        if not minutes:
            return found[:-1]
        rebuilt = Candle(symbol, last.time, minutes[0].open, max(m.high for m in minutes),
                         min(m.low for m in minutes), minutes[-1].close, sum(m.volume for m in minutes),
                         sum(m.quote_volume for m in minutes), sum(m.trades for m in minutes),
                         sum(m.taker_buy_volume for m in minutes))
        return found[:-1] + [rebuilt]

    async def mark_candles(self, symbol: str, start: int, end: int) -> list[Candle]:
        found = await self.inner.mark_candles(symbol, start, _cut(end, self.as_of))
        return [m for m in found if m.time + 60 <= self.as_of]

    async def daily_stats(self, symbol: str) -> DailyStats:
        hours = await self.candles(symbol, "1h", 24)
        if not hours:
            raise MarketDataError(f"{symbol} için o ana kadar mum yok.")
        return DailyStats(symbol, (hours[-1].close / hours[0].open - 1) * 100, max(h.high for h in hours),
                          min(h.low for h in hours), sum(h.quote_volume for h in hours))

    # Endeks fiyatının geçmişi yoktur, mark fiyatı yerine geçer. Tahmini
    # fonlama o ana kadar gerçekleşen son fonlamadır.
    async def premium(self, symbol: str | None = None) -> list[Premium]:
        if symbol is None:
            raise MarketDataError("Geçmiş anda bütün paritelerin premium verisi yok, parite verilmeli.")
        marks = await self.inner.mark_candles(symbol, self.as_of - 600, self.as_of)
        if not marks:
            raise MarketDataError(f"{symbol} için o anın mark fiyatı yok.")
        paid = await self.inner.funding_history(symbol, self.as_of - 3 * 86400, self.as_of)
        rate = paid[-1].rate if paid else 0.0
        nxt = (self.as_of // _FUNDING_S + 1) * _FUNDING_S
        return [Premium(self.as_of, symbol, marks[-1].close, marks[-1].close, rate, nxt)]

    async def funding_history(self, symbol, start=None, end=None, limit=1000):
        return await self.inner.funding_history(symbol, start, _cut(end, self.as_of), limit)

    async def open_interest(self, symbol: str) -> OpenInterest:
        found = await self.inner.open_interest_history(symbol, "5m", 1, end=self.as_of)
        if not found:
            raise MarketDataError(f"{symbol} için o anın açık pozisyonu yok.")
        return found[-1]

    async def open_interest_history(self, symbol, period, limit=500, start=None, end=None):
        return await self.inner.open_interest_history(symbol, period, limit, start, _cut(end, self.as_of))

    async def long_short(self, kind, symbol, period, limit=500, start=None, end=None):
        return await self.inner.long_short(kind, symbol, period, limit, start, _cut(end, self.as_of))

    async def taker_volume(self, symbol, period, limit=500, start=None, end=None):
        return await self.inner.taker_volume(symbol, period, limit, start, _cut(end, self.as_of))

    async def order_book(self, symbol: str, depth: int = 20):
        raise MarketDataError("Geçmiş bir anın emir defteri yoktur, bu veri sınamada kullanılamaz.")
