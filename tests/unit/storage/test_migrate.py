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


def test_python_migration_gets_the_context(tmp_path):
    """Python taşıması SQL'in hesaplayamadığı dönüşümler içindir, ihtiyaç duyduğu bilgiyi migrate()'ten alır."""
    m = tmp_path / "m"
    write(m, "0001_a.sql", "CREATE TABLE a (x INTEGER);")
    write(m, "0002_b.py", "def apply(con, value):\n    con.execute('INSERT INTO a VALUES (?)', (value,))\n")
    con = connect(tmp_path / "x.db")
    assert migrate(con, m, tmp_path / "bak", value=42) == ["0001_a.sql", "0002_b.py"]
    assert con.execute("SELECT x FROM a").fetchone()[0] == 42


def test_failed_python_migration_is_rolled_back_and_keys_come_back(tmp_path):
    """Python taşıması yarıda hata verirse yaptığı her şey geri alınır. Kapattığı yabancı anahtar denetimi
    açık kalmazsa sonraki yazmalar bozuk bağ kurabilirdi."""
    m = tmp_path / "m"
    write(m, "0001_a.sql", "CREATE TABLE a (x INTEGER);")
    write(m, "0002_b.py", "def apply(con):\n    con.execute('INSERT INTO a VALUES (1)')\n    raise ValueError('bozuk')\n")
    con = connect(tmp_path / "x.db")
    with pytest.raises(MigrationError, match="0002_b.py uygulanamadı.*bozuk"):
        migrate(con, m, tmp_path / "bak")
    assert con.execute("SELECT COUNT(*) FROM a").fetchone()[0] == 0
    assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_python_migration_that_breaks_a_reference_is_refused(tmp_path):
    """Taşıma sırasında yabancı anahtar denetimi kapalıdır. Sonunda kopan bir bağ kalırsa taşıma reddedilir."""
    m = tmp_path / "m"
    write(m, "0001_a.sql", "CREATE TABLE p (id INTEGER PRIMARY KEY);"
                           "CREATE TABLE c (p_id INTEGER REFERENCES p (id));")
    write(m, "0002_b.py", "def apply(con):\n    con.execute('INSERT INTO c VALUES (9)')\n")
    con = connect(tmp_path / "x.db")
    with pytest.raises(MigrationError, match="bağlandığı kayıt yok"):
        migrate(con, m, tmp_path / "bak")
    assert con.execute("SELECT COUNT(*) FROM c").fetchone()[0] == 0
