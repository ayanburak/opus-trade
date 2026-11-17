"""
file: liquidations.py
importable: connect, record, purge, waves, totals

Binance'in likidasyon akışından gelen kayıtları bütün profillerin ortak
kullandığı market veritabanında tutar. connect() veritabanını açar, record()
bir kaydı yazar, purge() saklama süresini aşan kayıtları siler. waves() bir
paritedeki kayıtları zamanda birbirine yakın dalgalara ayırır, totals() bir
dönemdeki long ve short likidasyonlarının toplamını verir.

description: Likidasyon kayıtlarının saklandığı ve özetlendiği ortak piyasa veritabanı katmanı.
"""

# NOTE: Binance bu akışta her parite için saniyede yalnızca en büyük
# likidasyonu gönderir. Bir dalgada aynı saniyede olan diğerleri hiç gelmez.
# Bu yüzden kayıtlar toplam tutarı değil, dalgaların zamanını, yönünü ve
# büyüklük sırasını doğru gösterir. Opus'a da örneklem olduğu söylenir.
#
# NOTE: Kayıt yalnızca program açıkken tutulur, geçmiş veri Binance'ten
# sonradan çekilemez. Program kapalı kaldığı aralıklar boş görünür.
#
# NOTE: side alanı likide olan pozisyonun yönüdür. Akıştaki SELL emri bir
# long'un, BUY emri bir short'un zorla kapatılmasıdır.

import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS liquidations (
    time    INTEGER NOT NULL,
    symbol  TEXT NOT NULL,
    side    TEXT NOT NULL,
    price   REAL NOT NULL,
    qty     REAL NOT NULL,
    usd     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS liquidations_symbol_time ON liquidations (symbol, time);
"""

# Aralarında bundan uzun boşluk olmayan kayıtlar aynı dalga sayılır.
WAVE_GAP_S = 120



def connect(path: Path, readonly: bool = False) -> sqlite3.Connection:
    """Market veritabanını açar, yazılabilir açılıyorsa tabloyu kurar.

    Args:
        path: Veritabanı dosyası.
        readonly: True ise salt okunur açılır. Dosya henüz yoksa boş bir bellek veritabanı döner.
    """
    if readonly:
        if not path.exists():
            con = sqlite3.connect(":memory:")
            con.executescript(_SCHEMA)
        else:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(path)
        con.executescript(_SCHEMA)
    con.row_factory = sqlite3.Row
    return con


def record(con: sqlite3.Connection, event: dict) -> None:
    """Akıştan gelen bir likidasyonu yazar.

    Args:
        event: time, symbol, side, price, qty ve usd anahtarlı kayıt.
    """
    con.execute("INSERT INTO liquidations VALUES (:time, :symbol, :side, :price, :qty, :usd)", event)
    con.commit()


def purge(con: sqlite3.Connection, now: int, keep_s: int) -> int:
    """keep_s saniyeden eski kayıtları siler, silinen kayıt sayısını döndürür."""
    cur = con.execute("DELETE FROM liquidations WHERE time < ?", (now - keep_s,))
    con.commit()
    return cur.rowcount


def totals(con: sqlite3.Connection, symbol: str, since: int) -> dict:
    """Bir paritede verilen andan beri likide olan long ve short tarafların toplamını döndürür.

    Returns:
        long ve short anahtarlı sözlük, her biri count ve usd taşır.
    """
    out = {"long": {"count": 0, "usd": 0.0}, "short": {"count": 0, "usd": 0.0}}
    for r in con.execute("SELECT side, COUNT(*) AS n, SUM(usd) AS usd FROM liquidations "
                         "WHERE symbol = ? AND time >= ? GROUP BY side", (symbol, since)):
        out[r["side"]] = {"count": r["n"], "usd": r["usd"]}
    return out


# Kayıtlar zaman sırasıyla gezilir, bir önceki kayıttan WAVE_GAP_S'den uzun
# süre sonra gelen kayıt yeni bir dalga başlatır.
def waves(con: sqlite3.Connection, symbol: str, since: int) -> list[dict]:
    """Bir paritedeki likidasyonları zamanda yakın dalgalara ayırır.

    Returns:
        Eskiden yeniye dalgalar. Her dalga start, end, count, long_usd,
        short_usd, low, high ve largest anahtarlarını taşır. largest dalganın
        en büyük kaydıdır.
    """
    found: list[dict] = []
    for r in con.execute("SELECT * FROM liquidations WHERE symbol = ? AND time >= ? ORDER BY time",
                         (symbol, since)):
        if not found or r["time"] - found[-1]["end"] > WAVE_GAP_S:
            found.append({"start": r["time"], "end": r["time"], "count": 0, "long_usd": 0.0, "short_usd": 0.0,
                          "low": r["price"], "high": r["price"], "largest": dict(r)})
        w = found[-1]
        w["end"] = r["time"]
        w["count"] += 1
        w[f"{r['side']}_usd"] += r["usd"]
        w["low"], w["high"] = min(w["low"], r["price"]), max(w["high"], r["price"])
        if r["usd"] > w["largest"]["usd"]:
            w["largest"] = dict(r)
    return found
