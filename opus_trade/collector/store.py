"""
file: store.py
importable: MIGRATIONS, CollectorStore

CollectorStore toplayıcının market.db deposuna yazar ve okur. Her add_ metodu
bir kayıt listesini tek işlemde yazar, zaten var olan kaydı sessizce atlar.
last_time() bir tablonun bir paritedeki en son kaydının zamanını, symbols()
izlenen pariteleri döndürür. touch() canlı bir kaynağın çalıştığını kapsama
kaydına işler. MIGRATIONS bu deponun taşıma klasörüdür.

description: Toplanan piyasa verisinin SQLite deposu.
"""

# NOTE: Depoya yalnızca toplayıcı servisi yazar. Çekirdek ve Opus'un piyasa
# araçları aynı dosyayı salt okunur açar.
#
# NOTE: Kapsama kaydı yalnızca canlıdan alınabilen kaynaklar içindir:
# likidasyon ve premium. Otuz günlük geçmişi olan istatistiklerde boşluk
# verinin kendisinden görülür ve geriye dönük doldurmayla kapanır.

import sqlite3
from pathlib import Path

from ..market.models import Liquidation, LongShortRatio, OpenInterest, Premium, TakerVolume
from ..market.source import RatioKind

MIGRATIONS = Path(__file__).parent / "migrations"

# last_time() yalnızca bu tablolara bakabilir. Tablo adı sorguya metin olarak
# girdiği için dışarıdan gelen bir ad doğrudan kullanılmaz.
_TIMED = ("premium", "open_interest", "long_short", "taker_volume")


class CollectorStore:
    """market.db'ye yazan ve okuyan depo.

    Args:
        con: migrate() ile güncellenmiş, yazılabilir bağlantı.
    """

    def __init__(self, con: sqlite3.Connection) -> None:
        self.con = con

    def _write(self, sql: str, rows: list[tuple]) -> int:
        before = self.con.total_changes
        with self.con:
            self.con.executemany(sql, rows)
        return self.con.total_changes - before

    # Her add_ metodu yazılan yeni kayıt sayısını döndürür, testler ve günlük
    # bununla ikilenmeyi görür.
    def add_liquidations(self, items: list[Liquidation]) -> int:
        return self._write("INSERT OR IGNORE INTO liquidations VALUES (?, ?, ?, ?, ?)",
                           [(x.time, x.symbol, str(x.side), x.price, x.qty) for x in items])

    def add_premiums(self, items: list[Premium]) -> int:
        return self._write("INSERT OR IGNORE INTO premium VALUES (?, ?, ?, ?, ?, ?)",
                           [(x.symbol, x.time, x.mark, x.index, x.est_funding, x.next_funding) for x in items])

    def add_open_interest(self, items: list[OpenInterest]) -> int:
        return self._write("INSERT OR IGNORE INTO open_interest VALUES (?, ?, ?, ?)",
                           [(x.symbol, x.time, x.qty, x.usd) for x in items])

    def add_long_short(self, kind: RatioKind, items: list[LongShortRatio]) -> int:
        return self._write("INSERT OR IGNORE INTO long_short VALUES (?, ?, ?, ?, ?)",
                           [(str(kind), x.symbol, x.time, x.long_pct, x.ratio) for x in items])

    def add_taker_volume(self, items: list[TakerVolume]) -> int:
        return self._write("INSERT OR IGNORE INTO taker_volume VALUES (?, ?, ?, ?, ?)",
                           [(x.symbol, x.time, x.buy_sell_ratio, x.buy_volume, x.sell_volume) for x in items])

    def last_time(self, table: str, symbol: str, kind: RatioKind | None = None) -> int | None:
        """Bir tablonun bir paritedeki en son kaydının zamanını döndürür, kayıt yoksa None.

        Args:
            table: premium, open_interest, long_short ya da taker_volume.
            symbol: Parite.
            kind: Yalnızca long_short tablosunda, oranın türü.
        """
        if table not in _TIMED:
            raise ValueError(f"Bilinmeyen tablo: {table}")
        if kind is None:
            row = self.con.execute(f"SELECT MAX(time) FROM {table} WHERE symbol = ?", (symbol,)).fetchone()
        else:
            row = self.con.execute(f"SELECT MAX(time) FROM {table} WHERE symbol = ? AND kind = ?",
                                   (symbol, str(kind))).fetchone()
        return row[0]

    def symbols(self) -> list[str]:
        return [r[0] for r in self.con.execute("SELECT symbol FROM tracked_symbols ORDER BY symbol")]

    def add_symbol(self, symbol: str, now: int) -> None:
        with self.con:
            self.con.execute("INSERT OR IGNORE INTO tracked_symbols VALUES (?, ?)", (symbol, now))

    def remove_symbol(self, symbol: str) -> None:
        with self.con:
            self.con.execute("DELETE FROM tracked_symbols WHERE symbol = ?", (symbol,))

    # Kaynağın son aralığı max_gap içinde dokunulmuşsa aralık uzatılır, daha
    # eskiyse arada kesinti olmuştur ve yeni bir aralık açılır.
    def touch(self, source: str, now: int, max_gap: int) -> None:
        """Bir canlı kaynağın now anında çalıştığını kapsama kaydına işler.

        Args:
            source: Kaynağın adı, örneğin liquidations.
            now: Kaynağın çalıştığı an.
            max_gap: Bundan uzun bir sessizlik kesinti sayılır, saniye.
        """
        last = self.con.execute("SELECT rowid, end_time FROM coverage WHERE source = ? "
                                "ORDER BY start_time DESC LIMIT 1", (source,)).fetchone()
        with self.con:
            if last and now - last[1] <= max_gap:
                self.con.execute("UPDATE coverage SET end_time = ? WHERE rowid = ?", (max(now, last[1]), last[0]))
            else:
                self.con.execute("INSERT INTO coverage VALUES (?, ?, ?)", (source, now, now))

    def coverage(self, source: str) -> list[tuple[int, int]]:
        return [(r[0], r[1]) for r in self.con.execute(
            "SELECT start_time, end_time FROM coverage WHERE source = ? ORDER BY start_time", (source,))]
