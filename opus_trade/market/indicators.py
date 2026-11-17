"""
file: indicators.py
importable: SERIES, summary, as_text, as_line, series, rounded

series() bir mum listesinden adı verilen göstergenin bütün değerlerini,
istenen periyotlarla hesaplar. SERIES hesaplanabilen göstergelerin adları ve
varsayılan periyotlarıdır. summary() her çağrıda Opus'a giden sabit
ayarlı gösterge özetini kurar, as_text() bu özeti Opus'un gördüğü satırlara
çevirir. rounded() bir değeri büyüklüğüne göre yeterli basamakla yuvarlar.
Mumlar eskiden yeniye sıralı olmalıdır.

description: Opus'a gönderilen ve araçla hesaplatılan teknik göstergelerin hesaplandığı modül.
"""

# NOTE: Hesaplar bilerek saf Python ile yazıldı. Mum sayısı birkaç bini
# geçmediği için numpy ya da pandas gibi ağır bir bağımlılığa gerek yok.
# Formüller TradingView'deki varsayılanlarla aynıdır, RSI ve ATR Wilder
# yumuşatmasını kullanır.
#
# NOTE: Her seri mum listesiyle aynı uzunluktadır. Gösterge henüz
# hesaplanamayan baştaki mumlarda None döner, böylece değerler mumların
# zamanıyla birebir eşleşir.
#
# XXX: Opus özet göstergeleri _LABELS içindeki Türkçe adlarla, SERIES
# içindeki adları da araç parametresi olarak görür. Bir ad değişirse Opus'a
# giden istem ve araç tanımı değişir, istem değiştiği için profillerin dönemi
# sıfırlanmak zorunda kalır.

import math

_LABELS = {
    "close": "kapanis", "ema20": "ema20", "ema50": "ema50", "rsi14": "rsi14", "macd": "macd",
    "macd_signal": "macd_sinyal", "macd_hist": "macd_histogram", "atr14": "atr14",
    "bb_upper": "bollinger_ust", "bb_lower": "bollinger_alt", "high_20": "son_20_mum_en_yuksek",
    "low_20": "son_20_mum_en_dusuk", "ema200": "ema200",
}

# Opus'un araçla isteyebileceği göstergeler ve varsayılan periyotları.
# Birden çok periyot alan göstergelerde sıra parametre sırasıdır.
SERIES = {
    "ema": (20,), "sma": (20,), "rsi": (14,), "atr": (14,), "bollinger": (20, 2),
    "macd": (12, 26, 9), "stokastik": (14, 3), "vwap": (),
}

Series = list[float | None]


def rounded(v: float) -> float:
    """Değeri büyüklüğüne göre en az altı anlamlı basamakla yuvarlar.

    BTC'de iki basamak yeterliyken XRP'de ATR gibi küçük değerler dört beş
    basamak ister.
    """
    if v == 0 or not math.isfinite(v):
        return v
    return round(v, max(2, 5 - int(math.floor(math.log10(abs(v))))))


# --- Seriler ---

def _ema(values: list[float], period: int) -> list[float]:
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def _sma(values: list[float], period: int) -> Series:
    out: Series = [None] * len(values)
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= period:
            total -= values[i - period]
        if i >= period - 1:
            out[i] = total / period
    return out


# Wilder yumuşatması: ilk ortalama basit ortalamadır, sonra her adımda bir
# önceki ortalamanın periyot eksi bir katına yeni değer eklenip periyoda
# bölünür. Seri girdiyle aynı uzunlukta döner.
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


def _rsi(closes: list[float], period: int) -> Series:
    diffs = [b - a for a, b in zip(closes, closes[1:])]
    gain = _wilder([max(d, 0) for d in diffs], period)
    loss = _wilder([max(-d, 0) for d in diffs], period)
    out: Series = [None]
    for g, l in zip(gain, loss):
        out.append(None if g is None else 100.0 if l == 0 else 100 - 100 / (1 + g / l))
    return out


def _atr(candles: list[dict], period: int) -> Series:
    tr = [max(m["h"] - m["l"], abs(m["h"] - p["c"]), abs(m["l"] - p["c"])) for p, m in zip(candles, candles[1:])]
    return [None] + _wilder(tr, period)


def _bollinger(closes: list[float], period: int, width: float) -> dict[str, Series]:
    mid = _sma(closes, period)
    upper: Series = [None] * len(closes)
    lower: Series = [None] * len(closes)
    for i, m in enumerate(mid):
        if m is None:
            continue
        window = closes[i - period + 1:i + 1]
        dev = (sum((x - m) ** 2 for x in window) / period) ** 0.5
        upper[i], lower[i] = m + width * dev, m - width * dev
    return {"orta": mid, "ust": upper, "alt": lower}


