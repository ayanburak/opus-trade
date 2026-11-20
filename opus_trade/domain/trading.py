"""
file: trading.py
importable: Actor, Ref, Position, Order, Trade, AccountState, EventKind, AccountEvent,
            OpenPosition, AddToPosition, ClosePosition, UpdateProtection, PlaceOrder, CancelOrder, CloseAll,
            Command, RISKY

Bir hesabın kayıtları, ona gönderilen komutlar ve hesapta olan olaylar.
Strateji, admin paneli ve sistem aynı komutları gönderir, broker hangisinden
geldiğini yalnızca Actor olarak bilir. Numarayla bir kayda dokunan komut onu
Ref ile gösterir, Ref numaranın yanında paritesini ve yönünü de taşır.

description: Pozisyon, emir, kapanmış işlem, komut ve hesap olayı tipleri.
"""

# NOTE: number kullanıcıya ve Opus'a görünen numaradır, o an açık olan
# pozisyon ve emirlerin kullanmadığı en küçük sayıdır ve kapanınca başka bir
# kayda verilebilir. id veritabanının benzersiz kimliğidir, geçmiş onunla
# tutulur. Ref'teki parite ve yön, numaranın arada başka bir kayda geçtiği
# durumu yakalamak içindir.
#
# NOTE: Komutlardaki tp_levels bir pozisyonda miktarı belli olmayan, yalnızca
# yüzde taşıyan kademelerdir. Tek bir TP isteyen, yüzde yüzlük tek kademe
# verir. UpdateProtection'da None değişmez demektir, boş tuple bütün
# kademeleri, sıfır SL'yi ya da izi kaldırır.

from dataclasses import dataclass
from enum import StrEnum

from .models import Side, TpLevel


class Actor(StrEnum):
    STRATEGY = "strategy"
    ADMIN = "admin"
    SYSTEM = "system"


@dataclass(frozen=True)
class Ref:
    number: int
    symbol: str
    side: Side


# --- Kayıtlar ---

@dataclass(frozen=True)
class Position:
    """Açık bir pozisyon.

    best ve worst pozisyonun açılıştan beri gördüğü en iyi ve en kötü
    fiyattır. İz süren stop best'ten hesaplanır. fee bugüne kadar ödenen
    komisyon, funding ödenen fonlamadır, aldığı fonlama negatiftir.
    """

    id: int
    account_id: int
    number: int
    symbol: str
    side: Side
    leverage: float
    qty: float
    margin: float
    entry_price: float
    entry_time: int
    tp_levels: tuple[TpLevel, ...]
    sl: float | None
    trail_pct: float | None
    best_price: float
    best_time: int
    worst_price: float
    worst_time: int
    liq_price: float
    fee: float
    funding: float
    order_id: int | None


@dataclass(frozen=True)
class Order:
    """Fiyat tetiğe gelince pozisyon açacak bekleyen bir emir.

    below True ise fiyat düşüp tetiğe değince, False ise yükselip değince
    gerçekleşir. maker lehe gelen tetiktir, kaymasız ve maker komisyonuyla dolar.
    """

    id: int
    account_id: int
    number: int
    symbol: str
    side: Side
    trigger: float
    below: bool
    maker: bool
    leverage: float
    margin: float
    tp_levels: tuple[TpLevel, ...]
    sl: float | None
    trail_pct: float | None
    expires_at: int | None
    created_at: int


# Bir pozisyonun kapanan her parçası bir Trade'dir, tam kapanış da son
# parçadır. pnl fiyat kârı eksi fonlamadır, komisyon fee'de ayrı durur, net
# sonuç pnl eksi fee'dir. fee bu parçanın açılış ve kapanış komisyonlarının
# payıdır. tp_levels ve sl pozisyonun kapanış anındaki korumasıdır.
@dataclass(frozen=True)
class Trade:
    id: int
    account_id: int
    position_id: int
    number: int
    symbol: str
    side: Side
    leverage: float
    qty: float
    margin: float
    entry_price: float
    entry_time: int
    exit_price: float
    exit_time: int
    reason: str
    pnl: float
    fee: float
    funding: float
    best_price: float
    worst_price: float
    tp_levels: tuple[TpLevel, ...] = ()
    sl: float | None = None

    @property
    def net(self) -> float:
        return self.pnl - self.fee


