"""
file: limits.py
importable: Limit, NoOpen, MaxLeverage, MaxMargin, MaxNotional, SideOnly, breach

Bir hesabın riskini artıran komutlara konan sınırlar. Her Limit tek bir kuralı
anlatır, breaks() bir komutun o kuralı aşıp aşmadığını hesabın o anki
durumuna bakarak söyler, text() kuralı Türkçe anlatır. breach() bir sınır
listesinden aşılan ilkini bulur. Admin panelinin kesin kısıtları da profilin
risk sınırları da bu tiplerle kurulur.

description: Riski artıran komutlara konan sınırlar.
"""

# NOTE: Sınırlar yalnızca domain.trading.risky'nin riskli saydığı komutlara
# bakar: giriş emri, limit emrin değiştirilmesi ve kaldıraç ayarı. Kapatma,
# iptal ve koruma hiçbir zaman engellenmez, çünkü riski azaltmak her zaman
# serbest olmalı.
#
# NOTE: Komutlar miktar taşır. Marjin miktar, fiyat ve paritenin kaldıracından
# hesaplanır. Piyasa emrinin fiyatı komutta olmadığı için son fiyat price
# fonksiyonundan alınır.
#
# NOTE: Kısıt konmadan önce verilmiş bir bekleyen emir tetiklenince kısıta
# bakılmaz. Tetiklenme bir komut değil broker'ın kendi kuralıdır. Kısıtın o
# emri de kapsaması isteniyorsa emir ayrıca iptal edilir.

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

from ..domain import numbers
from ..domain.models import Side
from ..domain.trading import AccountState, AmendOrder, Command, PlaceOrder, SetLeverage, risky


# Bir komutun paritesi, yönü, kaldıracı ve eklediği marjin. existing aynı
# yöndeki açık pozisyonun marjinidir, giriş onunla birleşir ve pozisyon başına
# marjin sınırı toplamla karşılaştırılır. release değişen limit emrin eski
# ayırdığı tutardır, toplam marjinde iki kez sayılmaz. opens kaldıraç ayarında
# False'tur, ayar tek başına işlem açmaz. Kaldıraç ayarının yönü yoktur.
@dataclass(frozen=True)
class _Ask:
    symbol: str
    side: Side | None
    leverage: float
    margin: float
    existing: float
    release: float = 0.0
    opens: bool = True


def _ask(cmd: Command, state: AccountState, price: Callable[[str], float]) -> _Ask:
    match cmd:
        case SetLeverage():
            return _Ask(cmd.symbol, None, cmd.leverage, 0.0, 0.0, opens=False)
        case PlaceOrder():
            lev = state.leverage(cmd.symbol)
            at = cmd.price or price(cmd.symbol)
            pos = state.position(cmd.symbol, cmd.side)
            return _Ask(cmd.symbol, cmd.side, lev, (cmd.qty or 0) * at / lev, pos.margin if pos else 0.0)
        case AmendOrder():
            o = state.order(cmd.ref.number)
            lev = state.leverage(cmd.ref.symbol)
            qty = cmd.qty if cmd.qty is not None else (o.qty if o else 0)
            pos = state.position(cmd.ref.symbol, cmd.ref.side)
            return _Ask(cmd.ref.symbol, cmd.ref.side, lev, (qty or 0) * cmd.price / lev,
                        pos.margin if pos else 0.0, release=o.margin if o else 0.0)
    raise TypeError(f"Riskli olmayan komut: {cmd!r}")


class Limit(ABC):
    symbol: str | None

    def applies(self, ask: _Ask) -> bool:
        return self.symbol is None or self.symbol == ask.symbol

    @abstractmethod
    def breaks(self, ask: _Ask, state: AccountState) -> bool:
        """Komutun bu sınırı aşıp aşmadığını döndürür. Paritesi tutmayan sınır için çağrılmaz."""

    @abstractmethod
    def text(self) -> str:
        """Sınırı Türkçe tek bir ifadeyle anlatır."""


def _where(symbol: str | None) -> str:
    return f"{symbol} paritesinde " if symbol else ""


@dataclass(frozen=True)
class NoOpen(Limit):
    symbol: str | None = None

    def breaks(self, ask, state) -> bool:
        return ask.opens

    def text(self) -> str:
        return f"{_where(self.symbol)}yeni işlem açılmaz"


@dataclass(frozen=True)
class MaxLeverage(Limit):
    value: float
    symbol: str | None = None

    def breaks(self, ask, state) -> bool:
        return ask.leverage > self.value

    def text(self) -> str:
        return f"{_where(self.symbol)}kaldıraç en fazla {numbers.num(self.value)}x"


# total True ise sınır bütün açık pozisyonların ve bekleyen emirlerin
# marjinine birlikte uygulanır, değilse tek bir pozisyonun marjinine.
@dataclass(frozen=True)
class MaxMargin(Limit):
    value: float
    total: bool = False
    symbol: str | None = None

    def breaks(self, ask, state) -> bool:
        if self.total:
            used = sum(p.margin for p in state.positions) + sum(o.margin for o in state.orders)
            return used - ask.release + ask.margin > self.value + 1e-9
        return ask.existing + ask.margin > self.value + 1e-9

    def text(self) -> str:
        what = "toplam marjin" if self.total else "pozisyon başına marjin"
        return f"{_where(self.symbol)}{what} en fazla {numbers.num(self.value)} dolar"


# Büyüklük marjin çarpı kaldıraçtır. Kaldıraç ve marjin sınırları ayrı ayrı
# tutulsa da ikisinin çarpımı büyüyebilir, canlı hesapta asıl sınır budur.
@dataclass(frozen=True)
class MaxNotional(Limit):
    value: float
    symbol: str | None = None

    def breaks(self, ask, state) -> bool:
        return ask.margin * ask.leverage > self.value + 1e-9

    def text(self) -> str:
        return f"{_where(self.symbol)}işlem büyüklüğü en fazla {numbers.num(self.value)} dolar"


@dataclass(frozen=True)
class SideOnly(Limit):
    side: Side
    symbol: str | None = None

    def breaks(self, ask, state) -> bool:
        return ask.side is not None and ask.side is not self.side

    def text(self) -> str:
        return f"{_where(self.symbol)}yalnızca {self.side} açılır"


def breach(cmd: Command, state: AccountState, limits: list[Limit],
           price: Callable[[str], float]) -> Limit | None:
    """Komutun aştığı ilk sınırı döndürür, aşmıyorsa ya da komut riski artırmıyorsa None.

    Args:
        cmd: Denetlenen komut.
        state: Hesabın o anki hali.
        limits: Sınırlar.
        price: Paritenin son fiyatı, piyasa emrinin marjini bununla hesaplanır.
    """
    if not risky(cmd):
        return None
    ask = _ask(cmd, state, price)
    return next((x for x in limits if x.applies(ask) and x.breaks(ask, state)), None)
