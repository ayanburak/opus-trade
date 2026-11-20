"""
file: service.py
importable: run_sources, import_legacy_liquidations

run_sources() kaynakları birlikte çalıştırır ve verilen olay kurulana kadar
bekler, sonra hepsini iptal edip tamponlarını yazmalarını bekler. Beklenmedik
bir hatayla düşen kaynak kısa bir süre sonra yeniden başlatılır.
import_legacy_liquidations() eski sistemin market.db dosyasındaki
likidasyonları depoya aktarır.

description: Toplayıcı kaynaklarının birlikte yürütülmesi ve eski verinin aktarımı.
"""

# TODO: import_legacy_liquidations geçişten sonra silinir. Eski sistemin
# deposu geçişte arşive gider ve bir daha aktarılacak bir şey kalmaz.

import asyncio
import logging
import sqlite3
from pathlib import Path

from ..domain.models import Side
from ..market.models import Liquidation
from .sources import Source
from .store import CollectorStore

log = logging.getLogger(__name__)

_RESTART_S = 10


# Bir kaynağın kendi kodundaki bir hata diğer kaynakları durdurmaz. Hata
# günlüğe tam izle yazılır ve kaynak baştan başlar. Kalıcı bir hata on
# saniyede bir tekrarlanır ve günlükte açıkça görünür.
async def _keep(source: Source) -> None:
    while True:
        try:
            await source.run()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s kaynağı beklenmedik bir hatayla durdu, %s saniye sonra yeniden başlıyor.",
                          source.name, _RESTART_S)
            await asyncio.sleep(_RESTART_S)


async def run_sources(sources: list[Source], stop: asyncio.Event) -> None:
    tasks = [asyncio.create_task(_keep(s), name=s.name) for s in sources]
    await stop.wait()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def import_legacy_liquidations(store: CollectorStore, path: Path) -> int:
    """Eski sistemin likidasyonlarını aktarır ve yazılan yeni kayıt sayısını döndürür.

    Eski dosya salt okunur açılır. Toplayıcının zaten yazdığı kayıtlar aynı
    akıştan geldiği için birebir aynıdır ve atlanır.
    """
    old = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = old.execute("SELECT time, symbol, side, price, qty FROM liquidations").fetchall()
    finally:
        old.close()
    return store.add_liquidations([Liquidation(t, s, Side(side), p, q) for t, s, side, p, q in rows])
