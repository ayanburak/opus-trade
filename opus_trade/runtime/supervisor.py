"""
file: supervisor.py
importable: Supervisor

Supervisor bütün profillerin çalıştırıcılarını yönetir. refresh() bir
profilin çalıştırıcısını veritabanındaki son haline göre kurar, değiştirir
ya da kaldırır, program yeniden başlamadan. run() canlı akışı, saniyelik
zamanlayıcıyı ve saatlik izleme listesi yenilemesini çalıştırır. Akıştan
gelen mumları, tekil işlemleri ve likidasyonları ilgili çalıştırıcılara
dağıtır, bağlantı her kurulduğunda kopukken kaçan dakikaları işletir. Akışın
aboneliği çalıştırıcıların dinlemek istediği paritelerin birleşimidir. check_stream() iki dakikadan uzun mum gelmezse akışın koptuğunu olay
yoluna yayar.

description: Profillerin çalıştırıcılarını kuran ve canlı veriyi onlara dağıtan yönetici.
"""

# NOTE: Çalıştırıcı taslak ve arşivlenmiş profiller dışında her profil için
# kurulur. Duraklatılmış ya da durdurulmuş bir profilin açık pozisyonları da
# fiyatla işlemeye devam etmelidir.
#
# NOTE: Bir profil yeni sürüme ya da duruma geçince eski çalıştırıcı bırakılır,
# yenisi kurulur ve son fiyatlar ona aktarılır. Eski çalıştırıcının süren bir
# kararı varsa değişiklik o karar bitince uygulanır: eski çalıştırıcı o sırada
# canlı fiyat almaya devam eder ama yeni karar başlatmaz, gelen uyandırmalar
# veritabanında bekler. Yeni çalıştırıcı kararın seçtiği sonraki çağrı
# saatini günlükten okur. Bekleme sırasında gelen başka değişiklikler tek
# seferde, profilin son haline göre uygulanır.
#
# NOTE: Bir çalıştırıcının hatası akışa ve diğer profillere sızmaz. Hata ilk
# seferinde tam izle yazılır, profil düzelince bu da yazılır.

import asyncio
import logging
from typing import Callable

from ..broker.paper import PaperBroker
from ..domain.catalog import Catalog
from ..domain.clock import Clock
from ..domain.fees import FeeSchedule
from ..domain.profile import Area, Profile, State, Universe
from ..market.flow import TradeFlow
from ..market.models import Candle, Liquidation, Trade
from ..market.screen import MarketScreen
from ..market.source import MarketData, MarketDataError
from ..market.stream import Feed, MarketStream, StreamListener
from ..registry import Entry
from ..storage.journal import JournalRepo
from ..storage.ledger import Ledger
from ..storage.notes import NotesRepo
from ..storage.profiles import ProfileRepo
from ..storage.runtime_state import StateRepo
from ..strategy.base import STRATEGIES, Deps
from ..triggers.base import TRIGGERS, Setup
from ..triggers.price_alert import PriceAlerts
from .bus import EventBus
from .gate import Gate
from .limits import MaxLeverage, MaxNotional
from .model_access import ModelAccess
from .risk import RiskGuard
from .runner import FUNDING_EVERY_S, ProfileRunner

log = logging.getLogger(__name__)

# Akışın kopmuş sayılması için mumsuz geçen en uzun süre, saniye.
STREAM_SILENCE_S = 120


