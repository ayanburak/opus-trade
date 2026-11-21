"""
file: base.py
importable: Broker, PriceDriven, Preview, ClosePreview

Broker bir hesabın soyut sınıfıdır. state() hesabın o anki halini döndürür,
execute() bir komutu uygular ve olan değişiklikleri AccountEvent olarak
döndürür, kurala aykırı komutta RuleError fırlatır. preview() bir giriş
emrini hesaba dokunmadan aynı kurallardan geçirir ve tahmini sonucunu verir,
preview_close() aynısını kapatma için yapar. Kapı, strateji ve admin paneli
yalnızca bunu bilir, hesabın kâğıt mı canlı mı olduğunu bilmez.

PriceDriven fiyat ve fonlamayla kendi kendine işleyen hesaplar içindir:
defterdeki emirlerin eşleşmesi, TP, SL, iz, likidasyon ve fonlama ödemesi.
Kâğıt broker bunu uygular. Canlı hesapta bu işleri Binance yapar ve canlı broker
onları kendi olay akışından öğrenir, bu yüzden PriceDriven'ı uygulamaz.

description: Broker ve fiyatla işleyen hesap için soyut sınıflar.
"""

# NOTE: execute() async'tir çünkü canlı broker her komutta Binance'e istek
# atar. Kâğıt broker'ın beklediği bir şey yoktur ama aynı imzayı taşır.

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..domain.trading import AccountEvent, AccountState, Actor, ClosePosition, Command, PlaceOrder


@dataclass(frozen=True)
class Preview:
    """Bir girişin tahmini. Piyasa girişinde giriş kaymalı güncel fiyat, bekleyen emirde emrin fiyatıdır.

    Giriş, büyüklük, marjin ve komisyon yalnızca bu girişindir. Aynı yönde açık pozisyon varsa giriş onunla birleşir,
    likidasyon birleşmiş pozisyonundur, avg_entry ve leverage birleşmiş pozisyonun ortalama girişi ve büyüklüğünün
    marjine oranıdır. Pozisyon yoksa bu ikisi None kalır. cash_after girişten sonraki kullanılabilir bakiyedir.
    """

    entry: float
    qty: float
    notional: float
    margin: float
    fee: float
    liq_price: float
    cash_after: float
    avg_entry: float | None = None
    leverage: float | None = None


@dataclass(frozen=True)
class ClosePreview:
    """Bir kapanışın tahmini. pnl fonlama düşülmüş fiyat kârıdır, fee açılış komisyonunun kapanan payıyla kapanış
    komisyonudur. net, kapanıştan sonra işlemin net'iyle aynıdır. remaining kapanıştan sonra kalan miktardır."""

    exit: float
    qty: float
    price_pnl: float
    funding: float
    fee: float
    net: float
    cash_after: float
    remaining: float


class Broker(ABC):
    account_id: int

    @abstractmethod
    def state(self) -> AccountState:
        """Hesabın o anki nakdini, özkaynağını, açık pozisyonlarını ve bekleyen emirlerini döndürür."""

    @abstractmethod
    def price(self, symbol: str) -> float:
        """Paritenin son işlem fiyatını döndürür.

        Raises:
            RuleError: Paritenin fiyatı henüz gelmediyse.
        """

    @abstractmethod
    async def execute(self, cmd: Command, actor: Actor) -> list[AccountEvent]:
        """Komutu uygular ve olan değişiklikleri döndürür.

        Raises:
            RuleError: Komut hesap kurallarına aykırıysa ya da gösterdiği kayıt artık yoksa.
        """

    @abstractmethod
    def preview(self, cmd: PlaceOrder, leverage: int | None = None) -> Preview:
        """Giriş emrini uygulamadan execute() ile aynı kurallardan geçirir ve tahmini sonucunu döndürür.

        Args:
            cmd: Önizlenecek giriş emri.
            leverage: Girişten önce ayarlanacak kaldıraç. Verilirse kaldıraç değişikliğinin kuralları da
                denetlenir ve tahmin bu kaldıraçla yapılır, hesabın ayarı değişmez.

        Raises:
            RuleError: Komut ya da kaldıraç değişikliği hesap kurallarına aykırıysa ya da emir kapatan bir emirse.
        """

    @abstractmethod
    def preview_close(self, cmd: ClosePosition) -> ClosePreview:
        """Kapatma komutunu uygulamadan execute() ile aynı kurallardan geçirir ve tahmini sonucunu döndürür.

        Raises:
            RuleError: Miktar geçersizse ya da pozisyon artık yoksa.
        """


class PriceDriven(ABC):
    @abstractmethod
    def on_price(self, symbol: str, open_: float, high: float, low: float, last: float,
                 time: int) -> list[AccountEvent]:
        """Son işlem fiyatının son kontrolden bu yana aralığıyla TP'leri ve giriş emirlerini işler.

        Args:
            symbol: Fiyatı gelen parite.
            open_: Aralığın başındaki fiyat. Akışta bir önceki son fiyat, kapalı geçen dakikada mumun açılışı.
            high: Aralığın en yüksek fiyatı.
            low: Aralığın en düşük fiyatı.
            last: Son fiyat, piyasa emirleri bununla gerçekleşir.
            time: Unix saniye.
        """

    @abstractmethod
    def on_mark(self, symbol: str, high: float, low: float, mark: float, time: int,
                fill: float | None) -> list[AccountEvent]:
        """Mark fiyatının son kontrolden bu yana aralığıyla SL'yi, izi ve likidasyonu işler.

        Args:
            symbol: Fiyatı gelen parite.
            high: Aralığın en yüksek mark fiyatı.
            low: Aralığın en düşük mark fiyatı.
            mark: Güncel mark fiyatı, özkaynak bununla hesaplanır.
            time: Unix saniye.
            fill: SL'nin gerçekleşeceği son işlem fiyatı. None ise SL kendi seviyesinden gerçekleşir, o anın son
                fiyatı bilinmediğinde, örneğin kapalı geçen dakikalar işlenirken.
        """

    @abstractmethod
    def on_funding(self, symbol: str, rate: float, mark: float, time: int) -> list[AccountEvent]:
        """Bir paritenin fonlama ödemesini o anda açık olan pozisyonlarına uygular."""
