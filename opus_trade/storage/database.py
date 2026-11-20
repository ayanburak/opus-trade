"""
file: database.py
importable: connect

connect() bir SQLite veritabanını programın bütün parçalarında aynı ayarlarla
açar. Yazılabilir açılışta klasörü kurar ve WAL kipine geçer. Salt okunur
açılış dosyaya hiçbir şey yazmaz. Satırlar sqlite3.Row olarak döner, sütun
adıyla okunabilir.

description: SQLite bağlantısının ortak açılışı.
"""

# NOTE: WAL kipi bir yazarın yanında aynı anda birden çok okuyucuya izin verir.
# Çekirdek, web servisi, toplayıcı ve Opus'un piyasa araçları aynı dosyaları
# ayrı süreçlerden okur, yazan her zaman tek bir servistir.
#
# NOTE: busy_timeout, bir okuyucu dosyayı kısa bir an kilitlediğinde yazarın
# hemen hata vermek yerine beklemesini sağlar.

import sqlite3
from pathlib import Path

_BUSY_MS = 5000


def connect(path: Path, readonly: bool = False) -> sqlite3.Connection:
    """Veritabanını açar.

    Args:
        path: Veritabanı dosyası.
        readonly: True ise dosya salt okunur açılır, yoksa hata verir.

    Raises:
        sqlite3.OperationalError: Salt okunur açılışta dosya yoksa.
    """
    if readonly:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(path)
        con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA foreign_keys = ON")
    con.execute(f"PRAGMA busy_timeout = {_BUSY_MS}")
    con.row_factory = sqlite3.Row
    return con
