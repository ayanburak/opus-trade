"""
file: collector.py
importable: yok

Toplayıcı servisinin giriş noktası. OPUS_STATE ile verilen klasördeki
market.db'yi açar, eksik taşımaları uygular ve verilen komutu çalıştırır.

    python -m opus_trade.app.collector run
    python -m opus_trade.app.collector symbols
    python -m opus_trade.app.collector import-liquidations ESKI_MARKET_DB

run SIGTERM ya da Ctrl+C gelene kadar çalışır, kapanırken tamponları yazar.
OPUS_COLLECTOR_SOCKET verilmişse run aynı süreçte toplayıcının uçlarını o
sokette açar. İzlenen pariteler yalnızca bu uçlardan, admin panelinin Veri
sayfasından değişir. symbols yalnızca listeyi yazar.

description: Veri toplayıcının kuruluş noktası ve komut satırı.
"""

# NOTE: Parçalar yalnızca burada seçilip birbirine bağlanır. Toplayıcının
# kaynakları hangi borsanın kullanıldığını bilmez, BinanceRest ve
# BinanceStream burada verilir.

import argparse
import asyncio
import logging
import signal
from pathlib import Path

import httpx
import uvicorn

from ..collector.api import create_collector_app
from ..collector.service import import_legacy_liquidations, run_sources
from ..collector.sources import LiquidationSource, PremiumSource, StatsSource
from ..collector.store import MIGRATIONS, CollectorStore
from ..config import AppConfig
from ..domain.clock import SystemClock
from ..market.binance.rest import BASE_URL, BinanceRest
from ..market.binance.stream import BinanceStream
from ..storage.database import connect
from ..storage.migrate import migrate

# İki istek arasındaki en kısa süre. Binance istatistik uçlarında beş dakikada
# bin istek sınırı koyuyor, bu aralık sınırın yarısının altında kalır.
_REQUEST_GAP_S = 0.6

# Panelin toplayıcıya giden httpx istemcisi boştaki bağlantıyı 5 saniye
# tutar. Sunucu bağlantıyı daha uzun tutmazsa iki taraf aynı anda kapatıp
# kullanmaya çalışır ve panel ara sıra 502 alır, çekirdekteki KEEP_ALIVE_S
# ile aynı neden.
KEEP_ALIVE_S = 30

log = logging.getLogger("opus_trade.collector")


def _store(cfg: AppConfig) -> CollectorStore:
    con = connect(cfg.market_db)
    applied = migrate(con, MIGRATIONS, cfg.backups)
    if applied:
        log.info("Taşımalar uygulandı: %s", ", ".join(applied))
    return CollectorStore(con)


async def _run(store: CollectorStore, cfg: AppConfig) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    clock = SystemClock()
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=20) as client:
        rest = BinanceRest(client, min_interval_s=_REQUEST_GAP_S)
        sources = [LiquidationSource(BinanceStream(), store, clock), PremiumSource(rest, store, clock),
                   StatsSource(rest, store, clock)]
        tasks = [asyncio.create_task(run_sources(sources, stop))]
        server = None
        if cfg.collector_socket:
            cfg.collector_socket.unlink(missing_ok=True)
            app = create_collector_app(store, cfg.market_db, rest, clock)
            # İndirme süren bir istektir, kapanış onu en fazla beş saniye bekler.
            server = uvicorn.Server(uvicorn.Config(app, uds=str(cfg.collector_socket), log_level="warning",
                                                   timeout_graceful_shutdown=5, timeout_keep_alive=KEEP_ALIVE_S))
            server.install_signal_handlers = lambda: None
            tasks.append(asyncio.create_task(server.serve()))
        log.info("Toplayıcı başladı, %d parite izleniyor.", len(store.symbols()))
        await stop.wait()
        if server:
            server.should_exit = True
        await asyncio.gather(*tasks, return_exceptions=True)
    log.info("Toplayıcı durdu.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m opus_trade.app.collector")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    sub.add_parser("symbols")
    sub.add_parser("import-liquidations").add_argument("path", type=Path)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # httpx her isteği INFO seviyesinde yazar. Toplayıcı günde on binlerce
    # istek attığı için bu satırlar asıl uyarıları günlükte boğardı.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = AppConfig.from_env()
    store = _store(cfg)
    if args.cmd == "run":
        asyncio.run(_run(store, cfg))
    elif args.cmd == "symbols":
        print("\n".join(store.symbols()))
    else:
        print(f"{import_legacy_liquidations(store, args.path)} yeni likidasyon aktarıldı.")


if __name__ == "__main__":
    main()
