"""
file: paper.py
importable: PaperBroker

PaperBroker kâğıt hesaptır. Komutları domain.rules'taki Binance kurallarıyla
uygular, durumunu Ledger'a yazar ve fiyat geldikçe on_price() ile TP, SL, iz
süren stop, likidasyon ve emir tetiklerini, on_funding() ile fonlamayı işler.
Her komut ve her fiyat kontrolü tek bir işlemde yazılır, yarıda kalan bir
değişiklik deftere geçmez.

description: Binance USDⓈ-M kurallarıyla simüle edilen kâğıt hesap.
"""

# NOTE: Fiyat kontrolü tek bir fiyatla değil son kontrolden bu yana görülen en
# yüksek ve en düşük fiyatla yapılır, iki güncelleme arasındaki iğneler de
# yakalanır. Aralığın içinde fiyatın önce hangi uca gittiği bilinmediği için
# temkinli sıra izlenir: önce zarar tarafı, sonra TP kademeleri, en son en iyi
# fiyat ve iz süren stop. Böylece aynı aralıktaki yükseliş kendi stopunu aynı
# anda tetikleyemez. Zarar tarafında SL ile likidasyondan fiyatın önce vardığı
# seçilir, ikisi eşitse SL.
#
# NOTE: Piyasa komutları son gelen fiyatla uygulanır. Bir paritenin fiyatı
# henüz gelmediyse komut reddedilir, eski sistemde açılışın ilk saniyelerinde
# görülen hata böyle kapanır.
#
# NOTE: Kısmi kapanışta pozisyonun marjini, fonlaması ve komisyonu kapanan
# payla bölünür. Kapanıştan sonra bir miktar adımından az kalacaksa pozisyonun
# tamamı kapanır.
#
# NOTE: Kapanan parçanın en iyi ve en kötü fiyatına kapanış fiyatı da katılır.
# Fiyat kontrolü SL ya da TP'de kapattığında o aralığın ucunu kaydetmeden
# kapatır, kapanış fiyatı katılmasa TP ile kapanan bir işlemin en iyi fiyatı
# TP'nin altında görünürdü.

from ..domain import rules
from ..domain.catalog import Catalog
from ..domain.clock import Clock
from ..domain.errors import RuleError
from ..domain.fees import FeeSchedule
from ..domain.models import Side
from ..domain.trading import (AccountEvent, AccountState, Actor, AddToPosition, CancelOrder, CloseAll, ClosePosition,
                              Command, EventKind, OpenPosition, Order, PlaceOrder, Position, Ref, Trade,
                              UpdateProtection)
from ..storage.ledger import Ledger
from .base import Broker, PriceDriven

# Bir emir tetiğinin güncel fiyattan en az bu oranda uzak olması gerekir. Daha
# yakın bir tetik ilk fiyatta dolardı ve aslında bir piyasa emridir.
_MIN_TRIGGER_GAP = 1e-5


def _better(side: Side, a: float, b: float) -> float:
    return max(a, b) if side is Side.LONG else min(a, b)


