"""
file: paper.py
importable: PaperBroker

PaperBroker kâğıt hesaptır. Binance USDⓈ-M'nin hedge modundaki hesabını
domain.rules'taki kurallarla taklit eder, durumunu Ledger'a yazar. Komutlar
execute() ile uygulanır. Son işlem fiyatı geldikçe on_price() defterdeki
limit emirleri, tetikli girişleri ve TP'leri, mark fiyatı geldikçe on_mark()
SL'yi, izi ve likidasyonu, on_funding() fonlamayı işler. Her komut ve her
fiyat kontrolü tek bir işlemde yazılır, yarıda kalan değişiklik deftere
geçmez.

description: Binance USDⓈ-M hedge modu kurallarıyla simüle edilen kâğıt hesap.
"""

# NOTE: Kâğıt broker yalnızca eşleştirmeyi taklit eder. Komutun anlamı canlı
# brokerdakiyle aynıdır, canlı broker onu Binance'e çevirmeden iletir. Bu
# yüzden buradaki her ret, Binance'in aynı emri reddettiği bir duruma denk
# gelmelidir.
#
# NOTE: Fiyat kontrolü son kontrolden bu yana görülen aralıkla yapılır, iki
# güncelleme arasındaki iğneler de yakalanır. Her emir Binance'teki gibi kendi
# fiyatına bakar: limit, tetikli giriş ve TP son işlem fiyatına, SL, iz ve
# likidasyon mark fiyatına. Aynı dakika sonradan işlenirken önce mark, sonra
# son fiyat işlenir, yani SL TP'den önce gelir.
#
# NOTE: Kısmi kapanışta pozisyonun marjini, fonlaması ve komisyonu kapanan
# payla bölünür. Kapanıştan sonra bir miktar adımından az kalacaksa pozisyonun
# tamamı kapanır. Pozisyon tamamen kapanınca ona bağlı bütün kapatan emirler
# iptal edilir, Burak'ın 21.11 kararı. Kalanı aşan kapatan emir kalanı kapatır.
#
# NOTE: Defterdeki limit girişin maliyeti, yani marjini ile açık zararı,
# emir verilirken kullanılabilir bakiyeden ayrılır ve emrin margin alanında
# durur. Tetikli emir Binance'te defterde beklemez, tetiklenene kadar marjin
# ayırmaz. Dolumda emrin ayırdığı tutar serbest kalır ve gerçek marjin dolum
# fiyatından paritenin o anki kaldıracıyla hesaplanır.
#
# NOTE: Kapanan parçanın en iyi ve en kötü fiyatına kapanış fiyatı da katılır.
# Fiyat kontrolü SL ya da TP'de kapattığında o aralığın ucunu kaydetmeden
# kapatır, kapanış fiyatı katılmasa TP ile kapanan bir işlemin en iyi fiyatı
# TP'nin altında görünürdü.
#
# NOTE: İz ile kapanış SL nedeniyle yazılır. İz bir stop emridir ve Opus'un
# metinleri kapanışı bu nedenle zaten anlatır.

from dataclasses import replace
from typing import NamedTuple

from ..domain import numbers, rules
from ..domain.catalog import Catalog
from ..domain.clock import Clock
from ..domain.errors import RuleError
from ..domain.fees import FeeSchedule
from ..domain.models import Side, SymbolSpec
from ..domain.trading import (AccountEvent, AccountState, Actor, AmendOrder, CancelOrder, CloseAll, ClosePosition,
                              Command, EventKind, Order, OrderType, PlaceOrder, Position, Protection, Ref,
                              SetLeverage, Trade)
from ..storage.ledger import Ledger
from .base import Broker, ClosePreview, ExitOutlook, PriceDriven, Preview

# Bir girişin isteyebileceği en fazla TP kademesi, Binance'in bölünmüş TP'si.
_MAX_TP = 4

_EXITS = (OrderType.TAKE_PROFIT, OrderType.STOP, OrderType.TRAILING_STOP)


class _Entry(NamedTuple):
    """Bir girişin deftere yazılmadan önceki hesabı. qty, margin ve fee bu girişin, gerisi birleşmiş pozisyonundur."""

    spec: SymbolSpec
    price: float
    qty: float
    margin: float
    fee: float
    total_qty: float
    total_margin: float
    avg: float
    liq_price: float
    available_after: float
    position: Position | None


class _Exit(NamedTuple):
    """Bir kapanışın deftere yazılmadan önceki hesabı. pnl fonlamayı içerir, fee açılış payıyla kapanış komisyonudur."""

    qty: float
    full: bool
    share: float
    margin: float
    funding: float
    exit: float
    price_pnl: float
    pnl: float
    fee: float
    cash_after: float


def _better(side: Side, a: float, b: float) -> float:
    return max(a, b) if side is Side.LONG else min(a, b)


def _worse(side: Side, a: float, b: float) -> float:
    return min(a, b) if side is Side.LONG else max(a, b)


# Stop alışta fiyat yükselince, satışta düşünce tetiklenir, take-profit
# tersine.
def _above(type_: OrderType, buy: bool) -> bool:
    return buy == (type_ is OrderType.STOP)


