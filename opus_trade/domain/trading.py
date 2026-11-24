"""
file: trading.py
importable: Actor, OrderType, Ref, Protection, Position, Order, Trade, AccountState, EventKind, AccountEvent,
            SetLeverage, PlaceOrder, AmendOrder, CancelOrder, ClosePosition, CloseAll, Command, risky

Bir hesabın kayıtları, ona gönderilen komutlar ve hesapta olan olaylar,
Binance USDⓈ-M'nin hedge modundaki modeliyle. Pozisyon parite ve yön başına
tektir, numarası yoktur, BTCUSDT long diye anılır. Emirler numaralıdır,
bir emre dokunan komut onu Ref ile gösterir. Strateji, admin paneli ve
sistem aynı komutları gönderir, broker hangisinden geldiğini yalnızca Actor
olarak bilir.

description: Pozisyon, emir, kapanmış işlem, komut ve hesap olayı tipleri.
"""

# NOTE: Emir tek tiptir. reduce False olan emir pozisyon açar ya da büyütür,
# True olan pozisyonu küçültür. Koruma dediğimiz TP, SL ve iz, reduce True
# olan take-profit, stop ve trailing-stop emirleridir. Binance'te de hedge
# modunda yön ve alış satış birlikte emrin açan mı kapatan mı olduğunu söyler,
# burada alış satış yerine reduce tutulur, alış mı satış mı olduğu rules.is_buy
# ile çıkar.
#
# NOTE: number kullanıcıya ve Opus'a görünen numaradır, o an açık emirlerin
# kullanmadığı en küçük sayıdır ve emir kapanınca başka bir emre verilebilir.
# id veritabanının benzersiz kimliğidir. Ref'teki parite ve yön, numaranın
# arada başka bir emre geçtiği durumu yakalamak içindir.
#
# NOTE: Komutlar Binance gibi marjin değil miktar taşır. Marjinden miktara
# çeviri Opus'un ve panelin katmanının işidir, rules.qty_for_margin bunun için
# vardır. Canlı broker böylece komutu çevirmeden iletebilir.

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from .models import Side
from .rules import DEFAULT_LEVERAGE


class Actor(StrEnum):
    STRATEGY = "strategy"
    ADMIN = "admin"
    SYSTEM = "system"


# MARKET yalnızca anında dolan giriştir, defterde beklemez. STOP ve
# TAKE_PROFIT tetiklenince piyasa emri olur, Binance'teki STOP_MARKET ve
# TAKE_PROFIT_MARKET. TRAILING_STOP yalnızca kapatan emirdir.
class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    TAKE_PROFIT = "take_profit"
    TRAILING_STOP = "trailing_stop"


@dataclass(frozen=True)
class Ref:
    number: int
    symbol: str
    side: Side


# Bekleyen bir girişin üzerinde istek olarak duran koruma. Giriş dolunca
# her istek o girişin miktarıyla kapatan bir emir olur. qty None ise girişin
# dolan miktarının tamamıdır. price TP ve SL'de tetik fiyatıdır, izde boştur.
# by_last yalnızca SL'de seçilir, SL'yi mark fiyatı yerine son fiyatla
# tetikler. Binance'teki karşılığı workingType CONTRACT_PRICE'tır. İğnede
# mark fiyatı son fiyatın gerisinde kalır, mark'la tetiklenen SL geç çalışır.
@dataclass(frozen=True)
class Protection:
    type: OrderType
    price: float | None = None
    trail_pct: float | None = None
    qty: float | None = None
    by_last: bool = False


# --- Kayıtlar ---

@dataclass(frozen=True)
class Position:
    """Bir paritenin bir yöndeki açık pozisyonu.

    entry_price aynı yöndeki girişlerin miktarla ağırlıklı ortalamasıdır.
    margin izole marjindir, kaldıraç değişince değişmez. best ve worst
    pozisyonun açılıştan beri gördüğü en iyi ve en kötü mark fiyatıdır. fee
    bugüne kadar ödenen komisyon, funding ödenen fonlamadır, aldığı fonlama
    negatiftir.
    """

    id: int
    account_id: int
    symbol: str
    side: Side
    qty: float
    margin: float
    entry_price: float
    entry_time: int
    best_price: float
    best_time: int
    worst_price: float
    worst_time: int
    liq_price: float
    fee: float
    funding: float


