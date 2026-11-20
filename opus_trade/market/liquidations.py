"""
file: liquidations.py
importable: WAVE_GAP_S, LiquidationWaveRow, open_readonly, totals, waves

Toplayıcının deposundaki likidasyonları okur. open_readonly() market.db'yi
salt okunur açar, dosya henüz yoksa boş bir bellek veritabanı döndürür.
totals() bir paritede verilen andan beri likide olan long ve short
tarafların toplamını, waves() kayıtları zamanda birbirine yakın dalgalara
ayırıp döndürür.

description: Toplanan likidasyon kayıtlarının okunması ve dalgalara ayrılması.
"""

# NOTE: Yazan taraf toplayıcıdır, bu modül hiç yazmaz. Binance akışta her
# parite için saniyede yalnızca en büyük likidasyonu gönderir, bu yüzden
# toplamlar gerçek tutarın bir örneklemidir.

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

# Aralarında bundan uzun boşluk olmayan kayıtlar aynı dalga sayılır.
WAVE_GAP_S = 120


@dataclass
class LiquidationWaveRow:
    start: int
    end: int
    count: int = 0
    long_usd: float = 0.0
    short_usd: float = 0.0
    low: float = float("inf")
    high: float = 0.0
    largest: dict = field(default_factory=dict)


def open_readonly(path: Path) -> sqlite3.Connection:
    if not path.exists():
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE liquidations (time INTEGER, symbol TEXT, side TEXT, price REAL, qty REAL)")
        return con
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def totals(con: sqlite3.Connection, symbol: str, since: int) -> dict[str, tuple[int, float]]:
    """Long ve short taraf için kayıt sayısını ve dolar toplamını döndürür."""
    out = {"long": (0, 0.0), "short": (0, 0.0)}
    for side, n, usd in con.execute("SELECT side, COUNT(*), SUM(price * qty) FROM liquidations "
                                    "WHERE symbol = ? AND time >= ? GROUP BY side", (symbol, since)):
        out[side] = (n, usd)
    return out


def waves(con: sqlite3.Connection, symbol: str, since: int) -> list[LiquidationWaveRow]:
    """Bir paritedeki likidasyonları eskiden yeniye dalgalar halinde döndürür."""
    found: list[LiquidationWaveRow] = []
    for t, side, price, qty in con.execute("SELECT time, side, price, qty FROM liquidations "
                                           "WHERE symbol = ? AND time >= ? ORDER BY time", (symbol, since)):
        if not found or t - found[-1].end > WAVE_GAP_S:
            found.append(LiquidationWaveRow(start=t, end=t))
        w, usd = found[-1], price * qty
        w.end, w.count = t, w.count + 1
        if side == "long":
            w.long_usd += usd
        else:
            w.short_usd += usd
        w.low, w.high = min(w.low, price), max(w.high, price)
        if usd > w.largest.get("usd", 0):
            w.largest = {"side": side, "price": price, "usd": usd}
    return found
