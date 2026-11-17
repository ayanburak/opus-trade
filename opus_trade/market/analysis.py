"""
file: analysis.py
importable: levels, volume_profile, correlation

Mum listelerinden fiyat yapısı çıkaran hesaplar. levels() tepe ve dipleri
bulup birbirine yakın olanları destek ve direnç seviyelerinde toplar.
volume_profile() hacmin hangi fiyat aralıklarında yoğunlaştığını hesaplar.
correlation() birden çok paritenin getirileri arasındaki korelasyonu verir.
Mumlar eskiden yeniye sıralı olmalıdır.

description: Destek ve direnç, hacim profili ve korelasyon hesaplarının yapıldığı modül.
"""

# NOTE: Bu hesaplar yorum yapmaz, yalnızca ölçer. Bir seviyenin önemli olup
# olmadığına ya da korelasyonun ne anlama geldiğine Opus karar verir.
#
# NOTE: Hesaplar indicators.py gibi saf Python'dur. En büyük girdi bin beş
# yüz mum ve birkaç paritedir, numpy gerekmez.

import math

from .indicators import series

# --- Destek ve direnç ---


# Bir mumun yükseği, iki yanındaki window kadar mumun hepsinden büyükse
# tepe, düşüğü hepsinden küçükse dip sayılır. Son window mum henüz iki
# yanı da görülmediği için değerlendirilmez.
def _pivots(candles: list[dict], window: int) -> list[tuple[int, float]]:
    found = []
    for i in range(window, len(candles) - window):
        around = candles[i - window:i] + candles[i + 1:i + window + 1]
        if candles[i]["h"] > max(m["h"] for m in around):
            found.append((candles[i]["t"], candles[i]["h"]))
        if candles[i]["l"] < min(m["l"] for m in around):
            found.append((candles[i]["t"], candles[i]["l"]))
    return found


def levels(candles: list[dict], window: int = 5) -> list[dict]:
    """Tepe ve dipleri ATR'nin yarısından yakın olanları birleştirerek seviyelere toplar.

    Args:
        candles: Eskiden yeniye mumlar, en az elli mum önerilir.
        window: Bir tepenin ya da dibin iki yanında kaç mumdan uç olması gerektiği.

    Returns:
        Fiyata göre sıralı seviyeler. Her seviye price, touches, first ve last
        anahtarlarını taşır. price seviyedeki uçların ortalaması, touches uç
        sayısı, first ve last ilk ve son ucun zamanıdır.
    """
    atr = next(v for v in reversed(series("atr", candles)["atr14"]) if v is not None)
    tolerance = atr / 2
    groups: list[list[tuple[int, float]]] = []
    # Uçlar fiyata göre sıralanır ve grubun ortalamasına yakın olan gruba
    # katılır. Ortalamayla karşılaştırmak, birbirine zincirleme yakın uçların
    # çok geniş tek bir seviyeye yayılmasını önler.
    for t, price in sorted(_pivots(candles, window), key=lambda x: x[1]):
        if groups and price - sum(p for _, p in groups[-1]) / len(groups[-1]) <= tolerance:
            groups[-1].append((t, price))
        else:
            groups.append([(t, price)])
    return [
        {"price": sum(p for _, p in g) / len(g), "touches": len(g),
         "first": min(t for t, _ in g), "last": max(t for t, _ in g)}
        for g in groups
    ]


# --- Hacim profili ---

def volume_profile(candles: list[dict], buckets: int = 24) -> dict:
    """Mumların hacmini fiyat kademelerine dağıtır.

    Her mumun hacmi o mumun düşüğü ile yükseği arasındaki kademelere eşit
    paylaştırılır. Mumun içinde fiyatın nerede ne kadar kaldığı bilinmediği
    için bu bir yaklaşıktır.

    Args:
        candles: Eskiden yeniye mumlar.
        buckets: Fiyat aralığının bölüneceği kademe sayısı.

    Returns:
        rows, poc, value_low ve value_high anahtarlı sözlük. rows her kademenin
        low, high ve volume değeridir. poc en çok hacmin olduğu kademenin
        ortası, value_low ve value_high hacmin yüzde yetmişinin toplandığı
        aralığın sınırlarıdır.
    """
    low, high = min(m["l"] for m in candles), max(m["h"] for m in candles)
    step = (high - low) / buckets or 1
    vols = [0.0] * buckets
    for m in candles:
        a = min(int((m["l"] - low) / step), buckets - 1)
        b = min(int((m["h"] - low) / step), buckets - 1)
        share = m["v"] / (b - a + 1)
        for i in range(a, b + 1):
            vols[i] += share

    # Değer alanı en yoğun kademeden başlayıp her adımda iki yanındaki
    # kademelerden daha yoğun olanı ekleyerek toplam hacmin yüzde yetmişine
    # ulaşana kadar genişletilir.
    top = max(range(buckets), key=vols.__getitem__)
    lo = hi = top
    total, covered = sum(vols), vols[top]
    while covered < total * 0.7 and (lo > 0 or hi < buckets - 1):
        down = vols[lo - 1] if lo > 0 else -1
        up = vols[hi + 1] if hi < buckets - 1 else -1
        if up >= down:
            hi += 1
            covered += vols[hi]
        else:
            lo -= 1
            covered += vols[lo]
    return {
        "rows": [{"low": low + i * step, "high": low + (i + 1) * step, "volume": v} for i, v in enumerate(vols)],
        "poc": low + (top + 0.5) * step, "value_low": low + lo * step, "value_high": low + (hi + 1) * step,
    }


# --- Korelasyon ---

def _returns(candles: list[dict]) -> dict[int, float]:
    return {m["t"]: math.log(m["c"] / p["c"]) for p, m in zip(candles, candles[1:])}


def correlation(by_symbol: dict[str, list[dict]]) -> dict[tuple[str, str], tuple[float, int]]:
    """Paritelerin logaritmik getirileri arasındaki Pearson korelasyonunu hesaplar.

    Getiriler mum zamanına göre eşleştirilir, yalnızca iki paritede de olan
    mumlar kullanılır. Böylece farklı saatlerde işlem gören varlıklar da
    karşılaştırılabilir.

    Args:
        by_symbol: Sembolden mum listesine sözlük.

    Returns:
        Sembol çiftinden korelasyon ve kullanılan ortak mum sayısına sözlük.
    """
    returns = {s: _returns(c) for s, c in by_symbol.items()}
    names = list(returns)
    out = {}
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            common = sorted(returns[a].keys() & returns[b].keys())
            x = [returns[a][t] for t in common]
            y = [returns[b][t] for t in common]
            out[(a, b)] = (_pearson(x, y), len(common))
    return out


def _pearson(x: list[float], y: list[float]) -> float:
    n = len(x)
    if n < 3:
        return float("nan")
    mx, my = sum(x) / n, sum(y) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(x, y))
    vx = sum((a - mx) ** 2 for a in x)
    vy = sum((b - my) ** 2 for b in y)
    return cov / math.sqrt(vx * vy) if vx and vy else float("nan")
