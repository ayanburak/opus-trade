"""
file: models.py
importable: Side, Bracket, SymbolSpec, TpLevel, Sizing

Hesap kurallarının üzerinde çalıştığı küçük veri tipleri. Side bir pozisyonun
ya da emrin yönüdür. SymbolSpec bir paritenin Binance'teki işlem kurallarıdır,
Bracket onun kaldıraç ve bakım marjı kademelerinden biridir.
TpLevel bir kâr alma kademesidir. Sizing bir işlemin büyüklük hesabının
sonucudur. Hepsi değişmezdir.

description: Hesap kurallarının ortak veri tipleri.
"""

# NOTE: Pozisyon, emir, alarm ve komut tipleri burada değil, onları kullanan
# kâğıt broker yazılırken eklenecek. Kullanıcısı olmayan bir model şimdiden
# tasarlanırsa broker'ın gerçek ihtiyacına göre sonradan değişmek zorunda kalır.

from dataclasses import dataclass
from enum import StrEnum


class Side(StrEnum):
    LONG = "long"
    SHORT = "short"

    # Long'da fiyatın yükselmesi kâr, short'ta düşmesi kârdır. Kâr ve zarar
    # formüllerinin hepsi bu işaretle tek satırda yazılabilir.
    @property
    def sign(self) -> int:
        return 1 if self is Side.LONG else -1


# Binance'in kaldıraç ve bakım marjı kademesi. cap kademenin kapsadığı en
# büyük pozisyon büyüklüğüdür, dolar. Bakım marjı büyüklük × mm_rate −
# mm_amount'tur, mm_amount kademe sınırında bakım marjının sıçramamasını sağlar.
@dataclass(frozen=True)
class Bracket:
    cap: float
    max_leverage: int
    mm_rate: float
    mm_amount: float


# Değerler çekirdek açılırken Binance'ten okunur, burada varsayılan yoktur.
# note boş değilse paritenin ne olduğunu anlatan kısa bir Türkçe ad taşır,
# örneğin XAUUSDT için altın.
@dataclass(frozen=True)
class SymbolSpec:
    """Bir paritenin işlem kuralları.

    Args:
        symbol: Binance'teki sembol, örneğin BTCUSDT.
        name: Arayüzde görünen ad, örneğin BTC/USDT.
        tick: Fiyat adımı. Fiyat, tetik, TP ve SL bunun katıdır.
        qty_step: Miktar adımı. Miktar her zaman bunun katıdır.
        min_notional: Binance'in en küçük işlem büyüklüğü, dolar.
        max_market_qty: Tek bir piyasa emrinin en büyük miktarı.
        price_up: Alış limitinin mark fiyatına oranı en fazla bu kadar olabilir, örneğin 1.03.
        price_down: Satış limitinin mark fiyatına oranı en az bu kadar olabilir, örneğin 0.97.
        max_orders: Paritede aynı anda açık olabilecek emir sayısı.
        brackets: Kaldıraç ve bakım marjı kademeleri, küçükten büyüğe.
        slippage: Taker işlemlerde fiyatın aleyhe kayma oranı.
        note: Paritenin ne olduğunu anlatan kısa ad, yoksa boş.
    """

    symbol: str
    name: str
    tick: float
    qty_step: float
    min_notional: float
    max_market_qty: float
    price_up: float
    price_down: float
    max_orders: int
    brackets: tuple[Bracket, ...]
    slippage: float
    note: str = ""


# qty yalnızca açık bir pozisyondaki kademede doludur. Bekleyen bir emirde
# miktar henüz belli değildir ve kademe yalnızca yüzde taşır. Açık pozisyonda
# qty None ise o kademe pozisyonun kalanının tamamını kapatır, yuvarlama
# yüzünden geride küçük bir kırıntı kalmasın diye.
@dataclass(frozen=True)
class TpLevel:
    price: float
    pct: float
    qty: float | None = None


@dataclass(frozen=True)
class Sizing:
    """Bir işlemin yuvarlanmış miktarı, ona düşen gerçek marjin ve giriş komisyonu."""

    qty: float
    margin: float
    fee: float
