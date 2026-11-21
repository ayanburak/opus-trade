"""
file: migrate.py
importable: MigrationError, migrate

migrate() bir veritabanını verilen klasördeki taşıma dosyalarına göre günceller.
Dosyalar 0001_ad.sql ya da 0001_ad.py biçiminde numaralıdır ve sırayla, her
biri bir kez uygulanır. Python taşıması apply(con, **context) fonksiyonu
taşır, context migrate()'e verilen ek bilgidir, örneğin parite kuralları. Uygulanacak bir taşıma varsa ve veritabanında zaten veri varsa
önce yedek alınır. Bir taşıma hata verirse o taşıma tamamen geri alınır ve
hata yükseltilir, veritabanı son başarılı taşımadaki halinde kalır.

description: Numaralı SQL ve Python dosyalarıyla veritabanı şemasının güncellenmesi.
"""

# NOTE: Her veritabanının kendi taşıma klasörü vardır, örneğin çekirdeğin ve
# toplayıcının. Uygulanan taşımalar schema_migrations tablosunda tutulur. Bir
# taşıma dosyası uygulandıktan sonra değiştirilmez, düzeltme yeni bir dosyayla
# yapılır.
#
# NOTE: Veritabanı koddaki son taşımadan yeniyse, yani eski kod yeni bir
# veritabanıyla açılıyorsa migrate() hata verir. Eski kod yeni sütunları
# bilmeden yazmaya başlarsa veri bozulurdu.
#
# NOTE: Python taşıması yalnızca veriyi SQL'le hesaplanamayacak biçimde
# dönüştüren işler içindir, örneğin kademeli likidasyon. İçinde
# executescript kullanılmaz, o açık işlemi kapatır ve geri alma bozulur.
#
# XXX: SQLite'ta ALTER TABLE ile sütun silmek ve tipi değiştirmek kısıtlıdır.
# Böyle bir değişiklik yeni tablo kurup veriyi taşıyan bir dosyayla yapılır.

import importlib.util
import re
import sqlite3
from pathlib import Path

_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.(sql|py)$")


class MigrationError(Exception):
    pass


def _files(folder: Path) -> list[tuple[int, Path]]:
    found = []
    for f in sorted(x for x in folder.iterdir() if x.suffix in (".sql", ".py")):
        m = _NAME.match(f.name)
        if not m:
            raise MigrationError(f"Taşıma dosyasının adı 0001_ad.sql ya da 0001_ad.py biçiminde olmalı: {f.name}")
        found.append((int(m.group(1)), f))
    numbers = [n for n, _ in found]
    if numbers != list(range(1, len(numbers) + 1)):
        raise MigrationError(f"Taşıma numaraları birden başlayıp boşluksuz artmalı: {numbers}")
    return found


# Yedek, uygulanmadan önceki sürümün numarasını taşır. Aynı sürümden ikinci
# bir deneme, örneğin hata verip düzeltilen bir taşıma, aynı dosyanın üstüne
# yazar, çünkü ilk deneme veritabanını zaten değiştirmemiştir.
def _backup(con: sqlite3.Connection, backup_dir: Path, version: int) -> Path:
    main = next(r for r in con.execute("PRAGMA database_list") if r[1] == "main")
    target = backup_dir / f"{Path(main[2]).stem}-v{version:04d}.db"
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = sqlite3.connect(target)
    try:
        con.backup(dest)
    finally:
        dest.close()
    return target


# SQL taşıması executescript ile çalışır. executescript önce açık işlemi
# kapatır, bu yüzden işlem betiğin içinde açılıp kapatılır. Bir satır hata
# verirse işlem açık kalır ve çağıranın ROLLBACK'i her şeyi geri alır.
def _run_sql(con: sqlite3.Connection, n: int, f: Path) -> None:
    con.executescript(f"BEGIN;\n{f.read_text()}\n"
                      f"INSERT INTO schema_migrations VALUES ({n}, '{f.name}', CAST(strftime('%s') AS INTEGER));\n"
                      f"COMMIT;")


# Python taşıması tabloları yeniden kurabilir. Yabancı anahtar açıkken başka
# tabloların gösterdiği bir tablo silinemez, SQLite'ın önerdiği yolla anahtar
# denetimi taşıma boyunca kapatılır, sonunda bütün bağlar bir kez denetlenir.
# Denetim işlemin içinde açılıp kapatılamaz, bu yüzden işlem burada biter.
def _run_py(con: sqlite3.Connection, n: int, f: Path, context: dict) -> None:
    spec = importlib.util.spec_from_file_location(f"migration_{n:04d}", f)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    con.execute("PRAGMA foreign_keys = OFF")
    try:
        con.execute("BEGIN")
        module.apply(con, **context)
        broken = con.execute("PRAGMA foreign_key_check").fetchall()
        if broken:
            raise MigrationError(f"Taşımadan sonra {len(broken)} satırın bağlandığı kayıt yok, "
                                 f"ilki {tuple(broken[0])}.")
        con.execute("INSERT INTO schema_migrations VALUES (?, ?, CAST(strftime('%s') AS INTEGER))", (n, f.name))
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    finally:
        con.execute("PRAGMA foreign_keys = ON")


def migrate(con: sqlite3.Connection, folder: Path, backup_dir: Path, **context) -> list[str]:
    """Eksik taşımaları sırayla uygular.

    Args:
        con: connect() ile yazılabilir açılmış bağlantı.
        folder: Bu veritabanının taşıma dosyalarının klasörü.
        backup_dir: Uygulamadan önce alınan yedeğin yazılacağı klasör.
        context: Python taşımalarının apply fonksiyonuna verilecek ek bilgi.

    Returns:
        Uygulanan dosyaların adları, hiçbiri uygulanmadıysa boş liste.

    Raises:
        MigrationError: Dosya adları geçersizse, veritabanı koddan yeniyse ya da bir taşıma hata verirse.
    """
    con.execute("CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at INTEGER NOT NULL)")
    con.commit()
    current = con.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
    files = _files(folder)
    if current > len(files):
        raise MigrationError(f"Veritabanı {current}. taşımada, kod yalnızca {len(files)} taşıma biliyor. "
                             f"Bu veritabanı daha yeni bir kodla güncellenmiş.")
    pending = [(n, f) for n, f in files if n > current]
    if pending and current > 0:
        _backup(con, backup_dir, current)
    applied = []
    for n, f in pending:
        try:
            if f.suffix == ".py":
                _run_py(con, n, f, context)
            else:
                _run_sql(con, n, f)
        except Exception as e:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise MigrationError(f"{f.name} uygulanamadı, veritabanı {n - 1}. taşımada kaldı: {e}") from e
        applied.append(f.name)
    return applied
