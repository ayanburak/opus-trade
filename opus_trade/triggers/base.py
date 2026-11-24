"""
file: base.py
importable: Fire, Setup, Trigger, TRIGGERS

Trigger piyasayı izleyip bir stratejiyi uyandıran parçadır. Çalıştırıcı ona
canlı likidasyonları, mumları, dinlediği paritelerin tekil işlemlerini ve
saniyede bir zamanı verir. Bir tetikleyici uyandırmak istediğinde Fire
listesi döndürür. wanted() tetikleyicinin dinlemek istediği pariteleri ve
akışlarını verir, None ise profilin kendi paritelerinin mumu yeter.
snapshot() arayüzde gösterilecek o anki durumunu verir. Kurulurken Setup
alır. TRIGGERS tetikleyici türlerinin kayıt defteridir.

description: Tetikleyici soyut sınıfı ve tür kayıt defteri.
"""

# NOTE: Girişlerin hepsi varsayılan olarak hiçbir şey yapmaz, bir tetikleyici
# yalnızca kendi işine yarayan girişi yazar.
#
# NOTE: Fire'ın data alanı tetikleyiciye özgüdür ve stratejiye olduğu gibi
# gider. Kural stratejisi onu doğrudan okur, LLM stratejisi Türkçe bir
# cümleye çevirir.
#
# NOTE: wanted() her saniye okunur ve sonucu değişince akışın aboneliği
# değişir. Sonuç bir izleme listesine bağlıysa liste değişmedikçe aynı kalmalı,
# yoksa her saniye abonelik mesajı giderdi.

from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

from ..market.models import Candle, Liquidation, Trade
from ..market.screen import MarketScreen
from ..market.stream import Feed
from ..registry import Registry


# ms bildirimi doğuran piyasa olayının Binance damgasıdır, milisaniye.
# Hızlı yolda olaydan emre geçen süre bununla ölçülür.
@dataclass(frozen=True)
class Fire:
    kind: str
    time: int
    symbol: str
    data: Any
    ms: int | None = None


# symbols profilin sabit listesi, universe profilin işlem yapabildiği bütün
# paritelerdir, bütün piyasa seçilmiş profilde katalogun tamamı. screen ve
# flow bütün profillerin ortak parçalarıdır.
@dataclass(frozen=True)
class Setup:
    symbols: tuple[str, ...]
    universe: Collection[str]
    screen: MarketScreen | None = None
    flow: Any = None


class Trigger:
    kind: str = ""

    def wanted(self) -> dict[str, frozenset[Feed]] | None:
        return None

    def on_trade(self, trade: Trade) -> list[Fire]:
        return []

    def on_liquidation(self, liq: Liquidation) -> list[Fire]:
        return []

    def on_candle(self, candle: Candle) -> list[Fire]:
        return []

    def tick(self, now: int) -> list[Fire]:
        return []

    def snapshot(self) -> dict | None:
        return None


TRIGGERS: Registry[Trigger] = Registry("tetikleyici")