@dataclass(frozen=True)
class Order:
    """Defterde bekleyen bir emir.

    price limitte emrin fiyatı, stop ve take-profit'te tetik fiyatıdır, izde
    boştur. qty kapatan emirde None ise emir tetiklendiği an pozisyonun
    kalanının tamamını kapatır. margin girişin kullanılabilir bakiyeden
    ayırdığı tutardır, kapatan emirde sıfırdır. best yalnızca izde dolu olur,
    emrin konduğu andan beri görülen en iyi mark fiyatıdır. protection
    yalnızca girişte bulunur. by_last SL'nin son fiyatla tetiklendiğini söyler.
    """

    id: int
    account_id: int
    number: int
    symbol: str
    side: Side
    type: OrderType
    reduce: bool
    qty: float | None
    price: float | None
    trail_pct: float | None
    best: float | None
    margin: float
    protection: tuple[Protection, ...]
    expires_at: int | None
    created_at: int
    by_last: bool = False


# Bir pozisyonun kapanan her parçası bir Trade'dir, tam kapanış da son
# parçadır. pnl fiyat kârı eksi fonlamadır, komisyon fee'de ayrı durur, net
# sonuç pnl eksi fee'dir. fee bu parçanın açılış ve kapanış komisyonlarının
# payıdır, margin bu parçanın serbest kalan marjinidir. tp_prices ve sl
# kapanış anında pozisyonu koruyan TP ve SL emirlerinin fiyatlarıdır, çıkış
# karnesi değmeyen TP'yi ve en kötü fiyatın SL'ye uzaklığını bunlarla ölçer.
# order_id kapanışı yapan emirdir, elle ya da likidasyonla kapanışta boştur.
@dataclass(frozen=True)
class Trade:
    id: int
    account_id: int
    position_id: int
    symbol: str
    side: Side
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
    tp_prices: tuple[float, ...] = ()
    sl: float | None = None
    order_id: int | None = None

    @property
    def net(self) -> float:
        return self.pnl - self.fee

    # Pozisyonun kaldıracı farklı kaldıraçlı girişlerden birleşebilir, bu
    # yüzden kayıtta tutulmaz, büyüklüğün marjine oranı olarak hesaplanır.
    @property
    def leverage(self) -> float:
        return self.entry_price * self.qty / self.margin


@dataclass(frozen=True)
class AccountState:
    """Hesabın bir andaki hali.

    cash pozisyonların marjini düşülmüş nakittir, bekleyen girişlerin ayırdığı
    marjin cash'ten düşmez, available onu da düşer. equity açık pozisyonların
    o anki kâr ve zararını içerir, kapanış maliyetini içermez. leverages
    yalnızca ayarlanmış paritelerin kaldıracını tutar.
    """

    account_id: int
    cash: float
    equity: float
    positions: tuple[Position, ...]
    orders: tuple[Order, ...]
    leverages: Mapping[str, int] = field(default_factory=dict)

    @property
    def available(self) -> float:
        return self.cash - sum(o.margin for o in self.orders)

    def leverage(self, symbol: str) -> int:
        return self.leverages.get(symbol, DEFAULT_LEVERAGE)

    def position(self, symbol: str, side: Side) -> Position | None:
        return next((p for p in self.positions if p.symbol == symbol and p.side is side), None)

    def order(self, number: int) -> Order | None:
        return next((o for o in self.orders if o.number == number), None)


# --- Olaylar ---