@dataclass(frozen=True)
class AccountState:
    """Hesabın bir andaki hali. equity açık pozisyonların o anki kâr ve zararını içerir, kapanış maliyetini içermez."""

    account_id: int
    cash: float
    equity: float
    positions: tuple[Position, ...]
    orders: tuple[Order, ...]

    def find(self, number: int) -> Position | Order | None:
        return next((x for x in (*self.positions, *self.orders) if x.number == number), None)


# --- Olaylar ---

class EventKind(StrEnum):
    OPENED = "opened"
    ADDED = "added"
    CLOSED = "closed"
    PROTECTION = "protection"
    ORDER_PLACED = "order_placed"
    ORDER_FILLED = "order_filled"
    ORDER_FAILED = "order_failed"
    ORDER_EXPIRED = "order_expired"
    ORDER_CANCELLED = "order_cancelled"
    FUNDING = "funding"


# Olay hesapta olan tek bir değişikliktir. Metni yoktur, Türkçe cümleyi onu
# gösteren taraf kurar. Opus'a, admin paneline ve herkese açık sayfaya aynı
# olay farklı sözlerle anlatılabilir. trade kapanışta kapanan parçadır,
# reason kapanışın ya da başarısız emrin nedenidir.
@dataclass(frozen=True)
class AccountEvent:
    kind: EventKind
    account_id: int
    time: int
    actor: Actor
    number: int
    symbol: str
    side: Side
    price: float | None = None
    position_id: int | None = None
    order_id: int | None = None
    trade: Trade | None = None
    reason: str | None = None
    amount: float | None = None


# --- Komutlar ---

# PlaceOrder'da taker True ise emir tetiğe değince, tetik hangi tarafta olursa
# olsun, piyasa emri gibi kaymayla ve taker komisyonuyla dolar. False ise
# lehe gelen tetik limit emri gibi maker, kırılım tetiği taker sayılır.

@dataclass(frozen=True)
class OpenPosition:
    symbol: str
    side: Side
    leverage: float
    margin: float
    tp_levels: tuple[TpLevel, ...] = ()
    sl: float | None = None
    trail_pct: float | None = None


@dataclass(frozen=True)
class AddToPosition:
    ref: Ref
    leverage: float
    margin: float


@dataclass(frozen=True)
class ClosePosition:
    ref: Ref
    pct: float = 100


@dataclass(frozen=True)
class UpdateProtection:
    ref: Ref
    tp_levels: tuple[TpLevel, ...] | None = None
    sl: float | None = None
    trail_pct: float | None = None


@dataclass(frozen=True)
class PlaceOrder:
    symbol: str
    side: Side
    trigger: float
    leverage: float
    margin: float
    tp_levels: tuple[TpLevel, ...] = ()
    sl: float | None = None
    trail_pct: float | None = None
    valid_min: int | None = None
    taker: bool = False


@dataclass(frozen=True)
class CancelOrder:
    ref: Ref


# Bütün pozisyonları kapatır ve bütün emirleri iptal eder. Admin panelinin
# hepsini kapat düğmesi ve acil durdurma bunu gönderir.
@dataclass(frozen=True)
class CloseAll:
    pass


Command = OpenPosition | AddToPosition | ClosePosition | UpdateProtection | PlaceOrder | CancelOrder | CloseAll

# Hesabın riskini artıran komutlar. Kısıtlar ve risk sınırları yalnızca bunlara
# bakar, kapatma, iptal ve koruma hiçbir zaman engellenmez.
RISKY = (OpenPosition, AddToPosition, PlaceOrder)
