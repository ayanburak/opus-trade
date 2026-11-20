"""
file: base.py
importable: Fire, Trigger, TRIGGERS

Trigger piyasayı izleyip bir stratejiyi uyandıran parçadır. Çalıştırıcı ona
canlı likidasyonları, mumları ve saniyede bir zamanı verir. Bir tetikleyici
uyandırmak istediğinde Fire listesi döndürür. snapshot() arayüzde
gösterilecek o anki durumunu verir. TRIGGERS tetikleyici türlerinin kayıt
defteridir.

description: Tetikleyici soyut sınıfı ve tür kayıt defteri.
"""

# NOTE: Girişlerin hepsi varsayılan olarak hiçbir şey yapmaz, bir tetikleyici
# yalnızca kendi işine yarayan girişi yazar.
#
# NOTE: Fire'ın data alanı tetikleyiciye özgüdür ve stratejiye olduğu gibi
# gider. Kural stratejisi onu doğrudan okur, LLM stratejisi Türkçe bir
# cümleye çevirir.

from dataclasses import dataclass
from typing import Any

from ..market.models import Candle, Liquidation
from ..registry import Registry


@dataclass(frozen=True)
class Fire:
    kind: str
    time: int
    symbol: str
    data: Any


class Trigger:
    kind: str = ""

    def on_liquidation(self, liq: Liquidation) -> list[Fire]:
        return []

    def on_candle(self, candle: Candle) -> list[Fire]:
        return []

    def tick(self, now: int) -> list[Fire]:
        return []

    def snapshot(self) -> dict | None:
        return None


TRIGGERS: Registry[Trigger] = Registry("tetikleyici")
