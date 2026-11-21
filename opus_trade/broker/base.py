"""
file: base.py
importable: Broker, PriceDriven, Preview, ClosePreview

Broker bir hesabın soyut sınıfıdır. state() hesabın o anki halini döndürür,
execute() bir komutu uygular ve olan değişiklikleri AccountEvent olarak
döndürür, kurala aykırı komutta RuleError fırlatır. preview() bir giriş ya da
ekleme komutunu hesaba dokunmadan aynı kurallardan geçirir ve tahmini sonucunu verir, preview_close() aynısını
kapatma için yapar. Kapı, strateji ve admin
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

from ..domain.trading import (AccountEvent, AccountState, Actor, AddToPosition, ClosePosition, Command, OpenPosition,
                              PlaceOrder)


@dataclass(frozen=True)
class Preview:
    """Bir girişin tahmini. Emirde giriş tetik fiyatıdır, nakit bugünkü nakittir.

    Eklemede giriş, büyüklük, marjin ve komisyon yalnızca eklenen kısmındır. Likidasyon eklemeden sonraki pozisyonundur,
    avg_entry ve leverage de eklemeden sonraki ortalama giriş ve kaldıraçtır. Girişte bu ikisi None kalır.
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
    async def execute(self, cmd: Command, actor: Actor) -> list[AccountEvent]:
        """Komutu uygular ve olan değişiklikleri döndürür.

        Raises:
            RuleError: Komut hesap kurallarına aykırıysa ya da gösterdiği kayıt artık yoksa.
        """

    @abstractmethod
    def preview(self, cmd: OpenPosition | PlaceOrder | AddToPosition) -> Preview:
        """Giriş ya da ekleme komutunu uygulamadan execute() ile aynı kurallardan geçirir ve tahmini sonucunu döndürür.

        Raises:
            RuleError: Komut hesap kurallarına aykırıysa ya da eklenecek pozisyon artık yoksa.
        """

    @abstractmethod
    def preview_close(self, cmd: ClosePosition) -> ClosePreview:
        """Kapatma komutunu uygulamadan execute() ile aynı kurallardan geçirir ve tahmini sonucunu döndürür.

        Raises:
            RuleError: Yüzde geçersizse, kapanacak miktar en küçük adımın altındaysa ya da pozisyon artık yoksa.
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