class EventKind(StrEnum):
    OPENED = "opened"
    ADDED = "added"
    CLOSED = "closed"
    LEVERAGE = "leverage"
    ORDER_PLACED = "order_placed"
    ORDER_AMENDED = "order_amended"
    ORDER_FILLED = "order_filled"
    ORDER_FAILED = "order_failed"
    ORDER_EXPIRED = "order_expired"
    ORDER_CANCELLED = "order_cancelled"
    FUNDING = "funding"


# Olay hesapta olan tek bir değişikliktir. Metni yoktur, Türkçe cümleyi onu
# gösteren taraf kurar. Opus'a, admin paneline ve herkese açık sayfaya aynı
# olay farklı sözlerle anlatılabilir. number emrin numarasıdır, pozisyon
# olaylarında ve kaldıraçta boştur. side kaldıraç olayında boştur, kaldıraç
# paritenin iki yönünü birlikte etkiler. trade kapanışta kapanan parçadır,
# reason kapanışın ya da başarısız emrin nedenidir. amount fonlamada tutar,
# kaldıraçta yeni kaldıraçtır. order_type ve reduce olayın emrinin türü ve
# kapatan emir olup olmadığıdır, kurulamayan bir korumada emir olmadığı için
# istenen türdür. Stop ve take-profit hem giriş hem koruma olabildiği için
# tür tek başına yetmez.
@dataclass(frozen=True)
class AccountEvent:
    kind: EventKind
    account_id: int
    time: int
    actor: Actor
    symbol: str
    side: Side | None = None
    number: int | None = None
    price: float | None = None
    qty: float | None = None
    position_id: int | None = None
    order_id: int | None = None
    trade: Trade | None = None
    reason: str | None = None
    amount: float | None = None
    order_type: OrderType | None = None
    reduce: bool = False


# --- Komutlar ---

# Kaldıraç paritenin ayarıdır, long ve short'u birlikte etkiler. Açık
# pozisyonun marjini ve likidasyonu değişmez, yeni kaldıraç sonraki girişlere
# uygulanır.
@dataclass(frozen=True)
class SetLeverage:
    symbol: str
    leverage: int


# Girişte qty zorunludur. Kapatan emirde qty None kalanın tamamıdır.
# price limitte emrin fiyatı, stop ve take-profit'te tetik fiyatıdır.
# protection yalnızca girişte verilir, valid_min verilirse emir o kadar dakika
# sonra kendiliğinden düşer.
@dataclass(frozen=True)
class PlaceOrder:
    symbol: str
    side: Side
    type: OrderType
    qty: float | None = None
    price: float | None = None
    reduce: bool = False
    trail_pct: float | None = None
    protection: tuple[Protection, ...] = ()
    valid_min: int | None = None
    by_last: bool = False


# Binance'teki gibi yalnızca limit emrin fiyatı ve miktarı değişir. qty None
# ise miktar aynı kalır.
@dataclass(frozen=True)
class AmendOrder:
    ref: Ref
    price: float
    qty: float | None = None


@dataclass(frozen=True)
class CancelOrder:
    ref: Ref


# Pozisyonu piyasa emriyle kapatır. qty None ise tamamını.
@dataclass(frozen=True)
class ClosePosition:
    symbol: str
    side: Side
    qty: float | None = None


# Bütün pozisyonları kapatır ve bütün emirleri iptal eder. Admin panelinin
# hepsini kapat düğmesi ve acil durdurma bunu gönderir.
@dataclass(frozen=True)
class CloseAll:
    pass


Command = SetLeverage | PlaceOrder | AmendOrder | CancelOrder | ClosePosition | CloseAll


# Hesabın riskini artırabilen komutlar. Kısıtlar ve risk sınırları yalnızca
# bunlara bakar, kapatma, iptal ve koruma hiçbir zaman engellenmez. Fiyat
# değiştirme yalnızca limit girişte olduğu için risklidir, miktarı büyütebilir.
def risky(cmd: Command) -> bool:
    if isinstance(cmd, PlaceOrder):
        return not cmd.reduce
    return isinstance(cmd, SetLeverage | AmendOrder)
