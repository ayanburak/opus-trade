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

# NOTE: Sınırlar yalnızca domain.trading.RISKY içindeki komutlara bakar:
# pozisyon açma, ekleme ve emir verme. Kapatma, iptal ve koruma değişikliği
# hiçbir zaman engellenmez, çünkü riski azaltmak her zaman serbest olmalı.
#
# NOTE: Kısıt konmadan önce verilmiş bir bekleyen emir tetiklenince kısıta
# bakılmaz. Tetiklenme bir komut değil broker'ın kendi kuralıdır. Kısıtın o
# emri de kapsaması isteniyorsa emir ayrıca iptal edilir.

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..domain.models import Side
from ..domain.trading import RISKY, AccountState, AddToPosition, Command, OpenPosition, PlaceOrder


# Bir komutun paritesi, yönü, kaldıracı ve eklediği marjin. Ekleme komutu
# pozisyonun var olan marjinini de taşır, pozisyon başına marjin sınırı
# toplamla karşılaştırılır.
@dataclass(frozen=True)
class _Ask:
    symbol: str
    side: Side
    leverage: float
    margin: float
    existing: float


def _ask(cmd: Command, state: AccountState) -> _Ask:
    match cmd:
        case OpenPosition() | PlaceOrder():
            return _Ask(cmd.symbol, cmd.side, cmd.leverage, cmd.margin, 0.0)
        case AddToPosition():
            pos = state.find(cmd.ref.number)
            return _Ask(cmd.ref.symbol, cmd.ref.side, cmd.leverage, cmd.margin, pos.margin if pos else 0.0)
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
        return True

    def text(self) -> str:
        return f"{_where(self.symbol)}yeni işlem açılmaz"


@dataclass(frozen=True)
class MaxLeverage(Limit):
    value: float
    symbol: str | None = None

    def breaks(self, ask, state) -> bool:
        return ask.leverage > self.value

    def text(self) -> str:
        return f"{_where(self.symbol)}kaldıraç en fazla {self.value:g}x"


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
            return used + ask.margin > self.value + 1e-9
        return ask.existing + ask.margin > self.value + 1e-9

    def text(self) -> str:
        what = "toplam marjin" if self.total else "pozisyon başına marjin"
        return f"{_where(self.symbol)}{what} en fazla {self.value:g} dolar"


# Büyüklük marjin çarpı kaldıraçtır. Kaldıraç ve marjin sınırları ayrı ayrı
# tutulsa da ikisinin çarpımı büyüyebilir, canlı hesapta asıl sınır budur.
@dataclass(frozen=True)
class MaxNotional(Limit):
    value: float
    symbol: str | None = None

    def breaks(self, ask, state) -> bool:
        return ask.margin * ask.leverage > self.value + 1e-9

    def text(self) -> str:
        return f"{_where(self.symbol)}işlem büyüklüğü en fazla {self.value:g} dolar"


@dataclass(frozen=True)
class SideOnly(Limit):
    side: Side
    symbol: str | None = None

    def breaks(self, ask, state) -> bool:
        return ask.side is not self.side

    def text(self) -> str:
        return f"{_where(self.symbol)}yalnızca {self.side} açılır"


def breach(cmd: Command, state: AccountState, limits: list[Limit]) -> Limit | None:
    """Komutun aştığı ilk sınırı döndürür, aşmıyorsa ya da komut riski artırmıyorsa None."""
    if not isinstance(cmd, RISKY):
        return None
    ask = _ask(cmd, state)
    return next((x for x in limits if x.applies(ask) and x.breaks(ask, state)), None)
