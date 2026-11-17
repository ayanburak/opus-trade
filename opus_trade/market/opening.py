"""
file: opening.py
importable: history, next_open, index_moves, volatility

NY borsası açılışına bağlı ölçümler. history() geçmiş işlem günlerinin
açılışlarındaki avları ölçer. next_open() bir sonraki açılış anını,
index_moves() Nasdaq ve S&P vadelilerinin açılış öncesi hareketini,
volatility() bir paritenin önceki 24 saatteki 1 dakikalık ortalama aralığını
verir. Av, açılıştan sonraki ilk 15 dakikada fiyatın açılış fiyatından en
uzağa gittiği noktadır. Metne çevirme işi piyasa araçlarındadır.

description: NY borsası açılışındaki av geçmişini ve açılış öncesi durumu ölçen modül.
"""

# NOTE: Ölçümlerin tanımı .claude/research/ny-acilis-avi.md raporundaki
# araştırmayla aynıdır. Nasdaq hareketi Binance'in QQQUSDT vadelisinden
# alınır, raporda bu vadelinin Nasdaq vadelisiyle 0,98 korelasyonlu olduğu
# ölçüldü. Bir tanım değişirse rapordaki sonuçlarla karşılaştırma bozulur.
#
# NOTE: Geçmiş açılışların sonucu değişmediği için history() her günün
# ölçümünü süreç boyunca saklar. Piyasa araçları her çağrıda ayrı bir süreçte
# açıldığı için bu saklama çağrı bitince kaybolur.
#
# XXX: history() her gün için iki tane bin beş yüz mumluk istek atar, yirmi
# günlük bir bakış Binance'in dakikalık istek ağırlığı sınırının altıda biri
# kadar tutar. Araçlar sık çağrılmaya başlarsa saklamanın süreç dışına,
# örneğin market.db'ye taşınması gerekebilir.

import asyncio
import statistics
import time
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from . import data as market_data
from . import hours as market_hours

_NY = ZoneInfo("America/New_York")
_NYSE = market_hours.MARKETS[0]

INDEX = (("QQQUSDT", "Nasdaq vadelisi"), ("SPYUSDT", "S&P vadelisi"))
_HUNT_MIN = 15

_cache: dict[tuple[str, date], dict | None] = {}


# --- Takvim ---

# NYSE'nin açık olduğu bir günün açılış anı. Yarım günlerde de açılış 09:30'dur.
def _open(d: date) -> int | None:
    if market_hours._day_kind(_NYSE, d) not in ("open", "half"):
        return None
    return int(datetime.combine(d, dtime(9, 30), _NY).timestamp())


def next_open(now: float) -> int:
    d = datetime.fromtimestamp(now, _NY).date()
    while True:
        o = _open(d)
        if o and o > now:
            return o
        d += timedelta(days=1)


def _prev_close(now: float) -> int:
    d = datetime.fromtimestamp(now, _NY).date()
    while True:
        o = _open(d)
        close = int(datetime.combine(d, dtime(16, 0), _NY).timestamp()) if o else None
        if close and close <= now:
            return close
        d -= timedelta(days=1)


# --- Geçmiş ---

