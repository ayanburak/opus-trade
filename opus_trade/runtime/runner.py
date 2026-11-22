"""
file: runner.py
importable: ProfileRunner, describe, FUNDING_EVERY_S

ProfileRunner tek bir profili yürütür. Canlı mumları ve likidasyonları
broker'a ve tetikleyicilere verir, uyandırmaları kalıcı bir sırada biriktirir
ve tick() her saniye çağrıldıkça sıradakileri tek bir kararda stratejiye
verir. Kararın komutlarını kapıdan geçirir, alarm işlemlerini uygular,
hafızayı, günlüğü, girişlerin notlarını ve sınıflandırmaları yazar.
Strateji kararı kontrol hatasıyla reddettiyse hiçbir şey uygulamaz.
Reddedilen bir karar ya da komut varsa ve strateji isterse hemen düzeltme
için yeniden uyandırır. describe() bir komutun olaylarını
kısa bir Türkçe sonuca çevirir.

description: Bir profilin uyandırma, karar ve uygulama döngüsü.
"""

# NOTE: Bir profilde aynı anda tek karar süreci olur. Karar sürerken gelen
# uyandırmalar sırada bekler ve bir sonraki kararda birlikte verilir. Kilit
# dışarıdan verilir, böylece bir profil yeni sürüme geçerken eski
# çalıştırıcının süren kararı bitmeden yenisi karar vermeye başlamaz.
#
# NOTE: Strateji yalnızca profil çalışırken uyandırılır. Duraklatılmış ya da
# durdurulmuş profilin çalıştırıcısı yine kurulur, çünkü açık pozisyonların
# TP, SL ve emirleri fiyatla işlemeye devam etmelidir. O sırada olan olaylar
# stratejiye bildirilmez, günlükte ve hesap olaylarında durur.
#
# NOTE: Karar hata verirse uyandırmalar sırada kalır ve retry_s sonra yeniden
# denenir. Model erişimi kapalıysa model kullanan strateji hiç çağrılmaz,
# uyandırmalar birikir ve erişim açılınca hepsi tek kararda verilir.
#
# NOTE: Fiyatla işleyen hesaplarda kâğıt broker'ın kendi olayları burada olay
# yoluna yayılır. Komutların olaylarını kapı zaten yayar.

import asyncio
import logging
from dataclasses import replace
from typing import Callable

from ..broker.base import Broker, PriceDriven
from ..domain import numbers
from ..domain.clock import Clock
from ..domain.errors import RuleError
from ..domain.profile import Profile, State, Version
from ..domain.trading import AccountEvent, Actor, CancelOrder, EventKind, Ref
from ..market.models import Candle, Liquidation
from ..market.source import MarketData, MarketDataError
from ..storage.journal import JournalRepo
from ..storage.ledger import Ledger
from ..storage.notes import NotesRepo
from ..storage.runtime_state import StateRepo
from ..strategy.base import ActionResult, Context, Decision, Facts, Strategy, Wake
from ..strategy.llm.texts import invalidation_text, order_name
from ..triggers.base import Fire, Trigger
from ..triggers.price_alert import PriceAlerts
from . import facts as facts_
from . import wakes as wake_codec
from .bus import EventBus
from .gate import Gate
from .invalidation import Hit, InvalidationWatch
from .model_access import ModelAccess

log = logging.getLogger(__name__)

# Stratejiyi uyandıran hesap olayları. Fonlama ödemesi uyandırmaz, bir sonraki
# kararda hesabın halinde görünür.
_WAKING = (EventKind.CLOSED, EventKind.ORDER_FILLED, EventKind.ORDER_FAILED, EventKind.ORDER_EXPIRED)

# Fonlamaların Binance'ten kontrol edilme aralığı, saniye. Kaçırılan bir
# fonlama bir sonraki kontrolde uygulanır.
FUNDING_EVERY_S = 300


# Komutun sonucu stratejiye ve yöneticiye gider. Pozisyon parite ve yönüyle,
# emir numarası ve türüyle anılır. Koruma emrinin miktarsızı tetik anında
# kalanın tamamını kapatır, izin seviyesi olayda yoktur, yalnızca türü yazılır.
def _order(e: AccountEvent) -> str:
    return f"#{e.number} {e.symbol} {e.side} {order_name(e.order_type, e.reduce)}"


