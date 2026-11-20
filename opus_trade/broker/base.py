"""
file: base.py
importable: Broker, PriceDriven, Preview

Broker bir hesabın soyut sınıfıdır. state() hesabın o anki halini döndürür,
execute() bir komutu uygular ve olan değişiklikleri AccountEvent olarak
döndürür, kurala aykırı komutta RuleError fırlatır. preview() bir giriş
komutunu hesaba dokunmadan aynı kurallardan geçirir ve tahmini sonucunu verir. Kapı, strateji ve admin
paneli yalnızca bunu bilir, hesabın kâğıt mı canlı mı olduğunu bilmez.

PriceDriven fiyat ve fonlamayla kendi kendine işleyen hesaplar içindir:
TP, SL, iz süren stop, likidasyon, emir tetiği ve fonlama ödemesi. Kâğıt
broker bunu uygular. Canlı hesapta bu işleri Binance yapar ve canlı broker
onları kendi olay akışından öğrenir, bu yüzden PriceDriven'ı uygulamaz.

description: Broker ve fiyatla işleyen hesap için soyut sınıflar.
"""

# NOTE: execute() async'tir çünkü canlı broker her komutta Binance'e istek
# atar. Kâğıt broker'ın beklediği bir şey yoktur ama aynı imzayı taşır.

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..domain.trading import AccountEvent, AccountState, Actor, Command, OpenPosition, PlaceOrder


@dataclass(frozen=True)
class Preview:
    """Bir girişin tahmini. Emirde giriş tetik fiyatıdır, nakit bugünkü nakittir."""

    entry: float
    qty: float
    notional: float
    margin: float
    fee: float
    liq_price: float
    cash_after: float


class Broker(ABC):
    account_id: int

    @abstractmethod
    def state(self) -> AccountState:
        """Hesabın o anki nakdini, özkaynağını, açık pozisyonlarını ve bekleyen emirlerini döndürür."""

    @abstractmethod
    async def execute(self, cmd: Command, actor: Actor) -> list[AccountEvent]:
        """Komutu uygular ve olan değişiklikleri döndürür.

        Raises:
            RuleError: Komut hesap kurallarına aykırıysa ya da gösterdiği kayıt artık yoksa.
        """

    @abstractmethod
    def preview(self, cmd: OpenPosition | PlaceOrder) -> Preview:
        """Giriş komutunu uygulamadan execute() ile aynı kurallardan geçirir ve tahmini sonucunu döndürür.

        Raises:
            RuleError: Komut hesap kurallarına aykırıysa.
        """


class PriceDriven(ABC):
    @abstractmethod
    def on_price(self, symbol: str, high: float, low: float, last: float, time: int) -> list[AccountEvent]:
        """Son kontrolden bu yana görülen en yüksek ve en düşük fiyatla hesabı işler.

        Args:
            symbol: Fiyatı gelen parite.
            high: Aralığın en yüksek fiyatı.
            low: Aralığın en düşük fiyatı.
            last: Son fiyat, piyasa emirleri ve özkaynak bununla hesaplanır.
            time: Unix saniye.
        """

    @abstractmethod
    def on_funding(self, symbol: str, rate: float, mark: float, time: int) -> list[AccountEvent]:
        """Bir paritenin fonlama ödemesini o anda açık olan pozisyonlarına uygular."""