def _worse(side: Side, a: float, b: float) -> float:
    return min(a, b) if side is Side.LONG else max(a, b)


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

    # --- Durum ---

    def _mark(self, p: Position) -> float:
        return self.prices.get(p.symbol, p.entry_price)

    def state(self) -> AccountState:
        cash = self.ledger.cash(self.account_id)
        positions = self.ledger.positions(self.account_id)
        equity = cash + sum(p.margin + rules.unrealized_pnl(p.side, p.entry_price, p.qty, self._mark(p))
                            for p in positions)
        return AccountState(self.account_id, cash, equity, positions, self.ledger.orders(self.account_id))

    def _price(self, symbol: str) -> float:
        if symbol not in self.prices:
            raise RuleError(f"{symbol} fiyatı henüz gelmedi, biraz sonra tekrar denenebilir.")
        return self.prices[symbol]

    # Numaranın gösterdiği kayıt bulunur ve parite ile yönü komuttakiyle
    # karşılaştırılır. Numara arada başka bir kayda geçtiyse komut reddedilir.
    def _resolve(self, ref: Ref, kind: type) -> Position | Order:
        found = [x for x in (self.ledger.positions(self.account_id) if kind is Position
                             else self.ledger.orders(self.account_id)) if x.number == ref.number]
        what = "pozisyon" if kind is Position else "bekleyen emir"
        if not found:
            raise RuleError(f"#{ref.number} numaralı açık {what} yok.")
        x = found[0]
        if (x.symbol, x.side) != (ref.symbol, ref.side):
            raise RuleError(f"#{ref.number} artık {ref.symbol} {ref.side} değil, {x.symbol} {x.side} {what}. "
                            f"Güncel numaralara bakıp tekrar dene.")
        return x

    def _event(self, kind: EventKind, actor: Actor, x: Position | Order, time: int, **extra) -> AccountEvent:
        is_pos = isinstance(x, Position)
        return AccountEvent(kind=kind, account_id=self.account_id, time=time, actor=actor, number=x.number,
                            symbol=x.symbol, side=x.side, position_id=x.id if is_pos else extra.pop("position_id", None),
                            order_id=x.order_id if is_pos else x.id, **extra)

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
            case OpenPosition():
                p = self._open(cmd.symbol, cmd.side, cmd.leverage, cmd.margin, self._price(cmd.symbol), now,
                               cmd.tp_levels, cmd.sl, cmd.trail_pct)
                return [self._event(EventKind.OPENED, actor, p, now, price=p.entry_price)]
            case AddToPosition():
                p = self._add(self._resolve(cmd.ref, Position), cmd.leverage, cmd.margin, now)
                return [self._event(EventKind.ADDED, actor, p, now, price=p.entry_price)]
            case ClosePosition():
                p = self._resolve(cmd.ref, Position)
                if not 0 < cmd.pct <= 100:
                    raise RuleError("Kapatma yüzdesi sıfırdan büyük ve en fazla 100 olmalı.")
                spec = self.catalog.get(p.symbol)
                qty = p.qty if cmd.pct >= 99.9 else rules.round_qty(spec, p.qty * cmd.pct / 100)
                if qty <= 0:
                    raise RuleError(f"Kapatılacak miktar {p.symbol} için en küçük adımın altında kalıyor.")
                t = self._close(p, qty, self._price(p.symbol), now, str(actor), maker=False)
                return [self._event(EventKind.CLOSED, actor, p, now, price=t.exit_price, trade=t, reason=str(actor))]
            case UpdateProtection():
                p = self._protect(self._resolve(cmd.ref, Position), cmd)
                return [self._event(EventKind.PROTECTION, actor, p, now)]
            case PlaceOrder():
                o = self._place(cmd, now)
                return [self._event(EventKind.ORDER_PLACED, actor, o, now, price=o.trigger)]
            case CancelOrder():
                o = self._resolve(cmd.ref, Order)
                self.ledger.resolve_order(o.id, "cancelled", now, str(actor))
                return [self._event(EventKind.ORDER_CANCELLED, actor, o, now)]
            case CloseAll():
                out = []
                for p in self.ledger.positions(self.account_id):
                    t = self._close(p, p.qty, self._price(p.symbol), now, str(actor), maker=False)
                    out.append(self._event(EventKind.CLOSED, actor, p, now, price=t.exit_price, trade=t,
                                           reason=str(actor)))
                for o in self.ledger.orders(self.account_id):
                    self.ledger.resolve_order(o.id, "cancelled", now, str(actor))
                    out.append(self._event(EventKind.ORDER_CANCELLED, actor, o, now))
                return out
        raise TypeError(f"Bilinmeyen komut: {cmd!r}")

    # Emirden açılan pozisyon emrin numarasını ve tetik fiyatını alır, maker
    # emir kaymasız ve maker komisyonuyla dolar.
    def _open(self, symbol: str, side: Side, leverage: float, margin: float, price: float, now: int,
              tp_levels, sl, trail_pct, maker: bool = False, order: Order | None = None) -> Position:
        spec = self.catalog.get(symbol)
        rules.check_leverage(spec, leverage)
        entry = price if maker else rules.slipped(spec, price, buy=side is Side.LONG)
        cash = self.ledger.cash(self.account_id)
        s = rules.size(spec, leverage, margin, entry, self.fees.rate(maker), cash)
        rules.check_sl(side, entry, sl)
        rules.check_trail(trail_pct)
        levels = rules.split_tp(spec, rules.check_tp_levels(side, entry, list(tp_levels)), s.qty)
        self.ledger.set_cash(self.account_id, cash - s.margin - s.fee)
        return self.ledger.insert_position(
            self.account_id, number=order.number if order else self.ledger.free_number(self.account_id),
            symbol=symbol, side=side, leverage=leverage, qty=s.qty, margin=s.margin, entry_price=entry,
            entry_time=now, tp_levels=tuple(levels), sl=sl, trail_pct=trail_pct, best_price=entry, best_time=now,
            worst_price=entry, worst_time=now, liq_price=rules.liq_price(spec, side, entry, s.qty, s.margin),
            fee=s.fee, funding=0.0, order_id=order.id if order else None)

    # Ekleme kendi marjini ve kaldıracı olan küçük bir yeni pozisyon gibi
    # düşünülür, piyasa fiyatından taker olarak gerçekleşir. Giriş fiyatı
    # miktara göre ağırlıklı ortalanır, kaldıraç toplam büyüklüğün toplam
    # marjine oranı olur. TP kademelerine dokunulmaz.
    def _add(self, p: Position, leverage: float, margin: float, now: int) -> Position:
        spec = self.catalog.get(p.symbol)
        rules.check_leverage(spec, leverage)
        entry = rules.slipped(spec, self._price(p.symbol), buy=p.side is Side.LONG)
        cash = self.ledger.cash(self.account_id)
        s = rules.size(spec, leverage, margin, entry, self.fees.taker, cash)
        qty = round(p.qty + s.qty, 10)
        avg = (p.entry_price * p.qty + entry * s.qty) / qty
        total = p.margin + s.margin
        self.ledger.set_cash(self.account_id, cash - s.margin - s.fee)
        return self.ledger.update_position(p.id, qty=qty, entry_price=avg, margin=total,
                                           leverage=round(avg * qty / total, 2),
                                           liq_price=rules.liq_price(spec, p.side, avg, qty, total), fee=p.fee + s.fee)

    def _close(self, p: Position, qty: float, price: float, now: int, reason: str, maker: bool) -> Trade:
        spec = self.catalog.get(p.symbol)
        qty = min(qty, p.qty)
        if p.qty - qty < spec.qty_step - 1e-12:
            qty = p.qty
        full = qty >= p.qty
        share = qty / p.qty
        margin, funding, open_fee = p.margin * share, p.funding * share, p.fee * share
        cash = self.ledger.cash(self.account_id)
        if reason == "liquidation":
            exit_, pnl, close_fee = price, -margin - funding, 0.0
        else:
            exit_, price_pnl, close_fee = rules.close_fill(spec, p.side, p.entry_price, qty, price, maker,
                                                           self.fees.rate(maker))
            self.ledger.set_cash(self.account_id, cash + margin + price_pnl - close_fee)
            pnl = price_pnl - funding
        trade = self.ledger.insert_trade(Trade(
            id=0, account_id=self.account_id, position_id=p.id, number=p.number, symbol=p.symbol, side=p.side,
            leverage=p.leverage, qty=qty, margin=margin, entry_price=p.entry_price, entry_time=p.entry_time,
            exit_price=exit_, exit_time=now, reason=reason, pnl=pnl, fee=open_fee + close_fee, funding=funding,
            best_price=_better(p.side, p.best_price, exit_), worst_price=_worse(p.side, p.worst_price, exit_)))
        if full:
            self.ledger.mark_closed(p.id)
        else:
            rest = 1 - share
            self.ledger.update_position(p.id, qty=round(p.qty - qty, 10), margin=p.margin * rest,
                                        funding=p.funding * rest, fee=p.fee * rest)
        return trade

    # None değişmez demektir. Boş kademe listesi bütün TP'leri, sıfır SL'yi ya
    # da izi kaldırır. Yeni seviyeler güncel fiyata göre doğrulanır. İz
    # verilince SL hemen izin seviyesine çekilir, ilk fiyat beklenmez.
    def _protect(self, p: Position, cmd: UpdateProtection) -> Position:
        spec, price = self.catalog.get(p.symbol), self._price(p.symbol)
        levels, sl, trail = p.tp_levels, p.sl, p.trail_pct
        if cmd.tp_levels is not None:
            levels = tuple(rules.split_tp(spec, rules.check_tp_levels(p.side, price, list(cmd.tp_levels)), p.qty))
        if cmd.sl is not None:
            sl = cmd.sl or None
            rules.check_sl(p.side, price, sl)
        if cmd.trail_pct is not None:
            trail = cmd.trail_pct or None
            rules.check_trail(trail)
        if cmd.trail_pct and trail:
            sl = rules.trailed_sl(p.side, p.best_price, trail, sl, price)
        return self.ledger.update_position(p.id, tp_levels=levels, sl=sl, trail_pct=trail)

    # Emir kurulurken pozisyon açılırken yapılacak kontroller tetik fiyatıyla
    # yapılır, hiç açılamayacak bir emir tetik anında değil şimdi reddedilir.
    # Nakit emir dolunca kontrol edilir, arada azalmışsa emir başarısız olur.
    def _place(self, cmd: PlaceOrder, now: int) -> Order:
        spec, price = self.catalog.get(cmd.symbol), self._price(cmd.symbol)
        rules.check_leverage(spec, cmd.leverage)
        if cmd.margin <= 0:
            raise RuleError("Marjin sıfırdan büyük olmalı.")
        if cmd.trigger <= 0 or abs(cmd.trigger - price) / price < _MIN_TRIGGER_GAP:
            raise RuleError("Tetik fiyatı güncel fiyattan farklı ve pozitif olmalı.")
        qty = rules.round_qty(spec, cmd.margin * cmd.leverage / cmd.trigger)
        smallest = rules.min_notional(spec, cmd.trigger)
        if qty * cmd.trigger < smallest - rules.TOLERANCE:
            raise RuleError(f"{cmd.symbol} için en küçük işlem büyüklüğü tetik fiyatında yaklaşık {smallest:.2f} "
                            f"dolar. {cmd.leverage:g}x kaldıraçla en az {smallest / cmd.leverage:.2f} dolar marjin gerekir.")
        rules.check_sl(cmd.side, cmd.trigger, cmd.sl)
        rules.check_trail(cmd.trail_pct)
        levels = rules.check_tp_levels(cmd.side, cmd.trigger, list(cmd.tp_levels))
        rules.split_tp(spec, levels, qty)
        if cmd.valid_min is not None and cmd.valid_min <= 0:
            raise RuleError("Geçerlilik süresi sıfırdan büyük olmalı.")
        return self.ledger.insert_order(Order(
            id=0, account_id=self.account_id, number=self.ledger.free_number(self.account_id), symbol=cmd.symbol,
            side=cmd.side, trigger=cmd.trigger, below=cmd.trigger < price,
            maker=rules.order_is_maker(cmd.side, cmd.trigger, price), leverage=cmd.leverage, margin=cmd.margin,
            tp_levels=tuple(levels), sl=cmd.sl, trail_pct=cmd.trail_pct,
            expires_at=now + cmd.valid_min * 60 if cmd.valid_min else None, created_at=now))

    # --- Fiyat ---

    def on_price(self, symbol: str, high: float, low: float, last: float, time: int) -> list[AccountEvent]:
        self.prices[symbol] = last
        with self.ledger.atomic():
            events = [*self._check_positions(symbol, high, low, time), *self._check_orders(symbol, high, low, time)]
            for e in events:
                self.ledger.log_event(e)
        return events

    def _check_positions(self, symbol: str, high: float, low: float, time: int) -> list[AccountEvent]:
        events = []
        for p in self.ledger.positions(self.account_id, symbol):
            long = p.side is Side.LONG
            worst, best = (low, high) if long else (high, low)
            crossed = (lambda s: worst <= s) if long else (lambda s: worst >= s)
            reached = (lambda s: best >= s) if long else (lambda s: best <= s)

            stops = ([("sl", p.sl)] if p.sl else []) + [("liquidation", p.liq_price)]
            kind, level = (max if long else min)(stops, key=lambda x: x[1])
            if crossed(level):
                t = self._close(p, p.qty, level, time, kind, maker=False)
                events.append(self._event(EventKind.CLOSED, Actor.SYSTEM, p, time, price=t.exit_price, trade=t,
                                          reason=kind))
                continue

            levels, closed = list(p.tp_levels), False
            while levels and reached(levels[0].price):
                lv = levels.pop(0)
                current = self.ledger.position(p.id)
                t = self._close(current, lv.qty if lv.qty is not None else current.qty, lv.price, time, "tp",
                                maker=True)
                events.append(self._event(EventKind.CLOSED, Actor.SYSTEM, p, time, price=t.exit_price, trade=t,
                                          reason="tp"))
                closed = t.qty >= current.qty
                if closed:
                    break
            if closed:
                continue

            changes: dict = {}
            if len(levels) != len(p.tp_levels):
                changes["tp_levels"] = tuple(levels)
            top = _better(p.side, p.best_price, best)
            if top != p.best_price:
                changes.update(best_price=top, best_time=time)
            bottom = _worse(p.side, p.worst_price, worst)
            if bottom != p.worst_price:
                changes.update(worst_price=bottom, worst_time=time)
            if p.trail_pct:
                level = rules.trail_level(p.side, top, p.trail_pct)
                if not p.sl or (level > p.sl if long else level < p.sl):
                    changes["sl"] = level
            if changes:
                self.ledger.update_position(p.id, **changes)
        return events

    def _check_orders(self, symbol: str, high: float, low: float, time: int) -> list[AccountEvent]:
        events = []
        for o in self.ledger.orders(self.account_id, symbol):
            if o.expires_at and time >= o.expires_at:
                self.ledger.resolve_order(o.id, "expired", time)
                events.append(self._event(EventKind.ORDER_EXPIRED, Actor.SYSTEM, o, time))
                continue
            if not (low <= o.trigger if o.below else high >= o.trigger):
                continue
            try:
                p = self._open(o.symbol, o.side, o.leverage, o.margin, o.trigger, time, o.tp_levels, o.sl,
                               o.trail_pct, maker=o.maker, order=o)
            except RuleError as e:
                self.ledger.resolve_order(o.id, "failed", time, str(e))
                events.append(self._event(EventKind.ORDER_FAILED, Actor.SYSTEM, o, time, reason=str(e)))
                continue
            self.ledger.resolve_order(o.id, "filled", time, position_id=p.id)
            events.append(self._event(EventKind.ORDER_FILLED, Actor.SYSTEM, o, time, price=p.entry_price,
                                      position_id=p.id))
        return events

    # --- Fonlama ---

    def on_funding(self, symbol: str, rate: float, mark: float, time: int) -> list[AccountEvent]:
        events = []
        with self.ledger.atomic():
            for p in self.ledger.positions(self.account_id, symbol):
                if p.entry_time > time or not rate:
                    continue
                payment = rules.funding_payment(p.side, p.qty, mark, rate)
                margin = p.margin - payment
                if margin <= 0:
                    t = self._close(p, p.qty, mark, time, "liquidation", maker=False)
                    events.append(self._event(EventKind.CLOSED, Actor.SYSTEM, p, time, price=mark, trade=t,
                                              reason="funding_liquidation"))
                    continue
                spec = self.catalog.get(symbol)
                self.ledger.update_position(p.id, margin=margin, funding=p.funding + payment,
                                            liq_price=rules.liq_price(spec, p.side, p.entry_price, p.qty, margin))
                events.append(self._event(EventKind.FUNDING, Actor.SYSTEM, p, time, price=mark, amount=payment))
            for e in events:
                self.ledger.log_event(e)
        return events