def describe(events: list[AccountEvent]) -> str:
    """Bir komutun olaylarını kısa bir Türkçe sonuca çevirir."""
    parts = []
    for e in events:
        pos = f"{e.symbol} {e.side}"
        match e.kind:
            case EventKind.OPENED:
                parts.append(f"{pos} pozisyonu {numbers.qty(e.qty, e.symbol)} ile {numbers.price(e.price)} "
                             f"fiyatından açıldı.")
            case EventKind.ADDED:
                parts.append(f"{pos} pozisyonuna {numbers.qty(e.qty, e.symbol)} {numbers.price(e.price)} "
                             f"fiyatından eklendi, pozisyon birleşti.")
            case EventKind.CLOSED:
                parts.append(f"{pos} pozisyonundan {numbers.qty(e.trade.qty, e.symbol)} {numbers.price(e.price)} "
                             f"fiyatından kapandı, net {numbers.num(e.trade.net, 2, sign=True)} dolar.")
            case EventKind.LEVERAGE:
                parts.append(f"{e.symbol} kaldıracı {numbers.num(e.amount)}x oldu.")
            case EventKind.ORDER_PLACED:
                level = f" {numbers.price(e.price)}" if e.price else ""
                qty = numbers.qty(e.qty, e.symbol) if e.qty is not None else "kalanın tamamı"
                parts.append(f"{_order(e)}{level} kuruldu, miktar {qty}.")
            case EventKind.ORDER_AMENDED:
                parts.append(f"{_order(e)} fiyatı {numbers.price(e.price)} oldu.")
            case EventKind.ORDER_CANCELLED if e.reason == "position_closed":
                parts.append(f"{_order(e)} pozisyon kapandığı için iptal oldu.")
            case EventKind.ORDER_CANCELLED:
                parts.append(f"{_order(e)} iptal edildi.")
            case EventKind.ORDER_FAILED:
                level = f" {numbers.price(e.price)}" if e.price else ""
                parts.append(f"{pos} pozisyonunun{level} {order_name(e.order_type, e.reduce)} kurulamadı: "
                             f"{e.reason}")
    return " ".join(parts) or "Açık pozisyon ve bekleyen emir yoktu."


