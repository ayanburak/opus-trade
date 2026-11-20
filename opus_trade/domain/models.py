"""
file: models.py
importable: Side, SymbolSpec, TpLevel, Sizing

Hesap kurallarının üzerinde çalıştığı küçük veri tipleri. Side bir pozisyonun
ya da emrin yönüdür. SymbolSpec bir paritenin Binance'teki işlem kurallarıdır.
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


# Değerler Binance'in kendi kurallarıdır. Bakım marjı en düşük kademeye
# yakın tutulur, bu hesabın büyüklüklerinde üst kademelere hiç çıkılmaz.
# note boş değilse paritenin ne olduğunu anlatan kısa bir Türkçe ad taşır,
# örneğin XAUUSDT için altın.
@dataclass(frozen=True)
class SymbolSpec:
    """Bir paritenin işlem kuralları.

    Args:
        symbol: Binance'teki sembol, örneğin BTCUSDT.
        name: Arayüzde görünen ad, örneğin BTC/USDT.
        max_leverage: İzin verilen en yüksek kaldıraç.
        maint_margin: Bakım marjı oranı, örneğin 0.004.
        slippage: Taker işlemlerde fiyatın aleyhe kayma oranı.
        min_notional: Binance'in en küçük işlem büyüklüğü, dolar.
        qty_step: Miktar adımı. Miktar her zaman bunun katıdır.
        note: Paritenin ne olduğunu anlatan kısa ad, yoksa boş.
    """

    symbol: str
    name: str
    max_leverage: int
    maint_margin: float
    slippage: float
    min_notional: float
    qty_step: float
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
