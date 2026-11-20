"""
file: test_migrate.py
importable: yok

Taşıma çalıştırıcısının testleri. Şema değişikliği artık hiçbir profili
sıfırlamadan yapılıyor, bu yüzden bir taşımanın yarım kalması, iki kez
uygulanması ya da yedeksiz çalışması doğrudan veri kaybıdır.

description: storage.migrate için birim testleri.
"""

import sqlite3

import pytest

from opus_trade.storage.database import connect
from opus_trade.storage.migrate import MigrationError, migrate


def write(folder, name, sql):
    folder.mkdir(exist_ok=True)
    (folder / name).write_text(sql)


def tables(con) -> set[str]:
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def test_each_migration_runs_once_in_order(tmp_path):
    """İkinci açılışta aynı taşıma tekrar çalışırsa tablo zaten var hatası verir ya da veriyi ikiler."""
    m = tmp_path / "m"
    write(m, "0001_a.sql", "CREATE TABLE a (x INTEGER);")
    write(m, "0002_b.sql", "INSERT INTO a VALUES (1);")
    con = connect(tmp_path / "x.db")
    assert migrate(con, m, tmp_path / "bak") == ["0001_a.sql", "0002_b.sql"]
    assert migrate(con, m, tmp_path / "bak") == []
    assert con.execute("SELECT COUNT(*) FROM a").fetchone()[0] == 1


def test_failed_migration_is_rolled_back_completely(tmp_path):
    """Yarısı uygulanmış bir taşıma veritabanını hiçbir sürüme uymayan bir halde bırakırdı."""
    m = tmp_path / "m"
    write(m, "0001_a.sql", "CREATE TABLE a (x INTEGER);")
    con = connect(tmp_path / "x.db")
    migrate(con, m, tmp_path / "bak")
    write(m, "0002_bad.sql", "CREATE TABLE b (y INTEGER);\nINSERT INTO nowhere VALUES (1);")
    with pytest.raises(MigrationError, match="1. taşımada kaldı"):
        migrate(con, m, tmp_path / "bak")
    assert "b" not in tables(con)
    assert con.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 1


def test_backup_is_taken_before_changing_existing_data(tmp_path):
    """Veri taşıyan bir veritabanı güncellenmeden önce eski hali yedeklenir. Taşıma veriyi bozarsa geri dönülebilir."""
    m = tmp_path / "m"
    write(m, "0001_a.sql", "CREATE TABLE a (x INTEGER); INSERT INTO a VALUES (7);")
    con = connect(tmp_path / "x.db")
    migrate(con, m, tmp_path / "bak")
    assert not (tmp_path / "bak").exists()
    write(m, "0002_drop.sql", "DROP TABLE a;")
    migrate(con, m, tmp_path / "bak")
    old = sqlite3.connect(tmp_path / "bak" / "x-v0001.db")
    assert old.execute("SELECT x FROM a").fetchone()[0] == 7


def test_database_newer_than_code_is_refused(tmp_path):
    """Eski kod yeni bir veritabanını açarsa bilmediği sütunları bozarak yazar."""
    m = tmp_path / "m"
    write(m, "0001_a.sql", "CREATE TABLE a (x INTEGER);")
    write(m, "0002_b.sql", "CREATE TABLE b (x INTEGER);")
    con = connect(tmp_path / "x.db")
    migrate(con, m, tmp_path / "bak")
    (m / "0002_b.sql").unlink()
    with pytest.raises(MigrationError, match="daha yeni"):
        migrate(con, m, tmp_path / "bak")