class PaperBroker(Broker, PriceDriven):
    """Bir kâğıt hesabı yürütür.

    Args:
        ledger: Hesap defteri.
        account_id: Defterdeki hesabın kimliği.
        catalog: İşlem yapılabilen paritelerin kuralları.
        fees: Komisyon oranları.
        clock: Komutların zamanı için saat.
    """

    def __init__(self, ledger: Ledger, account_id: int, catalog: Catalog, fees: FeeSchedule, clock: Clock) -> None:
        self.ledger = ledger
        self.account_id = account_id
        self.catalog = catalog
        self.fees = fees
        self.clock = clock
        self.prices: dict[str, float] = {}
        self.marks: dict[str, float] = {}

    # --- Durum ---

    def state(self) -> AccountState:
        cash = self.ledger.cash(self.account_id)
        positions = self.ledger.positions(self.account_id)
        equity = cash + sum(p.margin + rules.unrealized_pnl(p.side, p.entry_price, p.qty, self._mark_or(p))
                            for p in positions)
        return AccountState(self.account_id, cash, equity, positions, self.ledger.orders(self.account_id),
                            self.ledger.leverages(self.account_id))

    # Açık kâr Binance'teki gibi mark fiyatıyla hesaplanır. Mark henüz
    # gelmediyse son fiyat, o da yoksa giriş fiyatı kullanılır.
    def _mark_or(self, p: Position) -> float:
        return self.marks.get(p.symbol, self.prices.get(p.symbol, p.entry_price))

    def price(self, symbol: str) -> float:
        if symbol not in self.prices:
            raise RuleError(f"{symbol} fiyatı henüz gelmedi, biraz sonra tekrar denenebilir.")
        return self.prices[symbol]

    def _mark(self, symbol: str) -> float:
        return self.marks.get(symbol) or self.price(symbol)

    def _leverage(self, symbol: str) -> int:
        return self.ledger.leverages(self.account_id).get(symbol, rules.DEFAULT_LEVERAGE)

    def _position(self, symbol: str, side: Side) -> Position | None:
        return next((p for p in self.ledger.positions(self.account_id, symbol) if p.side is side), None)

    def _need_position(self, symbol: str, side: Side) -> Position:
        p = self._position(symbol, side)
        if p is None:
            raise RuleError(f"Açık {symbol} {side} pozisyonu yok.")
        return p

    # Numaranın gösterdiği emir bulunur ve parite ile yönü komuttakiyle
    # karşılaştırılır. Numara arada başka bir emre geçtiyse komut reddedilir.
    def _resolve(self, ref: Ref) -> Order:
        o = next((x for x in self.ledger.orders(self.account_id) if x.number == ref.number), None)
        if o is None:
            raise RuleError(f"#{ref.number} numaralı bekleyen emir yok.")
        if (o.symbol, o.side) != (ref.symbol, ref.side):
            raise RuleError(f"#{ref.number} artık {ref.symbol} {ref.side} değil, {o.symbol} {o.side} emri. "
                            f"Güncel numaralara bakıp tekrar dene.")
        return o

    # Pozisyonun kapatan emirleri. Pozisyon parite ve yön başına tek olduğu
    # için emir pozisyonu bu ikisiyle bulur.
    def _exits(self, p: Position) -> list[Order]:
        return [o for o in self.ledger.orders(self.account_id, p.symbol) if o.reduce and o.side is p.side]

    def _available(self, release: float = 0.0) -> float:
        reserved = sum(o.margin for o in self.ledger.orders(self.account_id))
        return self.ledger.cash(self.account_id) - reserved + release

    def _event(self, kind: EventKind, actor: Actor, time: int, symbol: str, side: Side | None = None,
               order: Order | None = None, position: Position | None = None, **extra) -> AccountEvent:
        extra.setdefault("order_type", order.type if order else None)
        extra.setdefault("reduce", order.reduce if order else False)
        return AccountEvent(kind=kind, account_id=self.account_id, time=time, actor=actor, symbol=symbol,
                            side=side, number=order.number if order else None,
                            order_id=order.id if order else None, position_id=position.id if position else None,
                            **extra)

    # --- Komutlar ---

    async def execute(self, cmd: Command, actor: Actor) -> list[AccountEvent]:
        now = self.clock.time()
        with self.ledger.atomic():
            events = self._dispatch(cmd, actor, now)
            for e in events:
                self.ledger.log_event(e)
        return events

    def _dispatch(self, cmd: Command, actor: Actor, now: int) -> list[AccountEvent]:
        match cmd:
            case SetLeverage():
                return self._set_leverage(cmd, actor, now)
            case PlaceOrder(reduce=True):
                p = self._need_position(cmd.symbol, cmd.side)
                return self._place_exit(p, cmd.type, cmd.qty, cmd.price, cmd.trail_pct, actor, now)
            case PlaceOrder(type=OrderType.MARKET):
                return self._market_entry(cmd, actor, now)
            case PlaceOrder():
                return self._place_entry(cmd, actor, now)
            case AmendOrder():
                return self._amend(cmd, actor, now)
            case CancelOrder():
                o = self._resolve(cmd.ref)
                self.ledger.resolve_order(o.id, "cancelled", now, str(actor))
                return [self._event(EventKind.ORDER_CANCELLED, actor, now, o.symbol, o.side, o)]
            case ClosePosition():
                p = self._need_position(cmd.symbol, cmd.side)
                qty = self._close_qty(p, cmd.qty)
                return self._close(p, qty, self.price(p.symbol), now, str(actor), actor, maker=False)
            case CloseAll():
                out = []
                for p in self.ledger.positions(self.account_id):
                    out += self._close(p, p.qty, self.price(p.symbol), now, str(actor), actor, maker=False)
                for o in self.ledger.orders(self.account_id):
                    self.ledger.resolve_order(o.id, "cancelled", now, str(actor))
                    out.append(self._event(EventKind.ORDER_CANCELLED, actor, now, o.symbol, o.side, o))
                return out
        raise TypeError(f"Bilinmeyen komut: {cmd!r}")

    # --- Kaldıraç ---

    # Binance kaldıraç kontrolünde paritenin açık büyüklüğüne mark fiyatıyla
    # bakar. Hedge modunda iki yön ayrı pozisyondur, büyük olanı sınırdır.
    # Önizleme de kaldıracı değiştirmeden aynı denetimden geçirir.
    def _check_leverage(self, symbol: str, leverage: float) -> None:
        positions = self.ledger.positions(self.account_id, symbol)
        notional = max((p.qty * self._mark(p.symbol) for p in positions), default=0.0)
        rules.check_leverage_change(self.catalog.get(symbol), self._leverage(symbol), leverage, notional)

    def _set_leverage(self, cmd: SetLeverage, actor: Actor, now: int) -> list[AccountEvent]:
        self._check_leverage(cmd.symbol, cmd.leverage)
        self.ledger.set_leverage(self.account_id, cmd.symbol, int(cmd.leverage))
        return [self._event(EventKind.LEVERAGE, actor, now, cmd.symbol, amount=int(cmd.leverage))]

    # --- Giriş ---

    # Girişin bütün kuralları burada denetlenir. Gerçek giriş, emrin dolumu ve
    # önizleme bunu kullanır, panelin gördüğü tahminle hesabın yaptığı
    # ayrışamaz. release, dolan emrin kendi ayırdığı tutardır, dolumda serbest
    # kalır. leverage yalnızca önizlemede verilir, kaldıracı değişecek bir
    # girişin tahminidir.
    def _plan_entry(self, symbol: str, side: Side, qty: float, price: float, maker: bool, market: bool,
                    release: float = 0.0, leverage: int | None = None) -> _Entry:
        spec = self.catalog.get(symbol)
        rules.check_qty(spec, qty, price, market)
        leverage = leverage or self._leverage(symbol)
        p = self._position(symbol, side)
        total_qty = round(qty + (p.qty if p else 0), 10)
        avg = rules.merge_entry(p.qty, p.entry_price, qty, price) if p else price
        rules.check_notional(spec, leverage, total_qty * avg)
        margin = rules.order_margin(qty, price, leverage)
        fee = qty * price * self.fees.rate(maker)
        available = self._available(release)
        if margin + fee > available + rules.TOLERANCE:
            raise RuleError(f"Yetersiz kullanılabilir bakiye. Kullanılabilir {numbers.num(available, 2)} dolar, "
                            f"bu giriş için {numbers.num(margin + fee, 2)} dolar gerekiyor.")
        total_margin = margin + (p.margin if p else 0)
        return _Entry(spec, price, qty, margin, fee, total_qty, total_margin, avg,
                      rules.liq_price(spec, side, avg, total_qty, total_margin), available - margin - fee, p)

    # Aynı yöndeki giriş açık pozisyonla birleşir. Pozisyonun en iyi ve en
    # kötü fiyatı korunur, onlar mark fiyatıyla ilerler.
    def _fill_entry(self, e: _Entry, symbol: str, side: Side, now: int) -> tuple[Position, EventKind]:
        cash = self.ledger.cash(self.account_id)
        self.ledger.set_cash(self.account_id, cash - e.margin - e.fee)
        if e.position:
            p = self.ledger.update_position(e.position.id, qty=e.total_qty, margin=e.total_margin,
                                            entry_price=e.avg, liq_price=e.liq_price, fee=e.position.fee + e.fee)
            return p, EventKind.ADDED
        p = self.ledger.insert_position(
            self.account_id, symbol=symbol, side=side, qty=e.qty, margin=e.margin, entry_price=e.price,
            entry_time=now, best_price=e.price, best_time=now, worst_price=e.price, worst_time=now,
            liq_price=e.liq_price, fee=e.fee, funding=0.0)
        return p, EventKind.OPENED

    # Girişin koruma istekleri girişle birlikte doğrulanır, dolumda kurulamayacak
    # bir istek baştan reddedilir. ref girişin beklenen fiyatıdır.
    def _check_protection(self, spec: SymbolSpec, side: Side, ref: float, qty: float,
                          protection: tuple[Protection, ...]) -> None:
        if sum(x.type is OrderType.TAKE_PROFIT for x in protection) > _MAX_TP:
            raise RuleError(f"Bir giriş en fazla {_MAX_TP} TP kademesi isteyebilir.")
        for x in protection:
            if x.type not in _EXITS:
                raise RuleError("Koruma yalnızca TP, SL ya da iz olabilir.")
            if x.qty is not None:
                rules.check_qty(spec, x.qty, ref, market=False)
                if x.qty > qty + rules.TOLERANCE:
                    raise RuleError(f"Korumanın miktarı {numbers.qty(x.qty, spec.symbol)}, girişin miktarı "
                                    f"{numbers.qty(qty, spec.symbol)} değerini aşamaz.")
            if x.type is OrderType.TRAILING_STOP:
                rules.check_trail(x.trail_pct)
                continue
            if not x.price or x.price <= 0:
                raise RuleError("TP ve SL'nin fiyatı sıfırdan büyük olmalı.")
            rules.check_ticks(spec, x.price)
            rules.check_trigger(x.type is OrderType.STOP, rules.is_buy(side, True), True, x.price, ref,
                                "girişin fiyatı")

    # Dolan girişin koruma istekleri o girişin miktarıyla kapatan emir olur.
    # Fiyat arada korumanın seviyesini geçtiyse Binance o emri reddeder ve
    # pozisyon o korumadan yoksun kalır. Burada da emir kurulmaz, olay nedenini
    # söyler.
    def _arm(self, p: Position, qty: float, protection: tuple[Protection, ...], now: int) -> list[AccountEvent]:
        out = []
        for x in protection:
            try:
                out += self._place_exit(p, x.type, x.qty or qty, x.price, x.trail_pct, Actor.SYSTEM, now)
            except RuleError as e:
                out.append(self._event(EventKind.ORDER_FAILED, Actor.SYSTEM, now, p.symbol, p.side, position=p,
                                       price=x.price, reason=str(e), order_type=x.type, reduce=True))
        return out

    def _market_entry(self, cmd: PlaceOrder, actor: Actor, now: int) -> list[AccountEvent]:
        spec = self.catalog.get(cmd.symbol)
        fill = rules.slipped(spec, self.price(cmd.symbol), buy=rules.is_buy(cmd.side, False))
        self._check_protection(spec, cmd.side, fill, cmd.qty or 0, cmd.protection)
        e = self._plan_entry(cmd.symbol, cmd.side, cmd.qty or 0, fill, maker=False, market=True)
        p, kind = self._fill_entry(e, cmd.symbol, cmd.side, now)
        return [self._event(kind, actor, now, p.symbol, p.side, position=p, price=fill, qty=e.qty),
                *self._arm(p, e.qty, cmd.protection, now)]

    # Bekleyen girişin kuralları emir verilirken emrin fiyatıyla denetlenir.
    # Limitin ayırdığı tutar da burada hesaplanır. replacing fiyatı değişen
    # emirdir, kendi yeri ve ayırdığı tutar hesaba katılmaz. leverage
    # _plan_entry'deki gibi yalnızca önizlemede verilir.
    def _check_entry(self, cmd: PlaceOrder, replacing: Order | None = None,
                     leverage: int | None = None) -> tuple[SymbolSpec, float]:
        spec, last = self.catalog.get(cmd.symbol), self.price(cmd.symbol)
        buy = rules.is_buy(cmd.side, False)
        if cmd.type not in (OrderType.LIMIT, OrderType.STOP, OrderType.TAKE_PROFIT):
            raise RuleError("Giriş emri piyasa, limit, stop ya da take-profit olabilir.")
        if not cmd.price or cmd.price <= 0:
            raise RuleError("Emrin fiyatı sıfırdan büyük olmalı.")
        if cmd.valid_min is not None and cmd.valid_min <= 0:
            raise RuleError("Geçerlilik süresi sıfırdan büyük olmalı.")
        rules.check_ticks(spec, cmd.price)
        rules.check_qty(spec, cmd.qty or 0, cmd.price, market=False)
        if len(self.ledger.orders(self.account_id, cmd.symbol)) - bool(replacing) >= spec.max_orders:
            raise RuleError(f"{cmd.symbol} için en fazla {spec.max_orders} bekleyen emir olabilir.")
        if cmd.type is OrderType.LIMIT:
            rules.check_price_band(spec, buy, cmd.price, self._mark(cmd.symbol))
        else:
            rules.check_trigger(cmd.type is OrderType.STOP, buy, False, cmd.price, last, "son fiyat")
        self._check_protection(spec, cmd.side, cmd.price, cmd.qty or 0, cmd.protection)
        reserve = 0.0
        if cmd.type is OrderType.LIMIT:
            leverage = leverage or self._leverage(cmd.symbol)
            reserve = (rules.order_margin(cmd.qty, cmd.price, leverage)
                       + rules.open_loss(buy, cmd.qty, cmd.price, self._mark(cmd.symbol)))
            available = self._available(replacing.margin if replacing else 0.0)
            if reserve > available + rules.TOLERANCE:
                raise RuleError(f"Yetersiz kullanılabilir bakiye. Kullanılabilir {numbers.num(available, 2)} "
                                f"dolar, bu emir {numbers.num(reserve, 2)} dolar ayırıyor.")
        return spec, reserve

    # Yanlış taraftaki limit Binance'te karşı tarafla hemen eşleşir ve taker
    # dolar. Dolum güncel fiyattan kaymayla olur ama limitten kötü olamaz.
    # Limit yanlış tarafta değilse ya da emir limit değilse None döner.
    def _cross_fill(self, spec: SymbolSpec, cmd: PlaceOrder) -> float | None:
        buy, last = rules.is_buy(cmd.side, False), self.price(cmd.symbol)
        if cmd.type is not OrderType.LIMIT or not rules.crosses(buy, cmd.price, last):
            return None
        fill = rules.slipped(spec, last, buy)
        return min(fill, cmd.price) if buy else max(fill, cmd.price)

    def _place_entry(self, cmd: PlaceOrder, actor: Actor, now: int) -> list[AccountEvent]:
        spec, reserve = self._check_entry(cmd)
        if (fill := self._cross_fill(spec, cmd)) is not None:
            e = self._plan_entry(cmd.symbol, cmd.side, cmd.qty, fill, maker=False, market=False)
            p, kind = self._fill_entry(e, cmd.symbol, cmd.side, now)
            return [self._event(kind, actor, now, p.symbol, p.side, position=p, price=fill, qty=e.qty),
                    *self._arm(p, e.qty, cmd.protection, now)]
        o = self.ledger.insert_order(Order(
            id=0, account_id=self.account_id, number=self.ledger.free_number(self.account_id), symbol=cmd.symbol,
            side=cmd.side, type=cmd.type, reduce=False, qty=cmd.qty, price=cmd.price, trail_pct=None, best=None,
            margin=reserve, protection=cmd.protection,
            expires_at=now + cmd.valid_min * 60 if cmd.valid_min else None, created_at=now))
        return [self._event(EventKind.ORDER_PLACED, actor, now, o.symbol, o.side, o, price=o.price, qty=o.qty)]

    def preview(self, cmd: PlaceOrder, leverage: int | None = None) -> Preview:
        if cmd.reduce:
            raise RuleError("Önizleme yalnızca giriş emri içindir.")
        spec = self.catalog.get(cmd.symbol)
        if leverage is not None:
            self._check_leverage(cmd.symbol, leverage)
        if cmd.type is OrderType.MARKET:
            price = rules.slipped(spec, self.price(cmd.symbol), buy=rules.is_buy(cmd.side, False))
            self._check_protection(spec, cmd.side, price, cmd.qty or 0, cmd.protection)
            maker, market = False, True
        else:
            _, reserve = self._check_entry(cmd, leverage=leverage)
            price, maker, market = cmd.price, cmd.type is OrderType.LIMIT, False
            # Yanlış taraftaki limitin tahmini gerçek dolumu gibi hemen ve taker olur.
            if (fill := self._cross_fill(spec, cmd)) is not None:
                price, maker = fill, False
        e = self._plan_entry(cmd.symbol, cmd.side, cmd.qty or 0, price, maker, market, leverage=leverage)
        merged = e.position is not None
        return Preview(e.price, e.qty, e.qty * e.price, e.margin, e.fee, e.liq_price, e.available_after,
                       avg_entry=e.avg if merged else None,
                       leverage=round(e.avg * e.total_qty / e.total_margin, 2) if merged else None)

    # --- Kapatan emir ---

    # Hedge modunda kapatan emir yalnızca açık pozisyona konur. Miktarsız
    # emir tetiklendiğinde pozisyonun kalanının tamamını kapatır. TP son
    # fiyata, SL ve iz mark fiyatına göre doğru tarafta olmalıdır. İzin en iyi
    # fiyatı emrin konduğu andaki mark fiyatından başlar.
    def _place_exit(self, p: Position, type_: OrderType, qty: float | None, price: float | None,
                    trail_pct: float | None, actor: Actor, now: int) -> list[AccountEvent]:
        spec = self.catalog.get(p.symbol)
        buy = rules.is_buy(p.side, True)
        if type_ not in _EXITS:
            raise RuleError("Kapatan emir TP, SL ya da iz olabilir. Pozisyonu hemen kapatmak için kapatma komutu "
                            "kullanılır.")
        if qty is not None:
            rules.check_qty(spec, qty, price or self.price(p.symbol), market=False)
            if qty > p.qty + rules.TOLERANCE:
                raise RuleError(f"Kapatan emrin miktarı {numbers.qty(qty, p.symbol)}, pozisyonun miktarı "
                                f"{numbers.qty(p.qty, p.symbol)} değerini aşamaz.")
        if len(self.ledger.orders(self.account_id, p.symbol)) >= spec.max_orders:
            raise RuleError(f"{p.symbol} için en fazla {spec.max_orders} bekleyen emir olabilir.")
        best = None
        if type_ is OrderType.TRAILING_STOP:
            rules.check_trail(trail_pct)
            price, best = None, self._mark(p.symbol)
        else:
            if not price or price <= 0:
                raise RuleError("TP ve SL'nin fiyatı sıfırdan büyük olmalı.")
            rules.check_ticks(spec, price)
            stop = type_ is OrderType.STOP
            if stop:
                rules.check_trigger(stop, buy, True, price, self._mark(p.symbol), "mark fiyatı")
            else:
                rules.check_trigger(stop, buy, True, price, self.price(p.symbol), "son fiyat")
            trail_pct = None
        o = self.ledger.insert_order(Order(
            id=0, account_id=self.account_id, number=self.ledger.free_number(self.account_id), symbol=p.symbol,
            side=p.side, type=type_, reduce=True, qty=qty, price=price, trail_pct=trail_pct, best=best, margin=0.0,
            protection=(), expires_at=None, created_at=now))
        return [self._event(EventKind.ORDER_PLACED, actor, now, o.symbol, o.side, o, position=p, price=o.price,
                            qty=o.qty)]

    # --- Değiştirme ---

    # Binance'teki gibi yalnızca limit emrin fiyatı ve miktarı değişir. Yeni
    # fiyat yanlış taraftaysa emir hemen taker dolar.
    def _amend(self, cmd: AmendOrder, actor: Actor, now: int) -> list[AccountEvent]:
        o = self._resolve(cmd.ref)
        if o.type is not OrderType.LIMIT:
            raise RuleError("Yalnızca limit emrin fiyatı değiştirilebilir. Diğer emir iptal edilip yeniden verilir.")
        qty = cmd.qty if cmd.qty is not None else o.qty
        place = PlaceOrder(o.symbol, o.side, OrderType.LIMIT, qty=qty, price=cmd.price, protection=o.protection)
        _, reserve = self._check_entry(place, replacing=o)
        buy, last = rules.is_buy(o.side, False), self.price(o.symbol)
        if rules.crosses(buy, cmd.price, last):
            spec = self.catalog.get(o.symbol)
            fill = rules.slipped(spec, last, buy)
            fill = min(fill, cmd.price) if buy else max(fill, cmd.price)
            return [self._event(EventKind.ORDER_AMENDED, actor, now, o.symbol, o.side, o, price=cmd.price, qty=qty),
                    *self._fill_order(o, fill, qty, maker=False, now=now)]
        o = self.ledger.update_order(o.id, price=cmd.price, qty=qty, margin=reserve)
        return [self._event(EventKind.ORDER_AMENDED, actor, now, o.symbol, o.side, o, price=o.price, qty=o.qty)]

    # --- Kapanış ---

    def _close_qty(self, p: Position, qty: float | None) -> float:
        if qty is None:
            return p.qty
        if qty < p.qty:
            rules.check_qty(self.catalog.get(p.symbol), qty, self.price(p.symbol), market=True)
        if qty > p.qty + rules.TOLERANCE:
            raise RuleError(f"Kapatılacak miktar {numbers.qty(qty, p.symbol)}, pozisyonun miktarı "
                            f"{numbers.qty(p.qty, p.symbol)} değerini aşamaz.")
        return qty

    def preview_close(self, cmd: ClosePosition) -> ClosePreview:
        p = self._need_position(cmd.symbol, cmd.side)
        x = self._exit(p, self._close_qty(p, cmd.qty), self.price(p.symbol), maker=False)
        return ClosePreview(x.exit, x.qty, x.price_pnl, x.funding, x.fee, x.pnl - x.fee, x.cash_after,
                            0.0 if x.full else round(p.qty - x.qty, 10))

    # TP ve stoplar gerçekte de fiyata en yakından başlayarak işlenir,
    # _check_tps ve _check_stops ile aynı sıra. Kayma tetik anının fiyatından
    # değil seviyeden hesaplanır, fiyat seviyeye sıçramadan gelmiş sayılır.
    # Bir stop likidasyonun ötesindeyse likidasyon önce gelir, o zaman
    # likidasyon da emri olmayan bir adım olarak sıraya girer.
    def outlook(self, p: Position) -> tuple[ExitOutlook, ...]:
        long = p.side is Side.LONG
        exits = self._exits(p)
        tps = sorted(((o, o.price, 0) for o in exits if o.type is OrderType.TAKE_PROFIT),
                     key=lambda x: x[1], reverse=not _above(OrderType.TAKE_PROFIT, rules.is_buy(p.side, True)))
        stops = [(o, o.price if o.type is OrderType.STOP else rules.trail_level(p.side, o.best, o.trail_pct), 0)
                 for o in exits if o.type is not OrderType.TAKE_PROFIT]
        if any((level < p.liq_price) if long else (level > p.liq_price) for _, level, _ in stops):
            stops.append((None, p.liq_price, 1))
        stops.sort(key=lambda x: (-x[1] if long else x[1], x[2]))
        return (*self._walk(p, tps), *self._walk(p, stops))

    # Kapanan pay kadar marjin, komisyon ve fonlama düşer, _close ile aynı.
    def _walk(self, p: Position, legs: list[tuple[Order | None, float, int]]) -> list[ExitOutlook]:
        out, current = [], p
        for o, level, _ in legs:
            if current is None:
                out.append(ExitOutlook(o.id if o else None, level, 0.0, 0.0))
                continue
            if o is None:
                x = self._exit(current, current.qty, level, maker=False, liquidation=True)
            else:
                x = self._exit(current, o.qty or current.qty, level, maker=False)
            out.append(ExitOutlook(o.id if o else None, level, x.qty, x.pnl - x.fee))
            rest = 1 - x.share
            current = None if x.full else replace(current, qty=round(current.qty - x.qty, 10),
                                                  margin=current.margin * rest, funding=current.funding * rest,
                                                  fee=current.fee * rest)
        return out

    # Kapanışın hesabı deftere yazmaz. Gerçek kapanış ve önizleme bunu
    # kullanır, kapatma kartının tahmini gerçek kapanıştan ayrışamaz. fill
    # verilmişse kayma uygulanmış dolum fiyatıdır.
    #
    # Likidasyonda pozisyon likidasyon fiyatından kapanır ve Binance kalan
    # marjinden likidasyon ücretini alır. Ücret kalan marjini aşamaz, izole
    # marjinde kayıp hiçbir zaman marjinden büyük olmaz. wiped fonlamanın
    # marjini tükettiği likidasyondur, marjinin tamamı gider.
    def _exit(self, p: Position, qty: float, price: float, maker: bool, liquidation: bool = False,
              fill: float | None = None, wiped: bool = False) -> _Exit:
        spec = self.catalog.get(p.symbol)
        qty = min(qty, p.qty)
        if p.qty - qty < spec.qty_step - 1e-12:
            qty = p.qty
        share = qty / p.qty
        margin, funding, open_fee = p.margin * share, p.funding * share, p.fee * share
        cash = self.ledger.cash(self.account_id)
        if wiped:
            exit_, price_pnl, close_fee = price, -margin, 0.0
        elif liquidation:
            price_pnl = max(rules.unrealized_pnl(p.side, p.entry_price, qty, price), -margin)
            close_fee = min(rules.liq_fee(spec, qty, price), margin + price_pnl)
            exit_ = price
        elif fill is not None:
            exit_, price_pnl = fill, rules.unrealized_pnl(p.side, p.entry_price, qty, fill)
            close_fee = qty * fill * self.fees.rate(maker)
        else:
            exit_, price_pnl, close_fee = rules.close_fill(spec, p.side, p.entry_price, qty, price, maker,
                                                           self.fees.rate(maker))
        return _Exit(qty, qty >= p.qty, share, margin, funding, exit_, price_pnl, price_pnl - funding,
                     open_fee + close_fee, cash + margin + price_pnl - close_fee)

    # Kapanış anında pozisyonu koruyan TP'lerin fiyatları ve fiyata en yakın
    # stop, iz dahil. Çıkış karnesi bunlarla ölçer.
    def _guards(self, p: Position) -> tuple[tuple[float, ...], float | None]:
        tps, stops = [], []
        for o in self._exits(p):
            if o.type is OrderType.TAKE_PROFIT:
                tps.append(o.price)
            elif o.type is OrderType.STOP:
                stops.append(o.price)
            elif o.best is not None:
                stops.append(rules.trail_level(p.side, o.best, o.trail_pct))
        sl = (max if p.side is Side.LONG else min)(stops) if stops else None
        return tuple(sorted(tps, reverse=p.side is Side.SHORT)), sl

    def _close(self, p: Position, qty: float, price: float, now: int, reason: str, actor: Actor, maker: bool,
               order: Order | None = None, liquidation: bool = False, fill: float | None = None,
               wiped: bool = False) -> list[AccountEvent]:
        x = self._exit(p, qty, price, maker, liquidation, fill, wiped)
        tps, sl = self._guards(p)
        self.ledger.set_cash(self.account_id, x.cash_after)
        trade = self.ledger.insert_trade(Trade(
            id=0, account_id=self.account_id, position_id=p.id, symbol=p.symbol, side=p.side, qty=x.qty,
            margin=x.margin, entry_price=p.entry_price, entry_time=p.entry_time, exit_price=x.exit, exit_time=now,
            reason=reason, pnl=x.pnl, fee=x.fee, funding=x.funding,
            best_price=_better(p.side, p.best_price, x.exit), worst_price=_worse(p.side, p.worst_price, x.exit),
            tp_prices=tps, sl=sl, order_id=order.id if order else None))
        if order:
            self.ledger.resolve_order(order.id, "filled", now, position_id=p.id)
        events = [self._event(EventKind.CLOSED, actor, now, p.symbol, p.side, order, p, price=trade.exit_price,
                              qty=trade.qty, trade=trade, reason=reason)]
        if not x.full:
            rest = 1 - x.share
            self.ledger.update_position(p.id, qty=round(p.qty - x.qty, 10), margin=p.margin * rest,
                                        funding=p.funding * rest, fee=p.fee * rest)
            return events
        self.ledger.mark_closed(p.id)
        for o in self._exits(p):
            self.ledger.resolve_order(o.id, "cancelled", now, "position_closed")
            events.append(self._event(EventKind.ORDER_CANCELLED, Actor.SYSTEM, now, o.symbol, o.side, o, p,
                                      reason="position_closed"))
        return events

    # --- Son fiyat ---

    def on_price(self, symbol: str, open_: float, high: float, low: float, last: float,
                 time: int) -> list[AccountEvent]:
        self.prices[symbol] = last
        with self.ledger.atomic():
            events = [*self._check_tps(symbol, open_, high, low, time),
                      *self._check_entries(symbol, open_, high, low, time)]
            for e in events:
                self.ledger.log_event(e)
        return events

    # TP take-profit-market emridir, son fiyatla tetiklenir ve kaymayla taker
    # dolar. Kademeler fiyata en yakından başlayarak işlenir.
    def _check_tps(self, symbol: str, open_: float, high: float, low: float, time: int) -> list[AccountEvent]:
        events = []
        spec = self.catalog.get(symbol)
        for p in self.ledger.positions(self.account_id, symbol):
            buy = rules.is_buy(p.side, True)
            above = _above(OrderType.TAKE_PROFIT, buy)
            tps = sorted((o for o in self._exits(p) if o.type is OrderType.TAKE_PROFIT),
                         key=lambda o: o.price, reverse=not above)
            for o in tps:
                if not rules.triggered(above, o.price, high, low):
                    break
                current = self.ledger.position(p.id)
                fill = rules.slipped(spec, rules.trigger_fill(above, o.price, open_), buy)
                events += self._close(current, o.qty or current.qty, o.price, time, "tp", Actor.SYSTEM,
                                      maker=False, order=o, fill=fill)
                if self._position(symbol, p.side) is None:
                    break
        return events

    # Limit seviyeyi bir tick geçince seviyesinden maker dolar. Tetikli giriş
    # son fiyatla tetiklenir ve kaymayla taker dolar.
    def _check_entries(self, symbol: str, open_: float, high: float, low: float, time: int) -> list[AccountEvent]:
        events = []
        spec = self.catalog.get(symbol)
        for o in self.ledger.orders(self.account_id, symbol):
            if o.reduce:
                continue
            if o.expires_at and time >= o.expires_at:
                self.ledger.resolve_order(o.id, "expired", time)
                events.append(self._event(EventKind.ORDER_EXPIRED, Actor.SYSTEM, time, o.symbol, o.side, o))
                continue
            buy = rules.is_buy(o.side, False)
            if o.type is OrderType.LIMIT:
                if rules.limit_reached(spec, buy, o.price, high, low):
                    events += self._fill_order(o, o.price, o.qty, maker=True, now=time)
                continue
            above = _above(o.type, buy)
            if rules.triggered(above, o.price, high, low):
                fill = rules.slipped(spec, rules.trigger_fill(above, o.price, open_), buy)
                events += self._fill_order(o, fill, o.qty, maker=False, now=time)
        return events

    # Emir dolarken nakit ve kaldıraç sınırı yeniden denetlenir, arada
    # değişmişse emir başarısız olur.
    def _fill_order(self, o: Order, fill: float, qty: float, maker: bool, now: int) -> list[AccountEvent]:
        try:
            e = self._plan_entry(o.symbol, o.side, qty, fill, maker, market=False, release=o.margin)
        except RuleError as err:
            self.ledger.resolve_order(o.id, "failed", now, str(err))
            return [self._event(EventKind.ORDER_FAILED, Actor.SYSTEM, now, o.symbol, o.side, o, reason=str(err))]
        p, kind = self._fill_entry(e, o.symbol, o.side, now)
        self.ledger.resolve_order(o.id, "filled", now, position_id=p.id)
        return [self._event(EventKind.ORDER_FILLED, Actor.SYSTEM, now, o.symbol, o.side, o, p, price=fill, qty=qty),
                self._event(kind, Actor.SYSTEM, now, p.symbol, p.side, position=p, price=fill, qty=qty),
                *self._arm(p, qty, o.protection, now)]

    # --- Mark fiyatı ---

    def on_mark(self, symbol: str, high: float, low: float, mark: float, time: int,
                fill: float | None) -> list[AccountEvent]:
        self.marks[symbol] = mark
        with self.ledger.atomic():
            events = []
            for p in self.ledger.positions(self.account_id, symbol):
                events += self._check_stops(p, high, low, time, fill)
            for e in events:
                self.ledger.log_event(e)
        return events

    # SL stop-market, iz trailing-stop-market emridir, ikisi de mark fiyatıyla
    # tetiklenir ve tetiklendiği anın son fiyatından kaymayla dolar. Son fiyat
    # seviyenin ötesine sıçradıysa dolum da oradadır. fill None ise o anın son
    # fiyatı bilinmez ve stop kendi seviyesinden dolar. Likidasyon kendi
    # seviyesinden olur.
    #
    # Fiyat zarar tarafında önce hangi seviyeye vardıysa o önce işlenir, eşitse
    # stop likidasyondan önce gelir. İzin en iyi fiyatı bu aralıkla ilerler
    # ama ancak bir sonraki fiyatla tetikleyebilir, aynı aralık hem izi taşıyıp
    # hem tetiklemez.
    def _check_stops(self, p: Position, high: float, low: float, time: int,
                     fill: float | None) -> list[AccountEvent]:
        long = p.side is Side.LONG
        worst, best = (low, high) if long else (high, low)
        spec = self.catalog.get(p.symbol)
        levels: list[tuple[float, int, Order | None]] = [(p.liq_price, 1, None)]
        for o in self._exits(p):
            if o.type is OrderType.STOP:
                levels.append((o.price, 0, o))
            elif o.type is OrderType.TRAILING_STOP:
                levels.append((rules.trail_level(p.side, o.best, o.trail_pct), 0, o))
        levels.sort(key=lambda x: (-x[0] if long else x[0], x[1]))
        events = []
        for level, _, o in levels:
            if not ((worst <= level) if long else (worst >= level)):
                break
            current = self.ledger.position(p.id)
            if o is None:
                events += self._close(current, current.qty, level, time, "liquidation", Actor.SYSTEM, maker=False,
                                      liquidation=True)
            else:
                at = fill if fill is not None else level
                price = rules.slipped(spec, at, rules.is_buy(p.side, True))
                events += self._close(current, o.qty or current.qty, at, time, "sl", Actor.SYSTEM, maker=False,
                                      order=o, fill=price)
            if self._position(p.symbol, p.side) is None:
                return events
        current = self.ledger.position(p.id)
        changes: dict = {}
        top = _better(p.side, current.best_price, best)
        if top != current.best_price:
            changes.update(best_price=top, best_time=time)
        bottom = _worse(p.side, current.worst_price, worst)
        if bottom != current.worst_price:
            changes.update(worst_price=bottom, worst_time=time)
        if changes:
            self.ledger.update_position(p.id, **changes)
        for o in self._exits(current):
            if o.type is OrderType.TRAILING_STOP and _better(p.side, o.best, best) != o.best:
                self.ledger.update_order(o.id, best=_better(p.side, o.best, best))
        return events

    # --- Fonlama ---

    def on_funding(self, symbol: str, rate: float, mark: float, time: int) -> list[AccountEvent]:
        events = []
        with self.ledger.atomic():
            spec = self.catalog.get(symbol)
            for p in self.ledger.positions(self.account_id, symbol):
                if p.entry_time > time or not rate:
                    continue
                payment = rules.funding_payment(p.side, p.qty, mark, rate)
                margin = p.margin - payment
                if margin <= 0:
                    events += self._close(p, p.qty, mark, time, "funding_liquidation", Actor.SYSTEM, maker=False,
                                          wiped=True)
                    continue
                self.ledger.update_position(p.id, margin=margin, funding=p.funding + payment,
                                            liq_price=rules.liq_price(spec, p.side, p.entry_price, p.qty, margin))
                events.append(self._event(EventKind.FUNDING, Actor.SYSTEM, time, symbol, p.side, position=p,
                                          price=mark, amount=payment))
            for e in events:
                self.ledger.log_event(e)
        return events
