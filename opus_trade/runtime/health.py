"""
file: health.py
importable: CollectorWatch, COLLECTOR_SILENCE_S

CollectorWatch toplayıcının çalışıp çalışmadığını market.db'nin kapsama
kaydından anlar. check() her çağrıldığında son kaydın zamanına bakar,
COLLECTOR_SILENCE_S'den uzun süredir yazılmamışsa bunu olay yoluna bir kez
yayar, yazmaya yeniden başlayınca döndüğünü yayar.

description: Toplayıcının çalıştığının çekirdekten izlenmesi.
"""

# NOTE: Toplayıcının canlı kaynakları her dakika kapsama kaydına dokunur, bu
# yüzden en yeni end_time toplayıcının son yaşadığı andır. Veritabanı salt
# okunur açılır, çekirdek market.db'ye hiç yazmaz.
#
# NOTE: market.db henüz yoksa ya da tablosu yoksa izleyici susar. Toplayıcı
# hiç kurulmamış bir yerde, örneğin public depoyu deneyen birinde, her dakika
# kritik bildirim çalmasın.

import sqlite3
from pathlib import Path

from .bus import EventBus

# Toplayıcının durmuş sayılması için yazmadan geçen en uzun süre, saniye.
COLLECTOR_SILENCE_S = 300


class CollectorWatch:
    def __init__(self, market_db: Path, bus: EventBus) -> None:
        self.market_db = market_db
        self.bus = bus
        self.down = False

    def last_seen(self) -> int | None:
        if not self.market_db.exists():
            return None
        con = sqlite3.connect(f"file:{self.market_db}?mode=ro", uri=True)
        try:
            return con.execute("SELECT MAX(end_time) FROM coverage").fetchone()[0]
        except sqlite3.OperationalError:
            return None
        finally:
            con.close()

    def check(self, now: int) -> None:
        last = self.last_seen()
        if last is None:
            return
        silent = now - last > COLLECTOR_SILENCE_S
        if silent and not self.down:
            self.down = True
            self.bus.publish({"collector": "down", "since": last})
        elif not silent and self.down:
            self.down = False
            self.bus.publish({"collector": "up"})
