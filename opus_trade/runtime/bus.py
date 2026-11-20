"""
file: bus.py
importable: EventBus

EventBus olanları dinleyicilere yayar. subscribe() yeni bir kuyruk açar,
unsubscribe() kapatır, publish() bir olayı bütün açık kuyruklara koyar ve
beklemeden döner. Yayan taraf kimin dinlediğini bilmez.

description: Süreç içi olay yayını.
"""

# NOTE: Kuyruğu dolan bir dinleyici, örneğin uyku modundaki bir tarayıcı
# sekmesi, diğerlerini bekletmesin diye olay o dinleyici için atlanır ve
# dropped sayacı artar. Kaybedilmemesi gereken kayıtlar zaten veritabanına
# yazılır, yol yalnızca canlı gösterim içindir.

import asyncio
from typing import Any


class EventBus:
    def __init__(self, maxsize: int = 2000) -> None:
        self._maxsize = maxsize
        self._queues: set[asyncio.Queue] = set()
        self.dropped = 0

    # Kaybetmemesi gereken bir dinleyici, örneğin bildirimler, sınırsız kuyruk
    # ister. Süreç içindedir ve olayları geldiği hızda tüketir.
    def subscribe(self, unbounded: bool = False) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=0 if unbounded else self._maxsize)
        self._queues.add(q)
        return q

    # Sistem sayfası kaç dinleyici olduğunu gösterir. Bildirici de bir
    # dinleyicidir, her açık panel sekmesi ve herkese açık akış birer tane.
    @property
    def listeners(self) -> int:
        return len(self._queues)

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._queues.discard(q)

    def publish(self, item: Any) -> None:
        for q in self._queues:
            if q.full():
                self.dropped += 1
            else:
                q.put_nowait(item)
