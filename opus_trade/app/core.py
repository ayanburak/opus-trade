"""
file: core.py
importable: assemble

Çekirdek servisinin giriş noktası. OPUS_STATE klasöründeki core.db'yi açar,
eksik taşımaları uygular, profilleri kurar ve canlı akışla birlikte kontrol
API'sini OPUS_CORE_SOCKET soketinde çalıştırır. SIGTERM ya da Ctrl+C gelince
süren kararların bitmesini bekleyip kapanır.

    python -m opus_trade.app.core

assemble() parçaları birbirine bağlar ve testlerin de kullandığı Core'u
döndürür.

description: Çekirdeğin kuruluş noktası.
"""

# NOTE: Somut parçalar yalnızca burada seçilir: kâğıt broker, Binance piyasa
# verisi ve akışı, claude komutuyla çalışan model istemcisi. Kodun geri
# kalanı yalnızca soyut sınıflarını bilir.
#
# NOTE: Piyasa araçları sunucusu claude komutunun alt sürecidir ve aynı
# Python yorumlayıcısıyla, proje kökü PYTHONPATH'te olacak şekilde başlar.

import asyncio
import logging
import signal
import sys
from pathlib import Path

import httpx
import uvicorn

from .. import plugins
from ..api.control import Core, create_app
from ..config import AppConfig
from ..domain.catalog import Catalog
from ..domain.clock import Clock, SystemClock
from ..domain.fees import BINANCE_STANDARD
from ..domain.profile import Profile
from ..market.binance.rest import BASE_URL, BinanceRest
from ..market.binance.stream import BinanceStream
from ..market.source import MarketData
from ..market.stream import MarketStream
from ..runtime.bus import EventBus
from ..runtime.interventions import Interventions
from ..runtime.health import CollectorWatch
from ..runtime.model_access import ModelAccess
from ..runtime.notifications import Notifier, PanelChannel
from ..runtime.profiles import ProfileService
from ..runtime.supervisor import Supervisor
from ..storage.database import connect
from ..storage.journal import JournalRepo
from ..storage.ledger import MIGRATIONS, Ledger
from ..storage.migrate import migrate
from ..storage.notifications import NotificationRepo
from ..storage.profiles import ProfileRepo
from ..storage.runtime_state import StateRepo
from ..strategy.base import Deps
from ..strategy.llm.client import ClaudeCliClient, LLMClient

_ROOT = Path(__file__).resolve().parents[2]

log = logging.getLogger("opus_trade.core")


def assemble(cfg: AppConfig, clock: Clock, market: MarketData, stream: MarketStream, llm: LLMClient | None) -> Core:
    """Çekirdeğin parçalarını kurar ve birbirine bağlar."""
    plugins.load()
    con = connect(cfg.core_db)
    applied = migrate(con, MIGRATIONS, cfg.backups)
    if applied:
        log.info("Taşımalar uygulandı: %s", ", ".join(applied))
    ledger, journal, state, repo = Ledger(con), JournalRepo(con), StateRepo(con), ProfileRepo(con)
    catalog, bus = Catalog(), EventBus()
    access = ModelAccess(state, clock)

    def deps(p: Profile) -> Deps:
        env = {"OPUS_MARKET_DB": str(cfg.market_db), "OPUS_CORE_DB": str(cfg.core_db),
               "OPUS_ACCOUNT": str(p.account_id), "OPUS_PROFILE": str(p.id)}
        return Deps(clock, catalog, llm, {"work_dir": cfg.work_dir(p.id), "tool_env": env, "market_db": cfg.market_db})

    sup = Supervisor(repo, ledger, journal, state, market, stream, clock, bus, access, catalog, BINANCE_STANDARD,
                     deps)
    iv = Interventions(con, state, clock, sup.runners.get)
    sup.interventions = iv
    service = ProfileService(repo, ledger, journal, catalog, clock, sup.refresh)
    notes = NotificationRepo(con)
    notifier = Notifier(bus, notes, repo, journal, clock, [PanelChannel(bus)])
    return Core(repo, ledger, journal, state, service, sup, iv, access, bus, catalog, clock, notes, notifier,
                CollectorWatch(cfg.market_db, bus))


async def _watch_collector(watch: CollectorWatch, clock: Clock) -> None:
    while True:
        try:
            watch.check(clock.time())
        except Exception:
            log.exception("Toplayıcının durumu okunamadı.")
        await asyncio.sleep(60)


async def _main(cfg: AppConfig) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=20) as client:
        core = assemble(cfg, SystemClock(), BinanceRest(client), BinanceStream(),
                        ClaudeCliClient(Path(sys.executable), _ROOT))
        # Arka plan görevleri kapanışta iptal edilir, sunucu ise should_exit ile
        # süren isteklerini bitirip kendisi kapanır.
        background = [asyncio.create_task(core.supervisor.run()), asyncio.create_task(core.notifier.run()),
                      asyncio.create_task(_watch_collector(core.collector, core.clock))]
        tasks = list(background)
        server = None
        if cfg.core_socket:
            cfg.core_socket.unlink(missing_ok=True)
            # opus-web'in canlı akış bağlantısı kendiliğinden bitmez, kapanış onu
            # en fazla beş saniye bekler.
            server = uvicorn.Server(uvicorn.Config(create_app(core), uds=str(cfg.core_socket), log_level="warning",
                                                   timeout_graceful_shutdown=5))
            server.install_signal_handlers = lambda: None
            tasks.append(asyncio.create_task(server.serve()))
        log.info("Çekirdek başladı, %d profil kuruldu.", len(core.supervisor.runners))
        await stop.wait()
        if server:
            server.should_exit = True
        for t in background:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    log.info("Çekirdek durdu.")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(_main(AppConfig.from_env()))


if __name__ == "__main__":
    main()
