"""
file: stream.py
importable: MarketStream, StreamListener

MarketStream canlı piyasa akışının soyut sınıfıdır. run() sonsuza kadar
dinler, bağlantı koparsa kendisi yeniden bağlanır ve olayları kurulurken
verilen StreamListener'a iletir. watch() dakikalık mumları dinlenecek
pariteleri değiştirir, akış çalışırken de çağrılabilir. Likidasyonlar her
zaman bütün piyasadan gelir.

description: Canlı mum ve likidasyon akışının soyut sınıfı.
"""

# NOTE: Pariteler çalışırken değiştirilebilir, çünkü admin panelinden yeni bir
# profil açıldığında programı yeniden başlatmadan onun paritelerinin de
# dinlenmesi gerekiyor.
#
# NOTE: Likidasyonların bütün piyasadan gelmesi bilerek seçildi. Akış küçüktür
# ve geçmişi Binance'ten sonradan alınamaz. Toplayıcı hepsini saklar, stratejiler
# kendi paritelerini süzer.

from abc import ABC, abstractmethod

from .models import Candle, Liquidation


# Metotlar varsayılan olarak hiçbir şey yapmaz. Bir dinleyici yalnızca ihtiyaç
# duyduğu olayı yazar, örneğin toplayıcı mumlarla ilgilenmez.
class StreamListener:
    async def on_connect(self) -> None:
        """Her bağlantı kurulduğunda, ilk mesajdan önce çağrılır. Kopukken kaçan veri bununla tamamlanır."""

    async def on_candle(self, candle: Candle, closed: bool) -> None:
        """Dakikalık mumun her güncellemesinde çağrılır. closed mum kapandıysa True'dur."""

    async def on_liquidation(self, liq: Liquidation) -> None:
        """Bütün piyasadaki her likidasyonda çağrılır."""


class MarketStream(ABC):
    @abstractmethod
    async def run(self, listener: StreamListener) -> None:
        """Akışı sonsuza kadar dinler, iptal edilene kadar dönmez."""

    @abstractmethod
    async def watch(self, symbols: set[str]) -> None:
        """Dakikalık mumları dinlenecek pariteleri verilen kümeyle değiştirir."""
