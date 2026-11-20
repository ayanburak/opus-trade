"""
file: collector.py
importable: yok

Toplayıcı servisinin giriş noktası. OPUS_STATE ile verilen klasördeki
market.db'yi açar, eksik taşımaları uygular ve verilen komutu çalıştırır.

    python -m opus_trade.app.collector run
    python -m opus_trade.app.collector symbols
    python -m opus_trade.app.collector add SYMBOL
    python -m opus_trade.app.collector remove SYMBOL
    python -m opus_trade.app.collector import-liquidations ESKI_MARKET_DB

run SIGTERM ya da Ctrl+C gelene kadar çalışır, kapanırken tamponları yazar.
Parite komutları izlenen pariteleri değiştirir, çalışan servis değişikliği
bir sonraki turda görür.

description: Veri toplayıcının kuruluş noktası ve komut satırı.
"""

# NOTE: Parçalar yalnızca burada seçilip birbirine bağlanır. Toplayıcının
# kaynakları hangi borsanın kullanıldığını bilmez, BinanceRest ve
# BinanceStream burada verilir.
#
# TODO: Parite komutları admin paneline taşınınca burada yalnızca run kalır.

import argparse
import asyncio
import logging
import signal
from pathlib import Path

import httpx

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

log = logging.getLogger("opus_trade.collector")


def _store(cfg: AppConfig) -> CollectorStore:
    con = connect(cfg.market_db)
    applied = migrate(con, MIGRATIONS, cfg.backups)
    if applied:
        log.info("Taşımalar uygulandı: %s", ", ".join(applied))
    return CollectorStore(con)


async def _run(store: CollectorStore) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    clock = SystemClock()
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=20) as client:
        rest = BinanceRest(client, min_interval_s=_REQUEST_GAP_S)
        sources = [LiquidationSource(BinanceStream(), store, clock), PremiumSource(rest, store, clock),
                   StatsSource(rest, store, clock)]
        log.info("Toplayıcı başladı, %d parite izleniyor.", len(store.symbols()))
        await run_sources(sources, stop)
    log.info("Toplayıcı durdu.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m opus_trade.app.collector")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    sub.add_parser("symbols")
    for name in ("add", "remove"):
        sub.add_parser(name).add_argument("symbol")
    sub.add_parser("import-liquidations").add_argument("path", type=Path)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # httpx her isteği INFO seviyesinde yazar. Toplayıcı günde on binlerce
    # istek attığı için bu satırlar asıl uyarıları günlükte boğardı.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    store = _store(AppConfig.from_env())
    if args.cmd == "run":
        asyncio.run(_run(store))
    elif args.cmd == "symbols":
        print("\n".join(store.symbols()))
    elif args.cmd == "add":
        store.add_symbol(args.symbol.upper(), SystemClock().time())
    elif args.cmd == "remove":
        store.remove_symbol(args.symbol.upper())
    else:
        print(f"{import_legacy_liquidations(store, args.path)} yeni likidasyon aktarıldı.")


if __name__ == "__main__":
    main()
