"""
file: runner.py
importable: ProfileRunner, describe, FUNDING_EVERY_S

ProfileRunner tek bir profili yürütür. Canlı mumları ve likidasyonları
broker'a ve tetikleyicilere verir, uyandırmaları kalıcı bir sırada biriktirir
ve tick() her saniye çağrıldıkça sıradakileri tek bir kararda stratejiye
verir. Kararın komutlarını kapıdan geçirir, alarm işlemlerini uygular,
hafızayı ve günlüğü yazar. Reddedilen bir komut varsa ve strateji isterse
hemen düzeltme için yeniden uyandırır. describe() bir komutun olaylarını
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
from typing import Callable

from ..broker.base import Broker, PriceDriven
from ..domain.clock import Clock
from ..domain.errors import RuleError
from ..domain.profile import Profile, State, Version
from ..domain.trading import AccountEvent, Actor, EventKind
from ..market.models import Candle, Liquidation
from ..market.source import MarketData, MarketDataError
from ..storage.journal import JournalRepo
from ..storage.ledger import Ledger
from ..storage.runtime_state import StateRepo
from ..strategy.base import ActionResult, Context, Decision, Strategy, Wake
from ..triggers.base import Fire, Trigger
from ..triggers.price_alert import PriceAlerts
from . import wakes as wake_codec
from .bus import EventBus
from .gate import Gate
from .model_access import ModelAccess

log = logging.getLogger(__name__)

# Stratejiyi uyandıran hesap olayları. Fonlama ödemesi uyandırmaz, bir sonraki
# kararda hesabın halinde görünür.
_WAKING = (EventKind.CLOSED, EventKind.ORDER_FILLED, EventKind.ORDER_FAILED, EventKind.ORDER_EXPIRED)

# Fonlamaların Binance'ten kontrol edilme aralığı, saniye. Kaçırılan bir
# fonlama bir sonraki kontrolde uygulanır.
FUNDING_EVERY_S = 300


def describe(events: list[AccountEvent]) -> str:
    """Bir komutun olaylarını kısa bir Türkçe sonuca çevirir."""
    parts = []
    for e in events:
        head = f"#{e.number} {e.symbol} {e.side}"
        match e.kind:
            case EventKind.OPENED:
                parts.append(f"{head} açıldı, giriş {e.price:g}.")
            case EventKind.ADDED:
                parts.append(f"{head} pozisyonuna ekleme yapıldı, ortalama giriş {e.price:g}.")
            case EventKind.CLOSED:
                parts.append(f"{head} pozisyonundan {e.trade.qty:g} birim {e.price:g} fiyatından kapandı, net "
                             f"{e.trade.net:+.2f} dolar.")
            case EventKind.PROTECTION:
                parts.append(f"{head} pozisyonunun koruması güncellendi.")
            case EventKind.ORDER_PLACED:
                parts.append(f"{head} emri kaydedildi, tetik {e.price:g}.")
            case EventKind.ORDER_CANCELLED:
                parts.append(f"{head} emri iptal edildi.")
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
    """

    def __init__(self, profile: Profile, version: Version, strategy: Strategy, broker: Broker, gate: Gate,
                 triggers: list[Trigger], alerts: PriceAlerts, ledger: Ledger, journal: JournalRepo,
                 state: StateRepo, market: MarketData, clock: Clock, bus: EventBus, access: ModelAccess,
                 lock: asyncio.Lock, history: int = 8, retry_s: int = 600, max_corrections: int = 2,
                 feed: Callable[[], tuple[dict, list[int]]] | None = None,
                 delivered: Callable[[list[int]], None] | None = None) -> None:
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
        self.prices: dict[str, float] = {}
        self.busy = False
        self.retiring = False
        self.retry_at = 0
        self.next_wake = self._last_next_wake()
        self._last_minute = 0
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

    def _account(self, events: list[AccountEvent]) -> None:
        for e in events:
            self.bus.publish(e)
            if e.kind in _WAKING:
                self.wake("account", e)

    # --- Piyasa ---

    def on_candle(self, candle: Candle, closed: bool) -> None:
        if candle.symbol not in self.symbols:
            return
        self.prices[candle.symbol] = candle.close
        if isinstance(self.broker, PriceDriven):
            self._account(self.broker.on_price(candle.symbol, candle.high, candle.low, candle.close,
                                               self.clock.time()))
        for t in [*self.triggers, self.alerts]:
            self._fire(t.on_candle(candle))
        if closed:
            self.state.set_setting(f"price_seen:{self.pid}:{candle.symbol}", candle.time)

    def on_liquidation(self, liq: Liquidation) -> None:
        for t in self.triggers:
            self._fire(t.on_liquidation(liq))

    # Program kapalıyken ya da akış kopukken geçen dakikalar REST'ten çekilip
    # sırayla işlenir. Kaçan bir dakikadaki SL ya da TP böylece kaybolmaz.
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
            for c in candles:
                self._account(self.broker.on_price(s, c.high, c.low, c.close, c.time + 59))
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

    # --- Zaman ---

    def tick(self, now: int) -> None:
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
        if self._task and not self._task.done():
            await asyncio.gather(self._task, return_exceptions=True)

    # --- Karar ---

    def _context(self, wakes: list[Wake], now: int, admin: dict) -> Context:
        trades = self.ledger.trades(self.broker.account_id)
        nets = [t.net for t in trades]
        return Context(now=now, wakes=wakes, state=self.broker.state(), prices=dict(self.prices),
                       symbols=self.symbols, market=self.market, memory=self.journal.memory(self.pid),
                       history=self.journal.recent(self.pid, self.history),
                       admin=admin,
                       alerts=self.alerts.alerts,
                       stats={"count": len(nets), "wins": sum(1 for n in nets if n > 0), "net": sum(nets),
                              "fee": sum(t.fee for t in trades)},
                       start_balance=self.ledger.start_balance(self.broker.account_id),
                       on_tool=lambda t: self.bus.publish({"profile": self.pid, "tool": t}))

    async def _apply(self, d: Decision, now: int) -> list[ActionResult]:
        results = list(d.errors)
        for cmd, label in zip(d.commands, d.labels):
            try:
                results.append(ActionResult(label, True, describe(await self.gate.submit(cmd, Actor.STRATEGY, now))))
            except RuleError as e:
                results.append(ActionResult(label, False, f"{label} reddedildi: {e}"))
        for op in d.alarms:
            label = op.get("label", "alarm")
            try:
                if op["op"] == "add":
                    if op["symbol"] not in self.prices:
                        raise ValueError(f"{op['symbol']} fiyatı henüz gelmedi.")
                    a = self.alerts.add(op["symbol"], op["price"], self.prices[op["symbol"]], op["note"],
                                        str(Actor.STRATEGY), now)
                    self.state.add_alert(self.pid, a)
                    results.append(ActionResult(label, True, f"alarm #{a.number} {a.symbol} {a.price:g} kuruldu."))
                else:
                    a = self.alerts.cancel(op["number"])
                    self.state.resolve_alert(self.pid, a.number, "cancelled", now)
                    results.append(ActionResult(label, True, f"alarm #{a.number} iptal edildi."))
            except ValueError as e:
                results.append(ActionResult(label, False, f"{label} reddedildi: {e}"))
        return results

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
        results = await self._apply(d, now)
        if d.memory:
            self.journal.set_memory(self.pid, d.memory)
        self.next_wake = now + d.wait_s if d.wait_s else None
        jid = self.journal.add(self.pid, self.version.id, now, stored,
                               [{"label": r.label, "ok": r.ok, "message": r.message} for r in results],
                               d.record, None, self.next_wake)
        self.bus.publish({"profile": self.pid, "journal": jid})
        if self.strategy.corrects and corrections < self.max_corrections and any(not r.ok for r in results):
            await self._decide([Wake("correction", now, results)], [], corrections + 1)
