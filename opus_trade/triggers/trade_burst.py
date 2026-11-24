"""
file: trade_burst.py
importable: Burst, TradeBurst

TradeBurst izleme listesindeki paritelerin tekil işlemlerine bakar ve sakin
bir paritede tek yönlü bir patlama başladığında Fire döndürür. Liste saat
başı MarketScreen'den kurulur, yalnızca o paritelerin işlemleri dinlenir.
Fire'ın data alanı Burst kaydıdır, ms alanı patlamayı tamamlayan işlemin
Binance damgasıdır.

description: Tekil işlem akışında tek yönlü patlama tetikleyicisi.
"""

# NOTE: Eşiklerin hepsi aynı işlemde geçilmelidir: 5 ve 60 saniyelik hareket
# aynı yönde, işlem hızı normalin katı, hareket yönündeki tarafın hacim payı
# ve fiyatın 20 dakikalık ortalamasından kopması. Normal, son normal_back_s
# saniyeyi saymayan önceki dakikalardır, varsayılanı 60. Hareket 60 saniyede
# ölçüldüğü için normal de o 60 saniyenin dışından alınır. Eskiden yalnızca son
# 5 saniye dışarıda kalıyordu, bir dakikaya yayılan iğnenin ilk 55 saniyesi
# normali şişirip iğneyi kendi kendine gizliyordu. 25.11 GUAUSDT 01:20'de
# görüldü: normal saniyede 13 işlemken hesap 44 buldu. Normalde hiç işlem
# yoksa parite tamamen sakindir ve hız katı sonsuz sayılır.
#
# NOTE: Bir paritenin özeti normalin penceresi ile dışarıda bırakılan sürenin
# toplamından kısaysa sinyal verilmez.
# Liste saat başı değişince yeni giren paritenin birkaç dakika ısınması
# gerekir, eksik geçmişle ölçülen normal patlamayı ya gizler ya da uydurur.
#
# NOTE: Sinyal yalnızca tekil işlemlerden hesaplanır ama mum da istenir.
# Panelin ve herkese açık sayfanın grafiği canlı fiyatı mum akışından alır,
# mum dinlenmezse listedeki coin'lerin grafiği donar. 25.11 yayınında görüldü.
# Mumla gelen fiyat broker'ı fiyatlamaz, emirler yine işlemle dolar.
#
# NOTE: İlk ve ucuz eşik 5 saniyelik harekettir. İşlemlerin neredeyse hepsi
# orada elenir, diğer sorgular yalnızca hareket varken yapılır.

from dataclasses import dataclass

from ..market.flow import TradeFlow
from ..market.models import Trade
from ..market.screen import RANKINGS
from ..market.stream import Feed
from ..registry import Param
from .base import TRIGGERS, Fire, Setup, Trigger

_MEAN_S = 1200


# up yükselen bir iğnedir. start iğnenin 60 saniye önceki başlangıç
# fiyatı, tip son fiyattır. Yüzdeler oran olarak tutulur.
@dataclass
class Burst:
    symbol: str
    up: bool
    start: float
    tip: float
    move_5s: float
    move_60s: float
    rate_mult: float
    side_share: float
    mean_gap: float


