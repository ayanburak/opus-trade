"""
file: stream.py
importable: MarketStream, StreamListener, Feed

MarketStream canlı piyasa akışının soyut sınıfıdır. run() sonsuza kadar
dinler, bağlantı koparsa kendisi yeniden bağlanır ve olayları kurulurken
verilen StreamListener'a iletir. watch() her parite için hangi akışların
dinleneceğini verir: dakikalık mum, mark fiyatı ya da tekil işlemler. Akış
çalışırken de çağrılabilir, verilen istek öncekinin yerine geçer. Bazı akışlar
pariteye bağlı değildir ve uygulamanın kurulumunda seçilir, örneğin bütün
piyasanın likidasyonları.

description: Canlı mum, mark fiyatı, tekil işlem ve likidasyon akışının soyut sınıfı.
"""

# NOTE: Pariteler çalışırken değiştirilebilir, çünkü admin panelinden yeni bir
# profil açıldığında ya da bir izleme listesi saat başı değiştiğinde programı
# yeniden başlatmadan yeni paritelerin dinlenmesi gerekiyor.
#
# NOTE: Likidasyonların bütün piyasadan gelmesi bilerek seçildi. Akış küçüktür
# ve geçmişi Binance'ten sonradan alınamaz. Toplayıcı hepsini saklar, stratejiler
# kendi paritelerini süzer. Çekirdek mark fiyatını da bütün piyasadan tek
# akışla alır, parite başına mark akışı bağlantının akış sınırını tüketiyordu.

from abc import ABC, abstractmethod
from collections.abc import Collection, Mapping
from enum import StrEnum

from .models import Candle, Liquidation, Trade


class Feed(StrEnum):
    KLINE = "kline"
    MARK = "mark"
    TRADE = "trade"


# Metotlar varsayılan olarak hiçbir şey yapmaz. Bir dinleyici yalnızca ihtiyaç
# duyduğu olayı yazar, örneğin toplayıcı mumlarla ilgilenmez.
class StreamListener:
    async def on_connect(self) -> None:
        """Her bağlantı kurulduğunda, ilk mesajdan önce çağrılır. Kopukken kaçan veri bununla tamamlanır."""

    async def on_candle(self, candle: Candle, closed: bool) -> None:
        """Dakikalık mumun her güncellemesinde çağrılır. closed mum kapandıysa True'dur."""

    async def on_mark(self, symbol: str, mark: float, time: int) -> None:
        """Paritenin mark fiyatı her güncellendiğinde çağrılır, Binance'te saniyede bir."""

    async def on_trade(self, trade: Trade) -> None:
        """Dinlenen bir paritedeki her tekil işlemde çağrılır."""

    async def on_liquidation(self, liq: Liquidation) -> None:
        """Bütün piyasadaki her likidasyonda çağrılır."""


class MarketStream(ABC):
    @abstractmethod
    async def run(self, listener: StreamListener) -> None:
        """Akışı sonsuza kadar dinler, iptal edilene kadar dönmez."""

    @abstractmethod
    async def watch(self, wants: Mapping[str, Collection[Feed]]) -> None:
        """Dinlenecek pariteleri ve her paritenin akışlarını verilen istekle değiştirir."""