def _macd(closes: list[float], fast: int, slow: int, signal: int) -> dict[str, Series]:
    line = [a - b for a, b in zip(_ema(closes, fast), _ema(closes, slow))]
    sig = _ema(line, signal)
    return {"macd": line, "sinyal": sig, "histogram": [a - b for a, b in zip(line, sig)]}


def _stochastic(candles: list[dict], period: int, smooth: int) -> dict[str, Series]:
    k: Series = [None] * len(candles)
    for i in range(period - 1, len(candles)):
        window = candles[i - period + 1:i + 1]
        hi, lo = max(m["h"] for m in window), min(m["l"] for m in window)
        k[i] = 50.0 if hi == lo else (candles[i]["c"] - lo) / (hi - lo) * 100
    known = [x for x in k if x is not None]
    d_tail = _sma(known, smooth)
    d: Series = [None] * (len(k) - len(known)) + d_tail
    return {"k": k, "d": d}


# VWAP verilen mumların ilkinden başlayarak birikir. Opus hangi andan
# itibaren istediğini mum sayısıyla seçer.
def _vwap(candles: list[dict]) -> Series:
    out: Series = []
    pv = vol = 0.0
    for m in candles:
        typical = (m["h"] + m["l"] + m["c"]) / 3
        pv += typical * m["v"]
        vol += m["v"]
        out.append(pv / vol if vol else None)
    return out


def series(name: str, candles: list[dict], periods: tuple = ()) -> dict[str, Series]:
    """Adı verilen göstergenin bütün değerlerini hesaplar.

    Args:
        name: SERIES içindeki adlardan biri.
        candles: Eskiden yeniye mumlar.
        periods: Göstergenin periyotları. Eksik kalanlar varsayılanla doldurulur.

    Returns:
        Serinin adından değerlere sözlük. Tek çizgili göstergelerde tek anahtar
        vardır, çok çizgililerde her çizgi ayrı anahtardır.

    Raises:
        ValueError: Gösterge bilinmiyorsa ya da periyot geçersizse.
    """
    if name not in SERIES:
        raise ValueError(f"Bilinmeyen gösterge {name}. Geçerli olanlar: {', '.join(SERIES)}")
    p = tuple(periods) + SERIES[name][len(periods):]
    if any(x <= 0 for x in p):
        raise ValueError("Periyotlar sıfırdan büyük olmalı.")
    closes = [m["c"] for m in candles]
    if name == "ema":
        return {f"ema{p[0]}": _ema(closes, int(p[0]))}
    if name == "sma":
        return {f"sma{p[0]}": _sma(closes, int(p[0]))}
    if name == "rsi":
        return {f"rsi{p[0]}": _rsi(closes, int(p[0]))}
    if name == "atr":
        return {f"atr{p[0]}": _atr(candles, int(p[0]))}
    if name == "bollinger":
        return _bollinger(closes, int(p[0]), float(p[1]))
    if name == "macd":
        return _macd(closes, int(p[0]), int(p[1]), int(p[2]))
    if name == "stokastik":
        return _stochastic(candles, int(p[0]), int(p[1]))
    return {"vwap": _vwap(candles)}


# --- Özet ---

def summary(candles: list[dict]) -> dict:
    """Mum listesinin sabit ayarlı teknik gösterge özetini döndürür.

    Args:
        candles: Her biri o, h, l, c, v anahtarlı mumlar. En az iki yüz mum önerilir.

    Returns:
        Gösterge adından değere sözlük. Değerler rounded() ile yuvarlanmıştır.
    """
    c = [m["c"] for m in candles]
    macd = _macd(c, 12, 26, 9)
    bb = _bollinger(c, 20, 2)
    out = {
        "close": c[-1],
        "ema20": _ema(c, 20)[-1],
        "ema50": _ema(c, 50)[-1],
        "rsi14": _rsi(c, 14)[-1],
        "macd": macd["macd"][-1],
        "macd_signal": macd["sinyal"][-1],
        "macd_hist": macd["histogram"][-1],
        "atr14": _atr(candles, 14)[-1],
        "bb_upper": bb["ust"][-1],
        "bb_lower": bb["alt"][-1],
        "high_20": max(m["h"] for m in candles[-20:]),
        "low_20": min(m["l"] for m in candles[-20:]),
    }
    # EMA200 ancak yeterli mum varsa anlamlıdır, az mumla hesaplanan değer yanıltıcı olur.
    if len(c) >= 200:
        out["ema200"] = _ema(c, 200)[-1]
    return {k: rounded(v) for k, v in out.items() if v is not None}


def as_text(s: dict) -> str:
    """summary() çıktısını Opus'un gördüğü ad: değer satırlarına çevirir."""
    return "\n".join(f"{_LABELS[k]}: {v}" for k, v in s.items())


def as_line(s: dict) -> str:
    """summary() çıktısını parite kartında kullanılan tek satıra çevirir."""
    return ", ".join(f"{_LABELS[k]} {v}" for k, v in s.items())