class ProfileRunner:
    """Bir profilin çalıştırıcısı.

    Args:
        profile: Profil.
        version: Yürürlükteki sürüm.
        strategy: Sürümün ayarlarıyla kurulmuş strateji.
        broker: Profilin hesabı.
        gate: Hesabın kapısı.
        triggers: Sürümün tetikleyicileri.
        alerts: Profilin fiyat alarmları.
        ledger: Hesap defteri, işlem özeti ve özkaynak için.
        journal: Günlük ve hafıza deposu.
        state: Bekleyen uyandırmaların, alarmların ve ayarların deposu.
        market: Piyasa verisi.
        clock: Saat.
        bus: Olay yolu.
        access: Model erişimi.
        lock: Profilin karar kilidi.
        history: Stratejiye verilen son karar sayısı.
        retry_s: Hatalı bir karardan sonra yeniden denemeye kadar geçen süre.
        max_corrections: Arka arkaya en fazla düzeltme çağrısı.
        feed: Yönetici bölümünü ve gösterilecek müdahalelerin kimliklerini veren fonksiyon.
        delivered: Başarılı bir karardan sonra gösterilen müdahalelerin kimlikleriyle çağrılır.
        notes: Giriş notları ve sınıflandırmalar. Verilmezse hesap gerçekleri boş gider ve not yazılmaz.
    """

    def __init__(self, profile: Profile, version: Version, strategy: Strategy, broker: Broker, gate: Gate,
                 triggers: list[Trigger], alerts: PriceAlerts, ledger: Ledger, journal: JournalRepo,
                 state: StateRepo, market: MarketData, clock: Clock, bus: EventBus, access: ModelAccess,
                 lock: asyncio.Lock, history: int = 8, retry_s: int = 600, max_corrections: int = 2,
                 feed: Callable[[], tuple[dict, list[int]]] | None = None,
                 delivered: Callable[[list[int]], None] | None = None, notes: NotesRepo | None = None) -> None:
        self.profile = profile
        self.version = version
        self.strategy = strategy
        self.broker = broker
        self.gate = gate
        self.triggers = triggers
        self.alerts = alerts
        self.ledger = ledger
        self.journal = journal
        self.state = state
        self.market = market
        self.clock = clock
        self.bus = bus
        self.access = access
        self.lock = lock
        self.history = history
        self.retry_s = retry_s
        self.max_corrections = max_corrections
        self.feed = feed or (lambda: ({}, []))
        self.delivered = delivered or (lambda ids: None)
        self.notes = notes
        self.watch = InvalidationWatch(notes, profile.id) if notes else None
        self._cancels: list[Hit] = []
        self._side_tasks: set[asyncio.Task] = set()
        self.prices: dict[str, float] = {}
        self.marks: dict[str, float] = {}
        self.busy = False
        self.retiring = False
        self.retry_at = 0
        self.next_wake = self._last_next_wake()
        self._last_minute = 0
        self._seen: dict[str, Candle] = {}
        self._task: asyncio.Task | None = None

    @property
    def pid(self) -> int:
        return self.profile.id

    @property
    def symbols(self) -> tuple[str, ...]:
        return self.version.config.symbols

    @property
    def running(self) -> bool:
        return self.profile.state is State.RUNNING

    def _last_next_wake(self) -> int | None:
        found = self.journal.entries(self.pid, limit=1)
        return found[0]["next_wake"] if found else None

    # --- Uyandırma ---

    def wake(self, kind: str, data=None) -> None:
        if self.running:
            self.state.push_wake(self.pid, self.clock.time(), kind, wake_codec.encode(data))

    # Çalışan bir profil hiç karar vermediyse ilk kararı hemen alır. Duraklatılmış
    # ya da durdurulmuş bir profil yeniden çalıştırılınca strateji bunu bilerek
    # uyanır, aradaki olaylar ona verilmez, hesabın son halini zaten görür.
    def start(self, previous: State | None = None) -> None:
        if not self.running:
            return
        if not self.journal.entries(self.pid, limit=1) and not self.state.wakes(self.pid):
            self.wake("start")
        elif previous in (State.PAUSED, State.STOPPED):
            self.wake("resume", "Profilin yönetici tarafından yeniden çalıştırıldı. Durduğu sürede olanlar hesabın "
                                "şimdiki halinde görünüyor.")

    def _fire(self, fires: list[Fire]) -> None:
        for f in fires:
            if f.kind == "alert":
                self.state.resolve_alert(self.pid, f.data.number, "triggered", f.time)
            self.bus.publish({"profile": self.pid, "fire": f})
            self.wake("trigger", f)

    # Dolan bir girişin notu oluşan pozisyona bağlanır, Opus tezini
    # pozisyonun yanında görmeye devam eder.
    def _account(self, events: list[AccountEvent]) -> None:
        for e in events:
            self.bus.publish(e)
            if e.kind is EventKind.ORDER_FILLED and self.notes and e.order_id and e.position_id:
                self.notes.attach_position(e.order_id, e.position_id)
            if e.kind in _WAKING:
                self.wake("account", e)

    # --- Piyasa ---

    # Binance dakikalık mumu dakika boyunca birikmiş tepe ve dibiyle günceller.
    # Bu aralığın bir kısmı, dakika içinde açılan pozisyondan, kurulan emirden
    # ya da alarmdan öncedir. Broker ve alarmlar yalnızca bir önceki
    # güncellemeden beri görülen aralığı alır. Tepe yükselmediyse o aralıkta
    # görülen en yüksek fiyat bilinmez, önceki ve şimdiki kapanışın büyüğü
    # alınır. Dip için de aynısı geçerlidir. Dakikanın ilk güncellemesi mumun
    # tamamıyla gelir.
    def _fresh(self, candle: Candle) -> Candle:
        prev = self._seen.get(candle.symbol)
        self._seen[candle.symbol] = candle
        if prev is None or prev.time != candle.time:
            return candle
        high = candle.high if candle.high > prev.high else max(prev.close, candle.close)
        low = candle.low if candle.low < prev.low else min(prev.close, candle.close)
        return replace(candle, high=high, low=low)

    def on_candle(self, candle: Candle, closed: bool) -> None:
        if candle.symbol not in self.symbols:
            return
        before = self.prices.get(candle.symbol, candle.open)
        self.prices[candle.symbol] = candle.close
        fresh = self._fresh(candle)
        if isinstance(self.broker, PriceDriven):
            self._account(self.broker.on_price(candle.symbol, before, fresh.high, fresh.low, candle.close,
                                               self.clock.time()))
        for t in self.triggers:
            self._fire(t.on_candle(candle))
        self._fire(self.alerts.on_candle(fresh))
        self._invalidations(candle.symbol, fresh.high, fresh.low, self.clock.time())
        if closed:
            self.state.set_setting(f"price_seen:{self.pid}:{candle.symbol}", candle.time)

    # Mark fiyatı saniyede bir tek değer olarak gelir, aralığın iki ucu da odur.
    # SL o anın son fiyatından dolar.
    def on_mark(self, symbol: str, mark: float, time: int) -> None:
        if symbol not in self.symbols:
            return
        self.marks[symbol] = mark
        if isinstance(self.broker, PriceDriven):
            self._account(self.broker.on_mark(symbol, mark, mark, mark, time, self.prices.get(symbol)))

    def on_liquidation(self, liq: Liquidation) -> None:
        for t in self.triggers:
            self._fire(t.on_liquidation(liq))

    # Program kapalıyken ya da akış kopukken geçen dakikalar REST'ten çekilip
    # sırayla işlenir. Kaçan bir dakikadaki SL ya da TP böylece kaybolmaz. Her
    # dakikada önce mark mumu, sonra son fiyat mumu işlenir, yani SL önce gelir.
    # O dakikanın hangi anında tetiklendiği bilinmediği için SL kendi
    # seviyesinden dolar.
    async def catch_up(self) -> None:
        if not isinstance(self.broker, PriceDriven):
            return
        now = self.clock.time()
        end = now // 60 * 60
        for s in self.symbols:
            seen = self.state.setting(f"price_seen:{self.pid}:{s}")
            if seen is None or seen + 60 >= end:
                continue
            candles = await self.market.candles(s, "1m", start=seen + 60, end=end)
            marks = {m.time: m for m in await self.market.mark_candles(s, start=seen + 60, end=end)}
            for c in candles:
                if m := marks.get(c.time):
                    self._account(self.broker.on_mark(s, m.high, m.low, m.close, c.time + 59, None))
                    self.marks[s] = m.close
                self._account(self.broker.on_price(s, c.open, c.high, c.low, c.close, c.time + 59))
                self._invalidations(s, c.high, c.low, c.time + 59)
                self.prices[s] = c.close
            if candles:
                self.state.set_setting(f"price_seen:{self.pid}:{s}", candles[-1].time)
            if end - seen > 180:
                self.wake("restart", f"Sistem {(end - seen) // 60} dakika boyunca fiyatları işleyemedi. "
                                     f"Bu sürenin mumları şimdi işlendi, o sırada olanlar ayrıca bildirildi.")

    # Gerçekleşen fonlamalar Binance'ten okunur ve kâğıt hesaba uygulanır.
    # Son uygulanan fonlamanın zamanı saklanır, program kapalıyken kaçanlar
    # açılışta uygulanır.
    async def apply_funding(self) -> None:
        if not isinstance(self.broker, PriceDriven):
            return
        now = self.clock.time()
        for s in self.symbols:
            key = f"funding_seen:{self.pid}:{s}"
            seen = self.state.setting(key)
            if seen is None:
                self.state.set_setting(key, now)
                continue
            try:
                found = await self.market.funding_history(s, start=seen + 1, end=now)
            except MarketDataError as e:
                log.warning("[%s] %s fonlaması alınamadı: %s", self.profile.short, s, e)
                continue
            for f in found:
                self._account(self.broker.on_funding(s, f.rate, f.mark or self.prices.get(s, 0), f.time))
                self.state.set_setting(key, f.time)

    # --- Geçersizlik ---

    # Açık pozisyonun seviyesine değilince Opus hemen uyandırılır. Bekleyen
    # girişin iptali kapıdan gider ve asenkrondur, bir sonraki tick'te
    # gönderilir. Opus iptal için çağrılmaz, bir sonraki çağrıda hesap
    # bölümünde görür.
    def _invalidations(self, symbol: str, high: float, low: float, now: int) -> None:
        if not self.watch:
            return
        for hit in self.watch.on_price(symbol, high, low, self.broker.state()):
            if hit.order is not None:
                self._cancels.append(hit)
                continue
            n = hit.note
            self.wake("invalidation", invalidation_text(n.symbol, n.side, n.invalid_price, n.invalid_text))
            self.watch.done(hit, now)

    async def _cancel(self, hits: list[Hit]) -> None:
        for hit in hits:
            o = hit.order
            now = self.clock.time()
            try:
                await self.gate.submit(CancelOrder(Ref(o.number, o.symbol, o.side)), Actor.SYSTEM, now)
                self.watch.done(hit, now)
                self.state.system_event(self.pid, now, "invalidated", f"#{o.number} {o.symbol} {o.side} girişi "
                                        f"geçersizlik fiyatına değdiği için iptal edildi.",
                                        {"order_id": o.id, "price": hit.note.invalid_price})
            except RuleError as e:
                self.watch.failed(hit)
                log.info("[%s] Geçersizlik iptali gönderilemedi: %s", self.profile.short, e)

    async def flush_cancels(self) -> None:
        """Sırada bekleyen geçersizlik iptallerini gönderir. Testler ve tick bunu kullanır."""
        hits, self._cancels = self._cancels, []
        if hits:
            await self._cancel(hits)

    # --- Zaman ---

    def tick(self, now: int) -> None:
        if self._cancels:
            task = asyncio.create_task(self.flush_cancels())
            self._side_tasks.add(task)
            task.add_done_callback(self._side_tasks.discard)
        for t in self.triggers:
            self._fire(t.tick(now))
        if now // 60 != self._last_minute:
            self._last_minute = now // 60
            st = self.broker.state()
            self.ledger.record_equity(st.account_id, now, st.cash, st.equity, sum(p.margin for p in st.positions))
            self._check_daily_loss(st)
        if self.running and self.next_wake is not None and now >= self.next_wake:
            self.next_wake = None
            self.wake("timer")
        if not self.running or self.busy or self.retiring or now < self.retry_at or not self.state.wakes(self.pid):
            return
        if self.strategy.uses_model and self.access.closed():
            return
        self.busy = True
        self._task = asyncio.create_task(self._process())

    # Günlük zarar sınırı aşılınca o gün bir kez yayılır. Gün ayarlarda
    # saklanır, böylece yeniden başlatma aynı gün ikinci kez yaymaz.
    def _check_daily_loss(self, st) -> None:
        guard = self.gate.guard
        if guard is None or not guard.breached(st):
            return
        key, day = f"loss_notified:{self.pid}", guard.day_start()
        if self.state.setting(key) != day:
            self.state.set_setting(key, day)
            self.bus.publish({"profile": self.pid, "daily_loss": guard.daily_loss})

    async def _process(self) -> None:
        try:
            async with self.lock:
                rows = self.state.wakes(self.pid)
                found = [Wake(kind, t, wake_codec.decode(data)) for _, t, kind, data in rows]
                await self._decide(found, [r[0] for r in rows], 0)
        except Exception:
            log.exception("[%s] Karar döngüsünde beklenmeyen hata.", self.profile.short)
            self.retry_at = self.clock.time() + self.retry_s
        finally:
            self.busy = False

    async def wait_idle(self) -> None:
        if self._side_tasks:
            await asyncio.gather(*self._side_tasks, return_exceptions=True)
        if self._task and not self._task.done():
            await asyncio.gather(self._task, return_exceptions=True)

    # --- Karar ---

    def _context(self, wakes: list[Wake], now: int, admin: dict) -> Context:
        trades = self.ledger.trades(self.broker.account_id)
        nets = [t.net for t in trades]
        state = self.broker.state()
        found = (facts_.build(self.ledger, self.journal, self.notes, self.pid, state, self.version.created_at, now,
                              describe) if self.notes else Facts())
        return Context(now=now, wakes=wakes, state=state, prices=dict(self.prices),
                       marks=dict(self.marks), symbols=self.symbols, market=self.market,
                       memory=self.journal.memory(self.pid),
                       history=self.journal.recent(self.pid, self.history),
                       admin=admin,
                       alerts=self.alerts.alerts,
                       stats={"count": len(nets), "wins": sum(1 for n in nets if n > 0), "net": sum(nets),
                              "fee": sum(t.fee for t in trades)},
                       start_balance=self.ledger.start_balance(self.broker.account_id), facts=found,
                       on_tool=lambda t: self.bus.publish({"profile": self.pid, "tool": t}))

    # Girişin notu, komutun olaylarından emrin ya da pozisyonun kimliğini
    # alır. Bekleyen girişte ORDER_PLACED, piyasa girişinde OPENED ya da
    # ADDED olayıdır. Not günlük satırı yazıldıktan sonra kimliğiyle depoya
    # girer, o yüzden burada yalnızca toplanır.
    @staticmethod
    def _noted(note: dict, events: list[AccountEvent]) -> dict | None:
        for e in events:
            if e.kind is EventKind.ORDER_PLACED and not e.reduce and e.order_id:
                return {**note, "order_id": e.order_id}
            if e.kind in (EventKind.OPENED, EventKind.ADDED) and e.position_id:
                return {**note, "position_id": e.position_id}
        return None

    # Rejimi uygun bulunmayan paritede beklenen fiyata sistemin kurduğu alarm.
    # Aynı paritenin eski beklenti alarmı yenisinden önce iptal edilir, Opus
    # yalnızca son beklediği fiyatta çağrılır.
    def _expect(self, op: dict, now: int) -> str:
        for old in [a for a in self.alerts.alerts if a.purpose == "expectation" and a.symbol == op["symbol"]]:
            self.alerts.cancel(old.number)
            self.state.resolve_alert(self.pid, old.number, "cancelled", now)
        a = self.alerts.add(op["symbol"], op["price"], self.prices[op["symbol"]], op["note"], str(Actor.SYSTEM), now,
                            purpose="expectation")
        self.state.add_alert(self.pid, a)
        return f"beklenen fiyata alarm #{a.number} {a.symbol} {numbers.price(a.price)} kuruldu."

    async def _apply(self, d: Decision, now: int) -> tuple[list[ActionResult], list[dict]]:
        results = d.errors + d.notes
        noted: list[dict] = []
        failed = False
        for i, (cmd, label) in enumerate(zip(d.commands, d.labels)):
            if failed and i < len(d.chained) and d.chained[i]:
                results.append(ActionResult(label, False, f"{label} uygulanmadı, aynı eylemin önceki adımı "
                                                          f"reddedildi."))
                continue
            try:
                events = await self.gate.submit(cmd, Actor.STRATEGY, now)
                results.append(ActionResult(label, True, describe(events)))
                failed = False
                note = d.annotations[i] if i < len(d.annotations) else None
                if note and (found := self._noted(note, events)):
                    noted.append(found)
            except RuleError as e:
                results.append(ActionResult(label, False, f"{label} reddedildi: {e}"))
                failed = True
        for op in d.alarms:
            label = op.get("label", "alarm")
            try:
                if op["op"] == "expect":
                    if op["symbol"] not in self.prices:
                        raise ValueError(f"{op['symbol']} fiyatı henüz gelmedi.")
                    results.append(ActionResult(label, True, self._expect(op, now)))
                elif op["op"] == "add":
                    if op["symbol"] not in self.prices:
                        raise ValueError(f"{op['symbol']} fiyatı henüz gelmedi.")
                    a = self.alerts.add(op["symbol"], op["price"], self.prices[op["symbol"]], op["note"],
                                        str(Actor.STRATEGY), now)
                    self.state.add_alert(self.pid, a)
                    results.append(ActionResult(label, True,
                                                f"alarm #{a.number} {a.symbol} {numbers.price(a.price)} kuruldu."))
                else:
                    a = self.alerts.cancel(op["number"])
                    self.state.resolve_alert(self.pid, a.number, "cancelled", now)
                    results.append(ActionResult(label, True, f"alarm #{a.number} iptal edildi."))
            except ValueError as e:
                results.append(ActionResult(label, False, f"{label} reddedildi: {e}"))
        return results, noted

    # Notlar ve sınıflandırmalar günlük satırının kimliğiyle yazılır, panel
    # bir girişin hangi kararla verildiğini buradan bulur.
    def _remember(self, d: Decision, noted: list[dict], jid: int, now: int) -> None:
        if not self.notes:
            return
        for n in noted:
            self.notes.add_entry(self.pid, self.broker.account_id, n["symbol"], n["side"], n["reason"],
                                 n["invalid_price"], n["invalid_text"], now, jid, n.get("order_id"),
                                 n.get("position_id"))
        for r in d.reviews:
            self.notes.add_review(r["position_id"], self.pid, jid, r["verdict"], r["text"], now)

    async def _decide(self, wakes: list[Wake], ids: list[int], corrections: int) -> None:
        now = self.clock.time()
        stored = [{"kind": w.kind, "time": w.time, "data": wake_codec.encode(w.data) if w.kind != "correction"
                   else None} for w in wakes]
        admin, shown = self.feed() if self.strategy.accepts_messages else ({}, [])
        try:
            d = await self.strategy.decide(self._context(wakes, now, admin))
        except Exception as e:
            text = str(e) or type(e).__name__
            limited = self.access.report_error(text)
            self.retry_at = now + self.retry_s
            record = {"tools": e.tools} if getattr(e, "tools", None) else {}
            self.journal.add(self.pid, self.version.id, now, stored, [], record, text, None)
            self.state.system_event(self.pid, now, "decide_error", "Karar alınamadı.",
                                    {"error": text, "limit": limited})
            self.bus.publish({"profile": self.pid, "decide_error": text, "limit": limited})
            log.warning("[%s] Karar alınamadı: %s", self.profile.short, text)
            return
        self.state.drop_wakes(ids)
        self.delivered(shown)
        can_correct = self.strategy.corrects and corrections < self.max_corrections
        left = self.max_corrections - corrections - 1
        if d.suggestion:
            self.bus.publish({"profile": self.pid, "suggestion": d.suggestion})
        if d.rejection:
            await self._rejected(d, now, stored, corrections, can_correct, left)
            return
        results, noted = await self._apply(d, now)
        if d.memory:
            self.journal.set_memory(self.pid, d.memory)
        self.next_wake = now + d.wait_s if d.wait_s else None
        jid = self.journal.add(self.pid, self.version.id, now, stored,
                               [{"label": r.label, "ok": r.ok, "message": r.message} for r in results],
                               d.record, None, self.next_wake)
        self._remember(d, noted, jid, now)
        self.bus.publish({"profile": self.pid, "journal": jid})
        if can_correct and any(not r.ok for r in results):
            await self._decide([Wake("correction", now, {"rejected": False, "results": results, "left": left})], [],
                               corrections + 1)

    # Kontrol hatasıyla reddedilen karar hiçbir komut uygulamaz, hafızayı
    # yazmaz. Günlüğe reddin nedenleriyle girer. Hak varsa hemen düzeltme
    # istenir. Hak bitince yöneticiye bildirilir ve bir sonraki çağrı
    # stratejinin verdiği beklemeyle olur. Düzeltme çağrısı da hata verirse
    # profil bu beklemeyle yine uyanır.
    async def _rejected(self, d: Decision, now: int, stored: list[dict], corrections: int, can_correct: bool,
                        left: int) -> None:
        results = [ActionResult("kontrol", False, e) for e in d.rejection]
        self.next_wake = now + d.wait_s if d.wait_s else None
        jid = self.journal.add(self.pid, self.version.id, now, stored,
                               [{"label": r.label, "ok": r.ok, "message": r.message} for r in results],
                               d.record, None, self.next_wake)
        self.bus.publish({"profile": self.pid, "journal": jid})
        if can_correct:
            await self._decide([Wake("correction", now, {"rejected": True, "results": results, "left": left})], [],
                               corrections + 1)
            return
        self.state.system_event(self.pid, now, "decision_rejected", "Karar düzeltme hakkı bittiği için uygulanmadı.",
                                {"errors": d.rejection, "journal": jid})
        self.bus.publish({"profile": self.pid, "decision_rejected": d.rejection, "journal_id": jid})