class TradeBurst(Trigger):
    """İzleme listesinde tek yönlü patlamayı bildirir.

    Args:
        setup: Kurulum: evren, izleme listesi kaynağı ve akış özeti.
        list_size: Listeye her sıralamadan alınan parite sayısı.
        ranking: both, range ya da change.
        move_5s_pct: Son 5 saniyedeki en küçük hareket, yüzde.
        move_60s_pct: Son 60 saniyedeki en küçük hareket, yüzde.
        rate_mult: Son 5 saniyenin işlem hızının normale en küçük katı.
        side_share_pct: Hareket yönündeki tarafın son 5 saniyedeki en küçük hacim payı, yüzde.
        mean_gap_pct: Fiyatın 20 dakikalık ortalamasından en küçük kopması, yüzde.
        normal_s: Normalin penceresi, saniye.
        normal_back_s: Normalin dışarıda bıraktığı son süre, saniye.
        cooldown_s: Aynı paritenin iki bildirimi arasındaki en kısa süre, saniye.
    """

    kind = "trade_burst"

    def __init__(self, setup: Setup, list_size: int = 20, ranking: str = "both", move_5s_pct: float = 3,
                 move_60s_pct: float = 5, rate_mult: float = 5, side_share_pct: float = 60, mean_gap_pct: float = 6,
                 normal_s: int = 300, cooldown_s: int = 120, normal_back_s: int = 60) -> None:
        self.setup = setup
        self.flow: TradeFlow = setup.flow
        self.list_size = list_size
        self.ranking = ranking
        self.move_5s = move_5s_pct / 100
        self.move_60s = move_60s_pct / 100
        self.rate_mult = rate_mult
        self.side_share = side_share_pct / 100
        self.mean_gap = mean_gap_pct / 100
        self.normal_s = normal_s
        self.normal_back = normal_back_s
        self.cooldown_s = cooldown_s
        self._version = -1
        self._list: list[str] = []
        self._wanted: dict[str, frozenset[Feed]] = {}
        self._last_fire: dict[str, int] = {}
        self._last: dict = {}

    # Liste izleme kaynağının sürümü değişince yeniden kurulur, arada aynı
    # sözlük döner ve abonelik değişmez.
    def wanted(self) -> dict[str, frozenset[Feed]]:
        screen = self.setup.screen
        if screen is not None and screen.version != self._version:
            self._version = screen.version
            self._list = screen.top(self.list_size, self.ranking, self.setup.universe)
            self._wanted = {s: frozenset({Feed.TRADE, Feed.KLINE}) for s in self._list}
        return dict(self._wanted)

    def on_trade(self, trade: Trade) -> list[Fire]:
        s, f = trade.symbol, self.flow
        if s not in self._wanted or not f.has(s):
            return []
        m5 = f.move(s, 5)
        if abs(m5) < self.move_5s:
            return []
        sec = trade.ms // 1000
        if sec - self._last_fire.get(s, -10**9) < self.cooldown_s or f.span(s) < self.normal_s + self.normal_back:
            return []
        up = m5 > 0
        m60 = f.move(s, 60)
        if (m60 > 0) != up or abs(m60) < self.move_60s:
            return []
        normal = f.count(s, self.normal_s, back=self.normal_back) / self.normal_s
        recent = f.count(s, 5) / 5
        mult = recent / normal if normal > 0 else float("inf")
        usd = f.usd(s, 5)
        share = f.buy_usd(s, 5) / usd if usd > 0 else 0.5
        share = share if up else 1 - share
        gap = f.last(s) / f.mean(s, _MEAN_S) - 1
        if mult < self.rate_mult or share < self.side_share or (gap > 0) != up or abs(gap) < self.mean_gap:
            return []
        self._last_fire[s] = sec
        burst = Burst(s, up, f.price_at(s, 60), f.last(s), m5, m60, mult, share, gap)
        self._last[s] = {"time": sec, "up": up, "move_5s": round(m5 * 100, 2), "move_60s": round(m60 * 100, 2)}
        return [Fire(self.kind, sec, s, burst, trade.ms)]

    def snapshot(self) -> dict:
        return {"list": list(self._list), "version": self._version, "last": dict(self._last)}


TRIGGERS.register(
    "trade_burst", "İşlem patlaması",
    "Saat başı kurulan izleme listesindeki paritelerin tekil işlemlerini dinler. Sakin bir paritede tek yönlü bir "
    "patlama başlayınca, yani fiyat hızla gidip işlem hızı normalin katına çıkınca ve hacim tek taraftan gelince "
    "stratejiyi uyandırır.",
    (Param("list_size", "int", "Listedeki parite sayısı, her sıralamadan", default=20, low=1, high=100),
     Param("ranking", "choice", "Sıralama", default="both", choices=RANKINGS,
           labels=("Aralık ve değişim", "24 saatlik aralık", "24 saatlik değişim")),
     Param("move_5s_pct", "float", "5 saniyelik en küçük hareket, yüzde", default=3, low=0.1, high=50),
     Param("move_60s_pct", "float", "60 saniyelik en küçük hareket, yüzde", default=5, low=0.1, high=80),
     Param("rate_mult", "float", "İşlem hızının normale en küçük katı", default=5, low=1, high=1000),
     Param("side_share_pct", "float", "Hareket yönündeki tarafın en küçük hacim payı, yüzde", default=60, low=50,
           high=100),
     Param("mean_gap_pct", "float", "20 dakikalık ortalamadan en küçük kopma, yüzde", default=6, low=0, high=80),
     Param("normal_s", "int", "Normalin penceresi, saniye", default=300, low=30, high=1100),
     Param("normal_back_s", "int", "Normalin dışarıda bıraktığı son süre, saniye", default=60, low=5, high=100),
     Param("cooldown_s", "int", "Aynı paritede iki sinyal arası, saniye", default=120, low=5, high=86400)),
    lambda setup, **p: TradeBurst(setup, **p),
)
