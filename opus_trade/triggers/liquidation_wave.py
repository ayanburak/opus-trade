"""
file: liquidation_wave.py
importable: Wave, LiquidationWave

LiquidationWave bir paritede likidasyon dalgası bittiğinde, baskın tarafı
paritenin eşiğini geçtiyse Fire döndürür. Fire'ın data alanı biten dalganın
Wave kaydıdır. Aynı saniyede biten dalgalar aynı tick'te birlikte döner.

description: Likidasyon dalgası tetikleyicisi.
"""

# NOTE: Aralarında quiet_s'den uzun boşluk olmayan likidasyonlar aynı dalgadır.
# Dalga son likidasyonundan quiet_s geçince bitmiş sayılır. Bitiş hem tick'te
# hem yeni bir likidasyon geldiğinde kontrol edilir, tick gecikse de iki dalga
# karışmaz.
#
# NOTE: Fiyat aralığı likidasyon fiyatlarından değil, dalganın başladığı
# dakikadan bitişine kadarki mumların en düşük ve en yüksek fiyatından ölçülür.
# 18.11 testinde aralık likidasyon fiyatlarından ölçülünce dalgadan sonraki
# dönüş kuralı kârını kaybetti, mumlardan ölçülünce korudu. Mum henüz
# gelmediyse likidasyon fiyatları kullanılır.
#
# NOTE: Bekleme süresi her paritede ayrıdır. Ortak bir bekleme, bir paritenin
# bildiriminden hemen sonra biten başka paritelerin dalgalarını atardı. Bekleme
# süresine denk gelen dalga atılır, sıraya alınmaz, eski bir dalgaya göre
# karar verilmesin diye.

from dataclasses import dataclass

from ..domain.models import Side
from ..market.models import Candle, Liquidation
from ..registry import Param
from .base import TRIGGERS, Fire, Trigger

# Bekleme süresi bundan kısa olamaz, bir dalganın kuyruğu aynı paritede art
# arda bildirim üretmesin diye.
_MIN_COOLDOWN_S = 30


@dataclass
class Wave:
    symbol: str
    start: int
    end: int
    long_usd: float
    short_usd: float
    low: float
    high: float

    @property
    def dominant(self) -> Side:
        return Side.LONG if self.long_usd >= self.short_usd else Side.SHORT

    @property
    def dominant_usd(self) -> float:
        return max(self.long_usd, self.short_usd)


class LiquidationWave(Trigger):
    """Biten ve eşiği geçen likidasyon dalgalarını bildirir.

    Args:
        symbols: İzlenen pariteler.
        min_usd: Her paritenin baskın taraf eşiği, dolar.
        quiet_s: Dalganın bittiği sayılması için son likidasyondan sonra geçmesi gereken süre, saniye.
        cooldown_s: Aynı paritenin iki bildirimi arasındaki en kısa süre, saniye.
    """

    kind = "liquidation_wave"

    def __init__(self, symbols: tuple[str, ...], min_usd: dict[str, float], quiet_s: int = 120,
                 cooldown_s: int = 60) -> None:
        missing = set(symbols) - set(min_usd)
        if missing:
            raise ValueError(f"Şu paritelerin eşiği verilmedi: {', '.join(sorted(missing))}.")
        self.symbols = set(symbols)
        self.min_usd = dict(min_usd)
        self.quiet_s = quiet_s
        self.cooldown_s = max(cooldown_s, _MIN_COOLDOWN_S)
        self._waves: dict[str, Wave] = {}
        self._candles: dict[str, Candle] = {}
        self._last_fire: dict[str, int] = {}
        self._results: dict[str, dict] = {}

    def on_candle(self, candle: Candle) -> list[Fire]:
        self._candles[candle.symbol] = candle
        w = self._waves.get(candle.symbol)
        if w is not None and candle.time >= w.start // 60 * 60:
            w.low, w.high = min(w.low, candle.low), max(w.high, candle.high)
        return []

    def on_liquidation(self, liq: Liquidation) -> list[Fire]:
        s = liq.symbol
        if s not in self.symbols:
            return []
        out = self._close_if_quiet(s, liq.time)
        w = self._waves.get(s)
        if w is None:
            w = self._waves[s] = Wave(s, liq.time, liq.time, 0.0, 0.0, liq.price, liq.price)
            c = self._candles.get(s)
            if c and c.time == liq.time // 60 * 60:
                w.low, w.high = min(w.low, c.low), max(w.high, c.high)
        w.end = liq.time
        if liq.side is Side.LONG:
            w.long_usd += liq.usd
        else:
            w.short_usd += liq.usd
        w.low, w.high = min(w.low, liq.price), max(w.high, liq.price)
        return out

    def tick(self, now: int) -> list[Fire]:
        return [f for s in sorted(self._waves) for f in self._close_if_quiet(s, now)]

    # outcome üç değer alır: below eşiğin altında kaldı, cooldown eşiği geçti
    # ama bekleme süresine denk geldi, fired stratejiyi uyandırdı.
    def _close_if_quiet(self, s: str, now: int) -> list[Fire]:
        w = self._waves.get(s)
        if w is None or now - w.end <= self.quiet_s:
            return []
        del self._waves[s]
        result = {"start": w.start, "end": w.end, "long": w.long_usd, "short": w.short_usd, "outcome": "below"}
        self._results[s] = result
        if w.dominant_usd < self.min_usd[s]:
            return []
        if now - self._last_fire.get(s, -10 ** 12) < self.cooldown_s:
            result["outcome"] = "cooldown"
            return []
        self._last_fire[s] = now
        result["outcome"] = "fired"
        return [Fire(self.kind, now, s, w)]

    def snapshot(self) -> dict:
        """Her paritenin eşiğini, süren dalgasını ve biten son dalgasının sonucunu döndürür."""
        out = {}
        for s in sorted(self.symbols):
            w = self._waves.get(s)
            out[s] = {"min_usd": self.min_usd[s],
                      "wave": {"start": w.start, "end": w.end, "long": w.long_usd, "short": w.short_usd} if w else None,
                      "last": dict(self._results[s]) if s in self._results else None}
        return {"quiet_s": self.quiet_s, "symbols": out}


TRIGGERS.register(
    "liquidation_wave", "Likidasyon dalgası",
    "Bir paritede likidasyon dalgası durulduğunda, baskın taraf paritenin eşiğini geçtiyse stratejiyi uyandırır.",
    (Param("min_usd", "symbol_map", "Parite başına eşik, dolar", low=0),
     Param("quiet_s", "int", "Durulma süresi, saniye", default=120, low=10, high=3600),
     Param("cooldown_s", "int", "Bekleme süresi, saniye", default=60, low=_MIN_COOLDOWN_S, high=86400)),
    lambda setup, **p: LiquidationWave(setup.symbols, **p),
)
