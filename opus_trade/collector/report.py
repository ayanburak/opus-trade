"""
file: report.py
importable: summary, plan_export, render, EXPORTS, FORMATS

summary() market.db'nin durumunu panel için özetler: canlı kaynakların
kesintisiz çalıştığı aralıklar ve aralarındaki boşluklar, her paritenin her
serisinde kayıt sayısı, ilk ve son kayıt, istatistiklerde boşluklar.
plan_export() indirilecek satırların sorgusunu kurar ve isteği doğrular,
render() o sorgunun imlecini CSV ya da JSON parçalarına çevirir.

    sql, args, cols = plan_export("premium", "csv", symbol="BTCUSDT")
    for piece in render(con.execute(sql, args), cols, "csv"):
        ...

description: Toplanan verinin kapsama raporu ve dışa aktarımı.
"""

# NOTE: Boşluk iki türlüdür. Likidasyon ve premium yalnızca canlıdan
# alınabildiği için boşlukları kapsama kaydının iki aralığı arasıdır ve bir
# daha kapanmaz. İstatistiklerin boşluğu verinin kendisinden görülür, beş
# dakikalık iki kayıt arası daha uzunsa arada eksik vardır. Toplayıcı her
# turda en son kayıttan devam ettiği için ortada kalan bir istatistik boşluğu
# kendiliğinden kapanmaz, yalnızca otuz günden eskiye düşünce görünmez olur.
#
# NOTE: Boşluk her yerde [önceki kayıt, sonraki kayıt] olarak döner. Arada
# hiç veri yoktur, süresi ikisinin farkıdır.
#
# NOTE: Bir parite izlenenlerden çıkarılınca verisi silinmez. Verisi olan ama
# izlenmeyen pariteler de listede tracked false ile durur, böylece eski veri
# indirilebilir ve parite yeniden eklenebilir.

import csv
import io
import json
import sqlite3
from typing import Iterator

from ..market.source import RatioKind
from .sources import STATS_PERIOD_S, STATS_SERIES

# İndirilebilen tablolar ve sütunları, tablodaki sırasıyla. Tablo adı sorguya
# metin olarak girdiği için yalnızca bu listedeki adlar kabul edilir.
EXPORTS = {
    "liquidations": ("time", "symbol", "side", "price", "qty"),
    "premium": ("symbol", "time", "mark", "index_price", "est_funding", "next_funding"),
    "open_interest": ("symbol", "time", "qty", "usd"),
    "long_short": ("kind", "symbol", "time", "long_pct", "ratio"),
    "taker_volume": ("symbol", "time", "buy_sell_ratio", "buy_volume", "sell_volume"),
}
FORMATS = ("csv", "json")

# Canlı kaynağın adı ve yazdığı tablo.
_LIVE = (("liquidations", "liquidations"), ("premium", "premium"))


# --- Özet ---

def _gaps(ranges: list[list[int]]) -> list[list[int]]:
    return [[a[1], b[0]] for a, b in zip(ranges, ranges[1:])]


def _stats(con: sqlite3.Connection, table: str, where: str, args: tuple) -> dict:
    rows, first, last = con.execute(f"SELECT COUNT(*), MIN(time), MAX(time) FROM {table} WHERE {where}",
                                    args).fetchone()
    return {"rows": rows, "first": first, "last": last}


# Her seri birincil anahtarın sırasıyla okunur, LAG bir önceki kaydın zamanını
# verir. Tablolar WITHOUT ROWID ve anahtar parite ile zamanla başladığı için
# sorgu ayrı bir sıralama yapmaz.
def _series(con: sqlite3.Connection, symbol: str) -> list[dict]:
    out = [{"series": "premium", **_stats(con, "premium", "symbol = ?", (symbol,)), "gaps": None}]
    for table, kind in STATS_SERIES:
        where, args = ("symbol = ?", (symbol,)) if kind is None else ("symbol = ? AND kind = ?", (symbol, str(kind)))
        gaps = [[p, t] for p, t in con.execute(
            f"SELECT prev, time FROM (SELECT time, LAG(time) OVER (ORDER BY time) AS prev FROM {table} "
            f"WHERE {where}) WHERE time - prev > ?", (*args, STATS_PERIOD_S))]
        name = table if kind is None else f"{table}:{kind}"
        out.append({"series": name, **_stats(con, table, where, args), "gaps": gaps})
    return out