class Supervisor(StreamListener):
    """Profillerin çalıştırıcıları.

    Args:
        repo: Profil deposu.
        ledger: Hesap defteri.
        journal: Günlük ve hafıza deposu.
        state: Alarmların, bekleyen uyandırmaların ve ayarların deposu.
        market: Piyasa verisi.
        stream: Canlı akış.
        clock: Saat.
        bus: Olay yolu.
        access: Model erişimi.
        catalog: Parite kuralları.
        fees: Kâğıt hesapların komisyon oranları.
        deps: Bir profil için strateji bağımlılıklarını kuran fonksiyon.
        notes: Giriş notları ve sınıflandırmalar, verilmezse defterin bağlantısıyla kurulur.
        screen: İzleme listelerinin kaynağı, verilirse saat başı yenilenir.
        flow: Tekil işlemlerin saniyelik özeti, bütün profillerin ortak kopyası.

    interventions kurulumdan sonra verilir, çünkü müdahaleler de çalıştırıcılara
    Supervisor üzerinden ulaşır.
    """

    def __init__(self, repo: ProfileRepo, ledger: Ledger, journal: JournalRepo, state: StateRepo,
                 market: MarketData, stream: MarketStream, clock: Clock, bus: EventBus, access: ModelAccess,
                 catalog: Catalog, fees: FeeSchedule, deps: Callable[[Profile], Deps],
                 notes: NotesRepo | None = None, screen: MarketScreen | None = None,
                 flow: TradeFlow | None = None) -> None:
        self.repo = repo
        self.ledger = ledger
        self.journal = journal
        self.state = state
        self.market = market
        self.stream = stream
        self.clock = clock
        self.bus = bus
        self.access = access
        self.catalog = catalog
        self.fees = fees
        self.deps = deps
        self.notes = notes or NotesRepo(ledger.con)
        self.runners: dict[int, ProfileRunner] = {}
        self.interventions = None
        self._locks: dict[int, asyncio.Lock] = {}
        self._failing: set[int] = set()
        self._background: set[asyncio.Task] = set()
        self._deferred: set[int] = set()
        self.last_candle = clock.time()
        self._candle_sent: dict[str, int] = {}
        self.watched: set[str] = set()
        self.screen = screen
        self.flow = flow or TradeFlow()
        self._by_trade: dict[str, list[int]] = {}
        self._sent: dict[str, tuple[Feed, ...]] | None = None
        self._pending: dict[str, tuple[Feed, ...]] | None = None
        self._pusher: asyncio.Task | None = None
        self.stream_down = False

    # --- Kurulum ---

    def _build(self, p: Profile) -> ProfileRunner:
        v = self.repo.version(p.version_id)
        cfg = v.config
        entry: Entry = STRATEGIES.get(cfg.strategy)
        strategy = entry.build(self.deps(p), **cfg.params)
        if p.area is not Area.PAPER:
            raise NotImplementedError("Canlı broker henüz yok.")
        broker = PaperBroker(self.ledger, p.account_id, self.catalog, self.fees, self.clock)
        limits = []
        if "max_leverage" in cfg.risk:
            limits.append(MaxLeverage(cfg.risk["max_leverage"]))
        if "max_notional" in cfg.risk:
            limits.append(MaxNotional(cfg.risk["max_notional"]))
        guard = RiskGuard(self.ledger, self.clock, limits, cfg.risk.get("daily_loss"))
        iv = self.interventions
        gate = Gate(broker, self.bus, self.catalog, guard, iv.restriction_source(p.id) if iv else None)
        universe = tuple(self.catalog.symbols) if cfg.universe is Universe.MARKET else None
        setup = Setup(cfg.symbols, frozenset(universe or cfg.symbols), self.screen, self.flow)
        triggers = []
        for t in cfg.triggers:
            spec = dict(t)
            triggers.append(TRIGGERS.get(spec.pop("type")).build(setup, **spec))
        alerts = PriceAlerts()
        alerts.load(self.state.alerts(p.id))
        lock = self._locks.setdefault(p.id, asyncio.Lock())
        return ProfileRunner(p, v, strategy, broker, gate, triggers, alerts, self.ledger, self.journal, self.state,
                             self.market, self.clock, self.bus, self.access, lock,
                             feed=(lambda: iv.feed(p.id)) if iv else None, delivered=iv.delivered if iv else None,
                             notes=self.notes, universe=universe, on_wants=self._watch)

    def refresh(self, profile_id: int) -> None:
        """Bir profilin çalıştırıcısını son haline göre kurar, değiştirir ya da kaldırır. Süren bir karar varsa
        değişiklik karar bitince uygulanır."""
        old = self.runners.get(profile_id)
        if old and old.busy:
            old.retiring = True
            if profile_id not in self._deferred:
                self._deferred.add(profile_id)
                task = asyncio.get_running_loop().create_task(self._refresh_after(profile_id, old))
                self._background.add(task)
                task.add_done_callback(self._background.discard)
            return
        self._replace(profile_id)

    async def _refresh_after(self, profile_id: int, old: ProfileRunner) -> None:
        await old.wait_idle()
        self._deferred.discard(profile_id)
        try:
            self._replace(profile_id)
        except Exception:
            log.exception("[%s] Profil yeniden kurulamadı.", old.profile.short)

    def _replace(self, profile_id: int) -> None:
        p = self.repo.get(profile_id)
        old = self.runners.pop(profile_id, None)
        if p.state in (State.DRAFT, State.ARCHIVED) or p.account_id is None:
            self._watch()
            return
        r = self._build(p)
        if old:
            r.prices = dict(old.prices)
            if isinstance(r.broker, PaperBroker) and isinstance(old.broker, PaperBroker):
                r.broker.prices = dict(old.broker.prices)
        self.runners[profile_id] = r
        if old and old.version.id != r.version.id:
            self.bus.publish({"profile": profile_id, "version": r.version.number})
        r.start(old.profile.state if old else None)
        self._watch()

    # Akışın isteği çalıştırıcıların isteklerinin birleşimidir. Mark fiyatı
    # bütün piyasadan tek akışla gelir, burada istenmez. Tekil işlemler
    # pariteye göre yalnızca onu isteyen çalıştırıcılara dağıtılır, dizin
    # burada kurulur.
    def _wants(self) -> dict[str, tuple[Feed, ...]]:
        merged: dict[str, set[Feed]] = {}
        by_trade: dict[str, list[int]] = {}
        for pid, r in self.runners.items():
            for sym, feeds in r.wants.items():
                merged.setdefault(sym, set()).update(f for f in feeds if f is not Feed.MARK)
                if Feed.TRADE in feeds:
                    by_trade.setdefault(sym, []).append(pid)
        self.watched, self._by_trade = set(merged), by_trade
        self.flow.forget(set(by_trade))
        return {sym: tuple(sorted(f)) for sym, f in merged.items()}

    # İstek değiştikçe akışa gider. Abonelik mesajları sırayla gitmeli, bu
    # yüzden tek bir görev bekleyen son isteği gönderir, arada gelen istekler
    # birbirinin yerine geçer. asyncio görevlere yalnızca zayıf referans tutar,
    # görev _pusher'da saklanır. Döngü yokken, örneğin testlerde, abonelik
    # atlanır.
    def _watch(self) -> None:
        wants = self._wants()
        if wants == self._sent:
            self._pending = None
            return
        self._pending = wants
        if self._pusher is not None and not self._pusher.done():
            return
        try:
            self._pusher = asyncio.get_running_loop().create_task(self._push())
        except RuntimeError:
            return

    async def _push(self) -> None:
        while self._pending is not None:
            wants, self._pending = self._pending, None
            try:
                await self.stream.watch(wants)
                self._sent = wants
            except Exception:
                log.exception("Akışın aboneliği değiştirilemedi.")

    # --- Akış ---

    def _guard(self, pid: int, job: Callable[[], None]) -> None:
        try:
            job()
        except Exception:
            if pid not in self._failing:
                self._failing.add(pid)
                log.exception("[%s] Canlı veri işlenirken hata.", self.runners[pid].profile.short)
            return
        if pid in self._failing:
            self._failing.discard(pid)
            log.info("[%s] Canlı veri yeniden hatasız işleniyor.", self.runners[pid].profile.short)

    async def on_connect(self) -> None:
        for r in list(self.runners.values()):
            try:
                await r.catch_up()
            except Exception:
                log.exception("[%s] Kopukken geçen dakikalar işlenemedi.", r.profile.short)

    # Panelin grafiği canlı fiyatı akıştaki candle olayından alır. Binance
    # dakikalık mumu saniyede birkaç kez günceller, akışa parite başına en
    # fazla saniyede bir güncelleme gider. Kapanan mum her zaman gider.
    async def on_candle(self, candle: Candle, closed: bool) -> None:
        now = self.clock.time()
        if self.stream_down:
            self.stream_down = False
            self.bus.publish({"stream": "up", "silent_s": now - self.last_candle})
        self.last_candle = now
        if closed or self._candle_sent.get(candle.symbol) != now:
            self._candle_sent[candle.symbol] = now
            self.bus.publish({"candle": candle, "closed": closed})
        for pid, r in list(self.runners.items()):
            self._guard(pid, lambda r=r: r.on_candle(candle, closed))

    # Mark fiyatı bütün piyasadan gelir, saniyede yüzlerce parite. Hiçbir
    # çalıştırıcının dinlemediği parite çalıştırıcılara hiç gitmez.
    async def on_mark(self, symbol: str, mark: float, time: int) -> None:
        if symbol not in self.watched:
            return
        for pid, r in list(self.runners.items()):
            self._guard(pid, lambda r=r: r.on_mark(symbol, mark, time))

    # İşlem önce ortak özete girer, tetikleyiciler kendi kararlarını o işlemi
    # de sayan özetle verir.
    async def on_trade(self, trade: Trade) -> None:
        pids = self._by_trade.get(trade.symbol)
        if not pids:
            return
        self.flow.add(trade)
        for pid in pids:
            r = self.runners.get(pid)
            if r is not None:
                self._guard(pid, lambda r=r: r.on_trade(trade))

    async def on_liquidation(self, liq: Liquidation) -> None:
        for pid, r in list(self.runners.items()):
            self._guard(pid, lambda r=r: r.on_liquidation(liq))

    # --- Döngüler ---

    # Dinlenen bir parite varken STREAM_SILENCE_S boyunca hiç mum gelmezse akış
    # kopmuş sayılır ve bu bir kez yayılır. Mum gelince akışın döndüğü yayılır.
    def check_stream(self, now: int) -> None:
        if self.runners and not self.stream_down and now - self.last_candle > STREAM_SILENCE_S:
            self.stream_down = True
            self.bus.publish({"stream": "down", "since": self.last_candle})

    async def _ticker(self) -> None:
        while True:
            now = self.clock.time()
            self.check_stream(now)
            for pid, r in list(self.runners.items()):
                self._guard(pid, lambda r=r: r.tick(now))
            await asyncio.sleep(1)

    async def _funding(self) -> None:
        while True:
            for r in list(self.runners.values()):
                try:
                    await r.apply_funding()
                except Exception:
                    log.exception("[%s] Fonlama uygulanamadı.", r.profile.short)
            await asyncio.sleep(FUNDING_EVERY_S)

    # Özet saat başını beş saniye geçe yenilenir, Binance'in saatlik verisi
    # o ana kadar oturmuş olur. Açılışta hemen bir kez yenilenir. Hata
    # olursa bir dakika sonra yeniden denenir, eski liste yerinde kalır.
    async def _screening(self) -> None:
        while True:
            try:
                await self.screen.refresh()
                wait = 3600 - self.clock.time() % 3600 + 5
            except (MarketDataError, NotImplementedError) as e:
                log.warning("İzleme listesinin özeti alınamadı: %s", e)
                wait = 60
            await asyncio.sleep(wait)

    async def run(self) -> None:
        """Bütün profilleri kurar ve iptal edilene kadar akışı ve zamanlayıcıları çalıştırır."""
        for p in self.repo.all():
            try:
                self.refresh(p.id)
            except Exception:
                log.exception("[%s] Profil kurulamadı.", p.short)
        log.info("%d profil kuruldu.", len(self.runners))
        self._sent = self._wants()
        await self.stream.watch(self._sent)
        self.last_candle = self.clock.time()
        tasks = [asyncio.create_task(self._ticker()), asyncio.create_task(self._funding())]
        if self.screen is not None:
            tasks.append(asyncio.create_task(self._screening()))
        try:
            await self.stream.run(self)
        finally:
            for t in tasks:
                t.cancel()
            for r in self.runners.values():
                await r.wait_idle()
