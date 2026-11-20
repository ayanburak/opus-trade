"""
file: indicators.py
importable: Series, rounded, ema, sma, rsi, atr, bollinger, macd, stochastic, vwap

Teknik göstergeleri mum listelerinden hesaplar. Her fonksiyon girdisiyle aynı
uzunlukta bir seri döndürür, gösterge henüz hesaplanamayan baştaki yerler
None'dır. Böylece seri mumların zamanıyla birebir eşleşir. Mumlar eskiden
yeniye sıralı olmalıdır. rounded() bir değeri büyüklüğüne göre yeterli
basamakla yuvarlar.

description: EMA, SMA, RSI, ATR, Bollinger, MACD, stokastik ve VWAP hesapları.
"""

# NOTE: Hesaplar bilerek saf Python. Mum sayısı birkaç bini geçmediği için
# numpy ya da pandas gibi ağır bir bağımlılığa gerek yok. Formüller
# TradingView'in varsayılanlarıyla aynıdır, RSI ve ATR Wilder yumuşatmasını
# kullanır.
#
# NOTE: Göstergelerin Opus'a görünen Türkçe adları ve varsayılan ayarları
# burada değil LLM stratejisindedir. Bu modül yalnızca hesaplar.

import math

from .models import Candle

Series = list[float | None]


def rounded(v: float) -> float:
    """Değeri büyüklüğüne göre en az altı anlamlı basamakla yuvarlar.

    BTC'de iki basamak yeterliyken XRP'de ATR gibi küçük değerler dört beş
    basamak ister.
    """
    if v == 0 or not math.isfinite(v):
        return v
    return round(v, max(2, 5 - int(math.floor(math.log10(abs(v))))))


# EMA ilk değerden başlar ve ilk elemandan itibaren tanımlıdır, bu yüzden
# None içermez. İlk periyot kadar değeri ısınma sayılmalıdır.
def ema(values: list[float], period: int) -> list[float]:
    k = 2 / (period + 1)
    out = [values[0]] if values else []
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def sma(values: list[float], period: int) -> Series:
    out: Series = [None] * len(values)
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= period:
            total -= values[i - period]
        if i >= period - 1:
            out[i] = total / period
    return out


# Wilder yumuşatması: ilk ortalama basit ortalamadır, sonra her adımda önceki
# ortalamanın periyot eksi bir katına yeni değer eklenip periyoda bölünür.
def _wilder(values: list[float], period: int) -> Series:
    out: Series = [None] * len(values)
    if len(values) < period:
        return out
    avg = sum(values[:period]) / period
    out[period - 1] = avg
    for i in range(period, len(values)):
        avg = (avg * (period - 1) + values[i]) / period
        out[i] = avg
    return out


# Fark serisi bir eksik olduğu için başa bir None eklenir. Hiç düşüş yoksa
# ortalama kayıp sıfırdır ve RSI tanım gereği yüzdür.
def rsi(closes: list[float], period: int = 14) -> Series:
    diffs = [b - a for a, b in zip(closes, closes[1:])]
    gain = _wilder([max(d, 0) for d in diffs], period)
    loss = _wilder([max(-d, 0) for d in diffs], period)
    out: Series = [None]
    for g, l in zip(gain, loss):
        out.append(None if g is None else 100.0 if l == 0 else 100 - 100 / (1 + g / l))
    return out


# Gerçek aralık bir önceki mumun kapanışını ister, ilk mum için hesaplanamaz.
def atr(candles: list[Candle], period: int = 14) -> Series:
    tr = [max(m.high - m.low, abs(m.high - p.close), abs(m.low - p.close)) for p, m in zip(candles, candles[1:])]
    return [None] + _wilder(tr, period)


def bollinger(closes: list[float], period: int = 20, width: float = 2) -> dict[str, Series]:
    """Orta, üst ve alt bantları mid, upper ve lower anahtarlarıyla döndürür."""
    mid = sma(closes, period)
    upper: Series = [None] * len(closes)
    lower: Series = [None] * len(closes)
    for i, m in enumerate(mid):
        if m is None:
            continue
        window = closes[i - period + 1:i + 1]
        dev = (sum((x - m) ** 2 for x in window) / period) ** 0.5
        upper[i], lower[i] = m + width * dev, m - width * dev
    return {"mid": mid, "upper": upper, "lower": lower}


def macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict[str, list[float]]:
    """MACD çizgisini, sinyalini ve histogramı macd, signal ve histogram anahtarlarıyla döndürür."""
    line = [a - b for a, b in zip(ema(closes, fast), ema(closes, slow))]
    sig = ema(line, signal)
    return {"macd": line, "signal": sig, "histogram": [a - b for a, b in zip(line, sig)]}


# Aralığı sıfır olan pencerede fiyat hiç oynamamıştır, %K tanımsızdır ve
# ortadaki 50 değeri verilir.
def stochastic(candles: list[Candle], period: int = 14, smooth: int = 3) -> dict[str, Series]:
    """%K ve %D çizgilerini k ve d anahtarlarıyla döndürür."""
    k: Series = [None] * len(candles)
    for i in range(period - 1, len(candles)):
        window = candles[i - period + 1:i + 1]
        hi, lo = max(m.high for m in window), min(m.low for m in window)
        k[i] = 50.0 if hi == lo else (candles[i].close - lo) / (hi - lo) * 100
    known = [x for x in k if x is not None]
    d: Series = [None] * (len(k) - len(known)) + sma(known, smooth)
    return {"k": k, "d": d}


# VWAP verilen mumların ilkinden başlayarak birikir. Hangi andan itibaren
# hesaplanacağını çağıran mum listesini keserek seçer.
def vwap(candles: list[Candle]) -> Series:
    out: Series = []
    pv = vol = 0.0
    for m in candles:
        pv += (m.high + m.low + m.close) / 3 * m.volume
        vol += m.volume
        out.append(pv / vol if vol else None)
    return out