# Bir günün ölçümü. Açılıştan 24 saat öncesinden açılıştan bir saat sonrasına
# kadar 1500 dakikalık mum tek istekte gelir. Gün henüz tamamlanmadıysa ya da
# parite o gün listede değilse None döner ve saklanmaz.
async def _day(symbol: str, d: date, o: int) -> dict | None:
    key = (symbol, d)
    if key in _cache:
        return _cache[key]
    raw, idx = await asyncio.gather(
        market_data.request("/fapi/v1/klines", symbol=symbol, interval="1m", startTime=(o - 86400) * 1000, limit=1500),
        market_data.request("/fapi/v1/klines", symbol="QQQUSDT", interval="1m", startTime=(o - 23400) * 1000, limit=361),
    )
    if len(raw) < 1500 or raw[0][0] // 1000 != o - 86400 or len(idx) < 361:
        return None
    k = [(float(m[1]), float(m[2]), float(m[3]), float(m[4])) for m in raw]
    p0, pre, first = k[1440][0], k[:1440], k[1440:1440 + _HUNT_MIN]
    up = max(m[1] for m in first) / p0 - 1
    dn = 1 - min(m[2] for m in first) / p0
    side = 1 if up > dn else -1
    peak = max(range(_HUNT_MIN), key=lambda i: first[i][1]) if side > 0 else min(range(_HUNT_MIN), key=lambda i: first[i][2])
    ext = first[peak][1] if side > 0 else first[peak][2]
    after = k[1440 + peak + 1:]
    back = (ext - min(m[2] for m in after)) if side > 0 else (max(m[1] for m in after) - ext)
    out = {
        "date": d, "nasdaq_6h": float(idx[-1][1]) / float(idx[0][1]) - 1, "side": side, "size": max(up, dn),
        "peak_min": peak + 1, "giveback": back / abs(ext - p0) if ext != p0 else 0.0,
        "vol": statistics.mean(m[1] - m[2] for m in pre) / p0,
    }
    _cache[key] = out
    return out


async def history(symbol: str, days: int) -> list[dict]:
    """Son işlem günlerinin açılış avlarını yeniden eskiye döndürür.

    Args:
        symbol: Binance USDT vadeli sembolü.
        days: Kaç işlem günü geriye bakılacağı.

    Returns:
        Her gün için date, nasdaq_6h, side, size, peak_min, giveback ve vol
        anahtarlı sözlük. nasdaq_6h QQQUSDT'nin 03:00-09:00 NY hareketidir.
        side yukarı av için 1, aşağı için -1. giveback avın açılıştan sonraki
        ilk saat içinde geri verilen oranı, vol önceki 24 saatin 1 dakikalık
        ortalama aralığının fiyata oranıdır. Açılıştan sonra bir saat
        geçmemiş gün dahil edilmez.
    """
    now = time.time()
    d = datetime.fromtimestamp(now, _NY).date()
    picked = []
    while len(picked) < days:
        o = _open(d)
        if o and o + 3600 <= now:
            picked.append((d, o))
        d -= timedelta(days=1)
    found = await asyncio.gather(*(_day(symbol, d, o) for d, o in picked))
    return [x for x in found if x]


# --- Açılış öncesi ---

# Geçmiş bir andaki fiyat o dakikanın mumunun açılışıdır. Şu anki fiyat ise
# son kapanmış dakikanın kapanışıdır. İçinde bulunulan dakikanın mumunu
# Binance dakikanın ilk saniyelerinde çoğu zaman henüz vermiyor, eskiden bu
# yüzden liste boş dönüp hesap düşüyordu. Mum yine de gelmezse None döner.
async def _price(symbol: str, t: float, latest: bool = False) -> float | None:
    minute = int(t) // 60 * 60 - (60 if latest else 0)
    k = await market_data.request("/fapi/v1/klines", symbol=symbol, interval="1m", startTime=minute * 1000, limit=1)
    if not k:
        return None
    return float(k[0][4] if latest else k[0][1])


def _change(a: float | None, b: float | None) -> float | None:
    return b / a - 1 if a and b else None


async def index_moves(now: float) -> list[dict]:
    """Nasdaq ve S&P vadelilerinin açılış öncesi hareketini döndürür.

    Returns:
        Her vadeli için symbol, name, six_hours ve since_close anahtarlı
        sözlük. six_hours son altı saatteki, since_close önceki NY
        kapanışından bu yana değişimdir. Fiyatı alınamayan değer None olur.
    """
    close = _prev_close(now)
    out = []
    for sym, name in INDEX:
        six, last_close, current = await asyncio.gather(
            _price(sym, now - 21600), _price(sym, close), _price(sym, now, latest=True))
        out.append({"symbol": sym, "name": name, "six_hours": _change(six, current),
                    "since_close": _change(last_close, current)})
    return out


async def volatility(symbol: str) -> float:
    """Önceki 24 saatin 1 dakikalık ortalama aralığının son fiyata oranını döndürür."""
    k = await market_data.candles("1m", 1440, symbol)
    return statistics.mean(m["h"] - m["l"] for m in k) / k[-1]["c"]
