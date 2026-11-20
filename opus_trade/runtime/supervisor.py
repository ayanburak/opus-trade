"""
file: supervisor.py
importable: Supervisor

Supervisor bütün profillerin çalıştırıcılarını yönetir. refresh() bir
profilin çalıştırıcısını veritabanındaki son haline göre kurar, değiştirir
ya da kaldırır, program yeniden başlamadan. run() canlı akışı ve saniyelik
zamanlayıcıyı çalıştırır. Akıştan gelen mumları ve likidasyonları ilgili
çalıştırıcılara dağıtır, bağlantı her kurulduğunda kopukken kaçan dakikaları
işletir.

description: Profillerin çalıştırıcılarını kuran ve canlı veriyi onlara dağıtan yönetici.
"""

# NOTE: Çalıştırıcı taslak ve arşivlenmiş profiller dışında her profil için
# kurulur. Duraklatılmış ya da durdurulmuş bir profilin açık pozisyonları da
# fiyatla işlemeye devam etmelidir.
#
# NOTE: Bir profil yeni sürüme geçince eski çalıştırıcı bırakılır, yenisi aynı
# karar kilidiyle kurulur. Eski çalıştırıcının süren bir kararı varsa yenisi
# kilit açılana kadar bekler. Son fiyatlar yeni çalıştırıcıya aktarılır.
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
from ..domain.profile import Area, Profile, State
from ..market.models import Candle, Liquidation
from ..market.source import MarketData
from ..market.stream import MarketStream, StreamListener
from ..registry import Entry
from ..storage.journal import JournalRepo
from ..storage.ledger import Ledger
from ..storage.profiles import ProfileRepo
from ..storage.runtime_state import StateRepo
from ..strategy.base import STRATEGIES, Deps
from ..triggers.base import TRIGGERS
from ..triggers.price_alert import PriceAlerts
from .bus import EventBus
from .gate import Gate
from .limits import MaxLeverage, MaxNotional
from .model_access import ModelAccess
from .risk import RiskGuard
from .runner import FUNDING_EVERY_S, ProfileRunner

log = logging.getLogger(__name__)


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

    interventions kurulumdan sonra verilir, çünkü müdahaleler de çalıştırıcılara
    Supervisor üzerinden ulaşır.
    """

    def __init__(self, repo: ProfileRepo, ledger: Ledger, journal: JournalRepo, state: StateRepo,
                 market: MarketData, stream: MarketStream, clock: Clock, bus: EventBus, access: ModelAccess,
                 catalog: Catalog, fees: FeeSchedule, deps: Callable[[Profile], Deps]) -> None:
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
        self.runners: dict[int, ProfileRunner] = {}
        self.interventions = None
        self._locks: dict[int, asyncio.Lock] = {}
        self._failing: set[int] = set()
        self._background: set[asyncio.Task] = set()

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
        gate = Gate(broker, self.bus, guard, iv.restriction_source(p.id) if iv else None)
        triggers = []
        for t in cfg.triggers:
            spec = dict(t)
            triggers.append(TRIGGERS.get(spec.pop("type")).build(cfg.symbols, **spec))
        alerts = PriceAlerts()
        alerts.load(self.state.alerts(p.id))
        lock = self._locks.setdefault(p.id, asyncio.Lock())
        return ProfileRunner(p, v, strategy, broker, gate, triggers, alerts, self.ledger, self.journal, self.state,
                             self.market, self.clock, self.bus, self.access, lock,
                             feed=(lambda: iv.feed(p.id)) if iv else None, delivered=iv.delivered if iv else None)

    def refresh(self, profile_id: int) -> None:
        """Bir profilin çalıştırıcısını son haline göre kurar, değiştirir ya da kaldırır."""
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
        r.start(old.profile.state if old else None)
        self._watch()

    # Akışın dinlediği pariteler çalışan bütün çalıştırıcıların paritelerinin
    # birleşimidir. asyncio görevlere yalnızca zayıf referans tutar, referansı
    # saklanmayan bir görev çalışmadan çöpe gidebilir, bu yüzden görev bitene
    # kadar _background'da durur. Döngü yokken, örneğin testlerde, abonelik atlanır.
    def _watch(self) -> None:
        symbols = {s for r in self.runners.values() for s in r.symbols}
        try:
            task = asyncio.get_running_loop().create_task(self.stream.watch(symbols))
        except RuntimeError:
            return
        self._background.add(task)
        task.add_done_callback(self._background.discard)

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

    async def on_candle(self, candle: Candle, closed: bool) -> None:
        for pid, r in list(self.runners.items()):
            self._guard(pid, lambda r=r: r.on_candle(candle, closed))

    async def on_liquidation(self, liq: Liquidation) -> None:
        for pid, r in list(self.runners.items()):
            self._guard(pid, lambda r=r: r.on_liquidation(liq))

    # --- Döngüler ---

    async def _ticker(self) -> None:
        while True:
            now = self.clock.time()
            if self.interventions:
                try:
                    self.interventions.tick(now)
                except Exception:
                    log.exception("Planlı yönetici çağrıları işlenemedi.")
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

    async def run(self) -> None:
        """Bütün profilleri kurar ve iptal edilene kadar akışı ve zamanlayıcıları çalıştırır."""
        for p in self.repo.all():
            try:
                self.refresh(p.id)
            except Exception:
                log.exception("[%s] Profil kurulamadı.", p.short)
        await self.stream.watch({s for r in self.runners.values() for s in r.symbols})
        tasks = [asyncio.create_task(self._ticker()), asyncio.create_task(self._funding())]
        try:
            await self.stream.run(self)
        finally:
            for t in tasks:
                t.cancel()
            for r in self.runners.values():
                await r.wait_idle()
