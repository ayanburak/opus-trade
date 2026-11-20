"""
file: opening.py
importable: Hunt, INDEX, next_open, history, index_moves, volatility

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
# NOTE: Geçmiş açılışların sonucu değişmediği için her günün ölçümü süreç
# boyunca saklanır. Piyasa araçları her çağrıda ayrı bir süreçte açıldığı için
# bu saklama çağrı bitince kaybolur.

import asyncio
import statistics
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from ..domain import sessions
from ..domain.sessions import DayKind
from .source import MarketData

_NY = ZoneInfo("America/New_York")
_NYSE = sessions.exchange("nyse")

INDEX = (("QQQUSDT", "Nasdaq vadelisi"), ("SPYUSDT", "S&P vadelisi"))
_HUNT_MIN = 15


@dataclass(frozen=True)
class Hunt:
    """Bir günün açılış avı.

    Args:
        day: İşlem günü.
        nasdaq_6h: QQQUSDT'nin 03:00-09:00 NY arasındaki değişimi, oran.
        side: Yukarı av için 1, aşağı için -1.
        size: Avın açılış fiyatına göre büyüklüğü, oran.
        peak_min: Avın tepesine kaçıncı dakikada varıldığı.
        giveback: Avın açılıştan sonraki ilk saat içinde geri verilen oranı.
        vol: Önceki 24 saatin 1 dakikalık ortalama aralığının açılış fiyatına oranı.
    """

    day: date
    nasdaq_6h: float
    side: int
    size: float
    peak_min: int
    giveback: float
    vol: float


_cache: dict[tuple[str, date], Hunt] = {}


def _open(d: date) -> int | None:
    if sessions.day_kind(_NYSE, d) not in (DayKind.OPEN, DayKind.HALF):
        return None
    return int(datetime.combine(d, time(9, 30), _NY).timestamp())


def next_open(now: int) -> int:
    d = datetime.fromtimestamp(now, _NY).date()
    while not ((o := _open(d)) and o > now):
        d += timedelta(days=1)
    return o


def _prev_close(now: int) -> int:
    d = datetime.fromtimestamp(now, _NY).date()
    while True:
        close = int(datetime.combine(d, time(16, 0), _NY).timestamp()) if _open(d) else None
        if close and close <= now:
            return close
        d -= timedelta(days=1)


# Açılıştan 24 saat öncesinden bir saat sonrasına kadar 1500 dakikalık mum
# kullanılır. Gün tamamlanmadıysa ya da parite o gün listede değilse None
# döner ve saklanmaz.
async def _day(market: MarketData, symbol: str, d: date, o: int) -> Hunt | None:
    if (symbol, d) in _cache:
        return _cache[(symbol, d)]
    k, idx = await asyncio.gather(market.candles(symbol, "1m", start=o - 86400, end=o + 3600),
                                  market.candles("QQQUSDT", "1m", start=o - 23400, end=o - 23400 + 361 * 60))
    if len(k) < 1500 or k[0].time != o - 86400 or len(idx) < 361:
        return None
    p0, pre, first = k[1440].open, k[:1440], k[1440:1440 + _HUNT_MIN]
    up = max(m.high for m in first) / p0 - 1
    dn = 1 - min(m.low for m in first) / p0
    side = 1 if up > dn else -1
    peak = (max(range(_HUNT_MIN), key=lambda i: first[i].high) if side > 0
            else min(range(_HUNT_MIN), key=lambda i: first[i].low))
    ext = first[peak].high if side > 0 else first[peak].low
    after = k[1440 + peak + 1:]
    back = (ext - min(m.low for m in after)) if side > 0 else (max(m.high for m in after) - ext)
    h = Hunt(day=d, nasdaq_6h=idx[-1].open / idx[0].open - 1, side=side, size=max(up, dn), peak_min=peak + 1,
             giveback=back / abs(ext - p0) if ext != p0 else 0.0,
             vol=statistics.mean(m.high - m.low for m in pre) / p0)
    _cache[(symbol, d)] = h
    return h


async def history(market: MarketData, symbol: str, days: int, now: int) -> list[Hunt]:
    """Açılıştan sonra en az bir saat geçmiş son işlem günlerinin avlarını yeniden eskiye döndürür."""
    d = datetime.fromtimestamp(now, _NY).date()
    picked = []
    while len(picked) < days:
        o = _open(d)
        if o and o + 3600 <= now:
            picked.append((d, o))
        d -= timedelta(days=1)
    found = await asyncio.gather(*(_day(market, symbol, d, o) for d, o in picked))
    return [x for x in found if x]


# Geçmiş bir andaki fiyat o dakikanın açılışıdır. Şu anki fiyat son kapanmış
# dakikanın kapanışıdır, içinde bulunulan dakikanın mumunu Binance ilk
# saniyelerde çoğu zaman henüz vermiyor. Mum yine de gelmezse None döner.
async def _price(market: MarketData, symbol: str, t: int, latest: bool = False) -> float | None:
    minute = t // 60 * 60 - (60 if latest else 0)
    k = await market.candles(symbol, "1m", start=minute, end=minute + 60)
    if not k:
        return None
    return k[0].close if latest else k[0].open


def _change(a: float | None, b: float | None) -> float | None:
    return b / a - 1 if a and b else None


async def index_moves(market: MarketData, now: int) -> list[dict]:
    """Her vadeli için symbol, name, six_hours ve since_close döndürür. Alınamayan değer None olur."""
    close = _prev_close(now)
    out = []
    for sym, name in INDEX:
        six, last_close, current = await asyncio.gather(
            _price(market, sym, now - 21600), _price(market, sym, close), _price(market, sym, now, latest=True))
        out.append({"symbol": sym, "name": name, "six_hours": _change(six, current),
                    "since_close": _change(last_close, current)})
    return out


async def volatility(market: MarketData, symbol: str) -> float:
    """Önceki 24 saatin 1 dakikalık ortalama aralığının son fiyata oranını döndürür."""
    k = await market.candles(symbol, "1m", 1440)
    return statistics.mean(m.high - m.low for m in k) / k[-1].close
