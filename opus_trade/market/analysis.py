"""
file: analysis.py
importable: Swing, swings, Level, levels, VolumeProfile, volume_profile, correlation

Mum listelerinden fiyat yapısı çıkaran hesaplar. swings() tepe ve dipleri
zaman sırasıyla verir, piyasanın yapısı onlardan okunur. levels() aynı uçları
bulup birbirine yakın olanları destek ve direnç seviyelerinde toplar.
volume_profile() hacmin hangi fiyat aralıklarında yoğunlaştığını hesaplar.
correlation() birden çok paritenin getirileri arasındaki korelasyonu verir.
Mumlar eskiden yeniye sıralı olmalıdır.

description: Destek ve direnç, hacim profili ve korelasyon hesapları.
"""

# NOTE: Bu hesaplar yorum yapmaz, yalnızca ölçer. Bir seviyenin önemli olup
# olmadığına strateji karar verir.

import math
from dataclasses import dataclass

from .indicators import atr
from .models import Candle

# --- Tepe ve dipler ---


@dataclass(frozen=True)
class Swing:
    """Bir tepe ya da dip. kind high ya da low olur."""

    time: int
    price: float
    kind: str


# Bir mumun yükseği iki yanındaki window kadar mumun hepsinden büyükse tepe,
# düşüğü hepsinden küçükse dip sayılır. Sağında window kadar mum olmayan
# mumlar değerlendirilmez. Son mum henüz kapanmamış olabilir, sağ yanı
# görülmemiş bir uç sonradan geçersiz kalabilirdi. Bu yüzden en yeni uç en az
# window mum önce oluşmuş olur. Aynı mum hem tepe hem dip olabilir, örneğin
# çok geniş bir mum, ikisi de sırayla yazılır.
def swings(candles: list[Candle], window: int = 5) -> list[Swing]:
    """Mumların tepe ve diplerini eskiden yeniye döndürür.

    Args:
        candles: Eskiden yeniye mumlar.
        window: Bir tepenin ya da dibin iki yanında kaç mumdan uç olması gerektiği.
    """
    found = []
    for i in range(window, len(candles) - window):
        around = candles[i - window:i] + candles[i + 1:i + window + 1]
        if candles[i].high > max(m.high for m in around):
            found.append(Swing(candles[i].time, candles[i].high, "high"))
        if candles[i].low < min(m.low for m in around):
            found.append(Swing(candles[i].time, candles[i].low, "low"))
    return found


# --- Destek ve direnç ---


@dataclass(frozen=True)
class Level:
    """Bir destek ya da direnç seviyesi.

    Args:
        price: Seviyedeki uçların ortalaması.
        touches: Seviyeye düşen tepe ve dip sayısı.
        first: İlk ucun zamanı.
        last: Son ucun zamanı.
    """

    price: float
    touches: int
    first: int
    last: int


# Uçlar fiyata göre sıralanır ve grubun ortalamasına yarım ATR'den yakınsa
# o gruba katılır. Komşu uçla değil ortalamayla karşılaştırmak, birbirine
# zincirleme yakın uçların çok geniş tek bir seviyeye yayılmasını önler.
def levels(candles: list[Candle], window: int = 5) -> list[Level]:
    """Tepe ve dipleri seviyelere toplar, fiyata göre sıralı döndürür.

    Args:
        candles: Eskiden yeniye mumlar, ATR için en az on beş, anlamlı sonuç için elli kadar.
        window: Bir tepenin ya da dibin iki yanında kaç mumdan uç olması gerektiği.
    """
    known = [v for v in atr(candles) if v is not None]
    if not known:
        return []
    tolerance = known[-1] / 2
    groups: list[list[tuple[int, float]]] = []
    for t, price in sorted(((x.time, x.price) for x in swings(candles, window)), key=lambda x: x[1]):
        if groups and price - sum(p for _, p in groups[-1]) / len(groups[-1]) <= tolerance:
            groups[-1].append((t, price))
        else:
            groups.append([(t, price)])
    return [Level(price=sum(p for _, p in g) / len(g), touches=len(g),
                  first=min(t for t, _ in g), last=max(t for t, _ in g)) for g in groups]


# --- Hacim profili ---

@dataclass(frozen=True)
class VolumeProfile:
    """Hacmin fiyat kademelerine dağılımı.

    Args:
        rows: Her kademenin alt sınırı, üst sınırı ve hacmi.
        poc: En çok hacmin olduğu kademenin ortası.
        value_low: Hacmin yüzde yetmişinin toplandığı aralığın alt sınırı.
        value_high: O aralığın üst sınırı.
    """

    rows: list[tuple[float, float, float]]
    poc: float
    value_low: float
    value_high: float


# Her mumun hacmi düşüğü ile yükseği arasındaki kademelere eşit paylaştırılır.
# Mumun içinde fiyatın nerede ne kadar kaldığı bilinmediği için bu bir
# yaklaşıktır.
def volume_profile(candles: list[Candle], buckets: int = 24) -> VolumeProfile:
    low, high = min(m.low for m in candles), max(m.high for m in candles)
    step = (high - low) / buckets or 1
    vols = [0.0] * buckets
    for m in candles:
        a = min(int((m.low - low) / step), buckets - 1)
        b = min(int((m.high - low) / step), buckets - 1)
        share = m.volume / (b - a + 1)
        for i in range(a, b + 1):
            vols[i] += share

    # Değer alanı en yoğun kademeden başlar ve her adımda iki yanındaki
    # kademelerden yoğun olanı ekleyerek toplam hacmin yüzde yetmişine
    # ulaşana kadar genişler.
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
    return VolumeProfile(rows=[(low + i * step, low + (i + 1) * step, v) for i, v in enumerate(vols)],
                         poc=low + (top + 0.5) * step, value_low=low + lo * step, value_high=low + (hi + 1) * step)


# --- Korelasyon ---

def _returns(candles: list[Candle]) -> dict[int, float]:
    return {m.time: math.log(m.close / p.close) for p, m in zip(candles, candles[1:])}


def _pearson(x: list[float], y: list[float]) -> float:
    n = len(x)
    if n < 3:
        return float("nan")
    mx, my = sum(x) / n, sum(y) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(x, y))
    vx = sum((a - mx) ** 2 for a in x)
    vy = sum((b - my) ** 2 for b in y)
    return cov / math.sqrt(vx * vy) if vx and vy else float("nan")


# Getiriler mum zamanına göre eşleştirilir ve yalnızca iki paritede de olan
# mumlar kullanılır. Böylece altın gibi hafta sonu işlem görmeyen bir varlık
# da kripto paritelerle karşılaştırılabilir.
def correlation(by_symbol: dict[str, list[Candle]]) -> dict[tuple[str, str], tuple[float, int]]:
    """Paritelerin logaritmik getirileri arasındaki Pearson korelasyonunu hesaplar.

    Args:
        by_symbol: Sembolden mum listesine sözlük.

    Returns:
        Sembol çiftinden korelasyon ve kullanılan ortak mum sayısına sözlük. Ortak mum üçten azsa korelasyon nan.
    """
    returns = {s: _returns(c) for s, c in by_symbol.items()}
    names = list(returns)
    out = {}
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            common = sorted(returns[a].keys() & returns[b].keys())
            out[(a, b)] = (_pearson([returns[a][t] for t in common], [returns[b][t] for t in common]), len(common))
    return out
