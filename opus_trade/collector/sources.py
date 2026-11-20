"""
file: sources.py
importable: Source, LiquidationSource, PremiumSource, StatsSource, HISTORY_S, STATS_PERIOD

Toplayıcının veri kaynakları. Her kaynak bir Source'tur ve run() ile iptal
edilene kadar çalışır. LiquidationSource canlı akıştan bütün piyasanın
likidasyonlarını dinler ve toplu yazar. PremiumSource dakikada bir mark
fiyatını ve tahmini fonlamayı okur. StatsSource açık pozisyonu, üç long short
oranını ve taker hacmini beş dakikalık aralıkla toplar, açılışta son 29 günü
geriye dönük doldurur.

description: Likidasyon, premium ve istatistik kaynakları.
"""

# NOTE: Bir kaynağın hatası diğerlerini durdurmaz. Binance'e ulaşılamayan bir
# turda kaynak uyarı yazar ve bir sonraki turda yeniden dener. İstatistik
# kaynağı bir sonraki turda en son kayıttan devam ettiği için kaçan turlar
# kendiliğinden kapanır.
#
# NOTE: İptal edilen bir kaynak run() içindeki finally'de tamponunu diske
# yazar. Servis durdurulurken bekleyen likidasyonlar böylece kaybolmaz.

import asyncio
import logging
from abc import ABC, abstractmethod

from ..domain.clock import Clock
from ..market.models import Candle, Liquidation
from ..market.source import MarketData, MarketDataError, RatioKind
from ..market.stream import MarketStream, StreamListener
from .store import CollectorStore

log = logging.getLogger(__name__)

STATS_PERIOD = "5m"
_PERIOD_S = 300

# Binance istatistikleri otuz gün geriye verir, daha eski bir başlangıç 400
# hatası alır. Bir günlük pay saat farkı ve istek süresi içindir.
HISTORY_S = 29 * 86400


class Source(ABC):
    name: str

    @abstractmethod
    async def run(self) -> None:
        """Kaynağı iptal edilene kadar çalıştırır."""


# Akış sessiz kalırsa yeniden bağlanır. Likidasyonlar sakin bir anda yarım
# dakika gelmeyebilir, bu yüzden tek bir paritenin dakikalık mumu da dinlenir
# ve akışın canlı olduğunu kanıtlar. Mumlar yazılmaz. Kapsama kaydı son
# mesajın zamanıyla tutulur, kopuk geçen süre kayıtta boşluk olarak görünür.
class LiquidationSource(Source, StreamListener):
    """Bütün piyasanın likidasyonlarını toplar.

    Args:
        stream: Canlı akış.
        store: Toplayıcının deposu.
        clock: Saat.
        flush_s: Tamponun diske yazılma aralığı, saniye.
    """

    name = "liquidations"
    _HEARTBEAT = "BTCUSDT"
    _MAX_GAP = 60

    def __init__(self, stream: MarketStream, store: CollectorStore, clock: Clock, flush_s: float = 2) -> None:
        self._stream = stream
        self._store = store
        self._clock = clock
        self._flush_s = flush_s
        self._buffer: list[Liquidation] = []
        self._seen: int | None = None

    async def on_candle(self, candle: Candle, closed: bool) -> None:
        self._seen = self._clock.time()

    async def on_liquidation(self, liq: Liquidation) -> None:
        self._seen = self._clock.time()
        self._buffer.append(liq)

    def _flush(self) -> None:
        items, self._buffer = self._buffer, []
        if items:
            self._store.add_liquidations(items)
        if self._seen is not None:
            self._store.touch(self.name, self._seen, self._MAX_GAP)

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self._flush_s)
            self._flush()

    async def run(self) -> None:
        await self._stream.watch({self._HEARTBEAT})
        flusher = asyncio.create_task(self._flush_loop())
        try:
            await self._stream.run(self)
        finally:
            flusher.cancel()
            self._flush()


class PremiumSource(Source):
    """İzlenen paritelerin mark fiyatını, endeksini ve tahmini fonlamasını dakikada bir toplar."""

    name = "premium"
    _INTERVAL = 60

    def __init__(self, market: MarketData, store: CollectorStore, clock: Clock) -> None:
        self._market = market
        self._store = store
        self._clock = clock

    # Bütün piyasa tek istekte gelir, yalnızca izlenen pariteler yazılır.
    async def collect(self) -> None:
        tracked = set(self._store.symbols())
        rows = [p for p in await self._market.premium() if p.symbol in tracked]
        self._store.add_premiums(rows)
        self._store.touch(self.name, self._clock.time(), self._INTERVAL * 2)

    async def run(self) -> None:
        while True:
            try:
                await self.collect()
            except MarketDataError as e:
                log.warning("Premium alınamadı: %s", e)
            await asyncio.sleep(self._INTERVAL - self._clock.time() % self._INTERVAL)


# Her tür kendi tablosuna yazılır. long_short tablosunda tür ayrıca kind
# sütununda durur, last_time() bu yüzden türü de alır.
_KINDS = (
    ("open_interest", None),
    ("long_short", RatioKind.ACCOUNTS),
    ("long_short", RatioKind.TOP_ACCOUNTS),
    ("long_short", RatioKind.TOP_POSITIONS),
    ("taker_volume", None),
)


# Her tur her parite ve tür için en son kayıttan şu ana kadarki aralığı
# ister. Kayıt yoksa ya da en son kayıt Binance'in verdiği otuz günden
# eskiyse başlangıç HISTORY_S öncesine çekilir. Binance bir aralığın
# değerini aralık bittikten biraz sonra yayınladığı için tur aralık
# başından yarım dakika sonra başlar.
class StatsSource(Source):
    """İzlenen paritelerin beş dakikalık istatistiklerini toplar ve geriye dönük doldurur."""

    name = "stats"
    _DELAY = 30

    def __init__(self, market: MarketData, store: CollectorStore, clock: Clock) -> None:
        self._market = market
        self._store = store
        self._clock = clock

    async def _one(self, table: str, kind: RatioKind | None, symbol: str, now: int) -> None:
        last = self._store.last_time(table, symbol, kind)
        start = max(last + _PERIOD_S if last is not None else 0, now - HISTORY_S)
        if start >= now:
            return
        m, s = self._market, self._store
        if table == "open_interest":
            s.add_open_interest(await m.open_interest_history(symbol, STATS_PERIOD, start=start, end=now))
        elif table == "long_short":
            s.add_long_short(kind, await m.long_short(kind, symbol, STATS_PERIOD, start=start, end=now))
        else:
            s.add_taker_volume(await m.taker_volume(symbol, STATS_PERIOD, start=start, end=now))

    async def collect(self) -> None:
        now = self._clock.time()
        for symbol in self._store.symbols():
            for table, kind in _KINDS:
                try:
                    await self._one(table, kind, symbol, now)
                except MarketDataError as e:
                    log.warning("%s %s %s alınamadı: %s", symbol, table, kind or "", e)

    async def run(self) -> None:
        while True:
            await self.collect()
            now = self._clock.time()
            await asyncio.sleep(_PERIOD_S - now % _PERIOD_S + self._DELAY)