def summary(con: sqlite3.Connection, now: int) -> dict:
    """market.db'nin kapsama raporunu döndürür.

    Args:
        con: market.db'ye bağlantı, salt okunur olabilir.
        now: Raporun anı. Panel son kaydın ne kadar eski olduğunu buna göre gösterir.

    Returns:
        now, live ve symbols alanlı bir sözlük. live her canlı kaynağın kayıt
        sayısını, ilk ve son kaydını, çalıştığı aralıkları ve boşluklarını
        taşır. symbols önce izlenen, sonra yalnızca eski verisi olan
        paritelerdir, her biri serilerinin özetiyle gelir.
    """
    live = []
    for source, table in _LIVE:
        ranges = [[s, e] for s, e in con.execute(
            "SELECT start_time, end_time FROM coverage WHERE source = ? ORDER BY start_time", (source,))]
        live.append({"source": source, **_stats(con, table, "1", ()), "ranges": ranges, "gaps": _gaps(ranges)})
    tracked = {r[0]: r[1] for r in con.execute("SELECT symbol, added_at FROM tracked_symbols")}
    stored = {r[0] for r in con.execute("SELECT DISTINCT symbol FROM premium UNION "
                                        "SELECT DISTINCT symbol FROM open_interest")}
    order = sorted(tracked) + sorted(stored - tracked.keys())
    symbols = [{"symbol": s, "tracked": s in tracked, "added_at": tracked.get(s), "series": _series(con, s)}
               for s in order]
    return {"now": now, "live": live, "symbols": symbols}


# --- Dışa aktarım ---

def plan_export(table: str, fmt: str, symbol: str | None = None, kind: str | None = None,
                start: int | None = None, end: int | None = None) -> tuple[str, tuple, tuple[str, ...]]:
    """İndirilecek satırların sorgusunu kurar. Satırlar zamana göre sıralı gelir.

    Args:
        table: EXPORTS'taki tablolardan biri.
        fmt: csv ya da json. Sorguyu değiştirmez, indirme başlamadan doğrulansın diye burada denetlenir.
        symbol: Verilirse yalnızca bu parite.
        kind: Yalnızca long_short'ta, verilirse yalnızca bu oran türü.
        start: Verilirse bu andan itibaren, dahil.
        end: Verilirse bu ana kadar, hariç.

    Returns:
        Sorgu, parametreleri ve sütun adları.

    Raises:
        ValueError: Tablo, biçim ya da tür bilinmiyorsa, ya da aralık boşsa.
    """
    if fmt not in FORMATS:
        raise ValueError(f"Bilinmeyen biçim: {fmt}. Seçenekler: csv, json.")
    if table not in EXPORTS:
        raise ValueError(f"Bilinmeyen tablo: {table}. Seçenekler: {', '.join(EXPORTS)}.")
    if kind is not None and table != "long_short":
        raise ValueError("Oran türü yalnızca long-short tablosunda seçilir.")
    if kind is not None and kind not in set(RatioKind):
        raise ValueError(f"Bilinmeyen oran türü: {kind}.")
    if start is not None and end is not None and end <= start:
        raise ValueError("Bitiş başlangıçtan sonra olmalı.")
    cols = EXPORTS[table]
    where, args = [], []
    for sql, value in (("symbol = ?", symbol), ("kind = ?", kind), ("time >= ?", start), ("time < ?", end)):
        if value is not None:
            where.append(sql)
            args.append(value)
    cond = " WHERE " + " AND ".join(where) if where else ""
    return f"SELECT {', '.join(cols)} FROM {table}{cond} ORDER BY time", tuple(args), cols


# İmleç parça parça okunur, bütün tablo hiçbir zaman bellekte durmaz. JSON
# bir dizi olarak yazılır, açılış ve kapanış köşeli parantezi ayrı parçadır.
def render(cursor: sqlite3.Cursor, cols: tuple[str, ...], fmt: str, chunk: int = 2000) -> Iterator[str]:
    """Sorgu sonucunu metin parçaları olarak üretir.

    Args:
        cursor: plan_export() sorgusunun imleci.
        cols: Sütun adları, CSV'nin başlık satırı ve JSON'un anahtarları.
        fmt: plan_export()'un doğruladığı biçim.
        chunk: Bir parçadaki en fazla satır.
    """
    first = True
    if fmt == "csv":
        buf = io.StringIO()
        csv.writer(buf, lineterminator="\n").writerow(cols)
        yield buf.getvalue()
    else:
        yield "["
    while rows := cursor.fetchmany(chunk):
        if fmt == "csv":
            buf = io.StringIO()
            csv.writer(buf, lineterminator="\n").writerows(tuple(r) for r in rows)
            yield buf.getvalue()
        else:
            text = ",\n".join(json.dumps(dict(zip(cols, r)), ensure_ascii=False) for r in rows)
            yield ("\n" if first else ",\n") + text
        first = False
    if fmt == "json":
        yield "\n]\n"
