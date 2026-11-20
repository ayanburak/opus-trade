"""
file: migrate.py
importable: MigrationError, migrate

migrate() bir veritabanını verilen klasördeki taşıma dosyalarına göre günceller.
Dosyalar 0001_ad.sql biçiminde numaralıdır ve sırayla, her biri bir kez
uygulanır. Uygulanacak bir taşıma varsa ve veritabanında zaten veri varsa
önce yedek alınır. Bir taşıma hata verirse o taşıma tamamen geri alınır ve
hata yükseltilir, veritabanı son başarılı taşımadaki halinde kalır.

description: Numaralı SQL dosyalarıyla veritabanı şemasının güncellenmesi.
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
# XXX: SQLite'ta ALTER TABLE ile sütun silmek ve tipi değiştirmek kısıtlıdır.
# Böyle bir değişiklik yeni tablo kurup veriyi taşıyan bir dosyayla yapılır.

import re
import sqlite3
from pathlib import Path

_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


class MigrationError(Exception):
    pass


def _files(folder: Path) -> list[tuple[int, Path]]:
    found = []
    for f in sorted(folder.glob("*.sql")):
        m = _NAME.match(f.name)
        if not m:
            raise MigrationError(f"Taşıma dosyasının adı 0001_ad.sql biçiminde olmalı: {f.name}")
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


def migrate(con: sqlite3.Connection, folder: Path, backup_dir: Path) -> list[str]:
    """Eksik taşımaları sırayla uygular.

    Args:
        con: connect() ile yazılabilir açılmış bağlantı.
        folder: Bu veritabanının taşıma dosyalarının klasörü.
        backup_dir: Uygulamadan önce alınan yedeğin yazılacağı klasör.

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
        # executescript önce açık işlemi kapatır, bu yüzden işlem betiğin
        # içinde açılıp kapatılır. Bir satır hata verirse işlem açık kalır ve
        # ROLLBACK taşımanın yaptığı her şeyi geri alır.
        script = (f"BEGIN;\n{f.read_text()}\n"
                  f"INSERT INTO schema_migrations VALUES ({n}, '{f.name}', CAST(strftime('%s') AS INTEGER));\n"
                  f"COMMIT;")
        try:
            con.executescript(script)
        except sqlite3.Error as e:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise MigrationError(f"{f.name} uygulanamadı, veritabanı {n - 1}. taşımada kaldı: {e}") from e
        applied.append(f.name)
    return applied
