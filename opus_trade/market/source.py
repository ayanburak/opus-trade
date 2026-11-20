"""
file: source.py
importable: MarketData, MarketDataError, RatioKind

MarketData piyasa verisinin soyut sınıfıdır. Strateji, kâğıt broker, LLM
araçları ve toplayıcı piyasaya yalnızca bunun üzerinden erişir, hangi borsadan
ve nasıl geldiğini bilmez. Veri alınamazsa her uygulama MarketDataError
fırlatır, çağıran geçici hatayı programın kendi hatasından böylece ayırır.

description: Geçmiş ve anlık piyasa verisinin soyut sınıfı.
"""

# NOTE: Bugün tek uygulaması binance.rest.BinanceRest'tir. Soyut sınıf yine
# de baştan var, çünkü testler ağa çıkmadan sahte veriyle çalışmak zorunda ve
# ileride kayıtlı veriden okuyan bir uygulama geçmişi yeniden oynatmak için
# aynı yere takılacak.
#
# NOTE: period parametresi Binance'in istatistik uçlarındaki aralıktır: 5m,
# 15m, 30m, 1h, 2h, 4h, 6h, 12h ya da 1d. Bu uçlar yalnızca son otuz günü verir.

from abc import ABC, abstractmethod
from enum import StrEnum

from .models import Candle, DailyStats, Funding, LongShortRatio, OpenInterest, OrderBook, Premium, TakerVolume


class MarketDataError(Exception):
    pass


# Binance üç ayrı long short oranı yayınlar. accounts bütün hesapların, top
# accounts en büyük hesapların hesap sayısına göre, top positions en büyük
# hesapların pozisyon büyüklüğüne göre long oranıdır.
class RatioKind(StrEnum):
    ACCOUNTS = "accounts"
    TOP_ACCOUNTS = "top_accounts"
    TOP_POSITIONS = "top_positions"


class MarketData(ABC):
    @abstractmethod
    async def candles(self, symbol: str, interval: str, limit: int = 500,
                      start: int | None = None, end: int | None = None) -> list[Candle]:
        """Mumları eskiden yeniye döndürür.

        start verilmezse end anından önceki son limit kadar mumu döndürür, end
        de verilmezse sonuncusu henüz kapanmamış olabilir. start verilirse start
        ile end arasında açılan bütün mumları limitten bağımsız olarak, gerekirse
        sayfa sayfa çeker. Geçmiş isteyen geri kalan metotlarda da start ve end
        aynı şekilde çalışır.

        Args:
            symbol: Parite.
            interval: Binance aralık kodu, örneğin 1m, 15m, 1h, 1d.
            limit: start verilmediğinde mum sayısı.
            start: İlk mumun açılış anı, unix saniye. Verilirse end de verilmelidir.
            end: Bu andan önce açılan mumlar döner.

        Raises:
            MarketDataError: Veri alınamazsa.
            ValueError: start verilip end verilmezse.
        """

    @abstractmethod
    async def daily_stats(self, symbol: str) -> DailyStats:
        """Son yirmi dört saatin değişimini, uçlarını ve dolar hacmini döndürür."""

    @abstractmethod
    async def premium(self, symbol: str | None = None) -> list[Premium]:
        """Mark fiyatını, endeksi ve tahmini fonlamayı döndürür. symbol verilmezse bütün pariteler için."""

    @abstractmethod
    async def funding_history(self, symbol: str, start: int | None = None, end: int | None = None,
                              limit: int = 1000) -> list[Funding]:
        """Gerçekleşmiş fonlamaları eskiden yeniye döndürür."""

    @abstractmethod
    async def open_interest(self, symbol: str) -> OpenInterest:
        """Anlık açık pozisyonu döndürür."""

    @abstractmethod
    async def open_interest_history(self, symbol: str, period: str, limit: int = 500,
                                    start: int | None = None, end: int | None = None) -> list[OpenInterest]:
        """Açık pozisyonun geçmişini eskiden yeniye döndürür. Binance yalnızca son otuz günü verir."""

    @abstractmethod
    async def long_short(self, kind: RatioKind, symbol: str, period: str, limit: int = 500,
                         start: int | None = None, end: int | None = None) -> list[LongShortRatio]:
        """Seçilen long short oranının geçmişini eskiden yeniye döndürür. Binance yalnızca son otuz günü verir."""

    @abstractmethod
    async def taker_volume(self, symbol: str, period: str, limit: int = 500,
                           start: int | None = None, end: int | None = None) -> list[TakerVolume]:
        """Agresif alış ve satış hacminin geçmişini eskiden yeniye döndürür. Binance yalnızca son otuz günü verir."""

    @abstractmethod
    async def order_book(self, symbol: str, depth: int = 20) -> OrderBook:
        """Emir defterinin anlık en iyi kademelerini döndürür."""
