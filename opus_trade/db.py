"""
file: db.py
importable: connect, get_state, set_state, rows

connect() veritabanını açar ve tablolar yoksa kurar. get_state ve set_state,
bakiye ya da not defteri gibi tekil değerleri anahtar ile saklar. rows()
bir sorgunun sonucunu sözlük listesi olarak döndürür.

description: Hesabın, işlemlerin, emirlerin, alarmların, müdahalelerin, olay kayıtlarının ve Opus günlüğünün tutulduğu SQLite katmanı.
"""

# NOTE: Program tek bir asyncio döngüsünde çalıştığı için tek bağlantı
# yeterli. Yazmalar küçük ve seyrek olduğu için async bir sürücüye gerek
# görülmedi.
#
# NOTE: trades tablosu hem açık hem kapanmış pozisyonları tutar, status
# alanı open ya da closed olur. journal tablosundaki decision alanı Opus'un
# kararını şemadaki Türkçe alan adlarıyla olduğu gibi saklar.
#
# NOTE: Kayıt düzeni. trade_events hesaptaki her değişikliği yapanıyla
# tutar, actor opus, admin ya da system olur. equity özkaynağı dakikada bir ve
# her olaydan sonra tutar, aynı saniyede ikinci kayıt öncekinin üstüne yazılır.
# tool_calls bir çağrıdaki her aracın girdisini, sonucunun tamamını, hata
# bilgisini ve süresini tutar, journal'daki tools alanı ise arayüz için
# hafif bir özettir. system_events programın kendi olaylarını tutar:
# açılış, kapanış, kapalı süre, dönem sonu, çağrı hataları.
#
# XXX: Eski bir veritabanına yeni sütunlar eklenmez. CREATE TABLE IF NOT
# EXISTS var olan tabloyu değiştirmez, yeni yapı sıfırlanmış bir dönemle,
# boş bir veri klasöründe başlar.
#
# NOTE: interventions tablosu yöneticinin müdahalelerini tutar. Kısıtlar da
# birer müdahaledir, ends_at ve removed_at alanları yürürlükte olup
# olmadıklarını belirler. Ayrıntısı engine/interventions.py'de.

import json
import sqlite3
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT NOT NULL,
    side         TEXT NOT NULL,
    leverage     REAL NOT NULL,
    margin       REAL NOT NULL,
    qty          REAL NOT NULL,
    entry_price  REAL NOT NULL,
    entry_time   INTEGER NOT NULL,
    tp_levels    TEXT NOT NULL DEFAULT '[]',
    sl           REAL,
    trail_pct    REAL,
    best_price   REAL,
    best_time    INTEGER,
    worst_price  REAL,
    worst_time   INTEGER,
    liq_price    REAL NOT NULL,
    exit_price   REAL,
    exit_time    INTEGER,
    exit_reason  TEXT,
    pnl          REAL,
    fee          REAL NOT NULL DEFAULT 0,
    funding      REAL NOT NULL DEFAULT 0,
    order_id     INTEGER,
    parent_id    INTEGER,
    status       TEXT NOT NULL DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS orders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT NOT NULL,
    side          TEXT NOT NULL,
    trigger_price REAL NOT NULL,
    condition     TEXT NOT NULL,
    maker         INTEGER NOT NULL,
    leverage      INTEGER NOT NULL,
    margin        REAL NOT NULL,
    tp_levels     TEXT NOT NULL DEFAULT '[]',
    sl            REAL,
    trail_pct     REAL,
    expires_at    INTEGER,
    created_at    INTEGER NOT NULL,
    resolved_at   INTEGER,
    result_note   TEXT,
    status        TEXT NOT NULL DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS alerts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT NOT NULL,
    price        REAL NOT NULL,
    condition    TEXT NOT NULL,
    note         TEXT,
    created_at   INTEGER NOT NULL,
    resolved_at  INTEGER,
    status       TEXT NOT NULL DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS journal (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    time            INTEGER NOT NULL,
    trigger         TEXT NOT NULL,
    trigger_detail  TEXT,
    prompt          TEXT,
    raw_response    TEXT,
    decision        TEXT,
    action_results  TEXT,
    next_call       INTEGER,
    duration_s      REAL,
    cost_usd        REAL,
    tools           TEXT,
    error           TEXT,
    events          TEXT,
    usage           TEXT,
    model           TEXT,
    effort          TEXT,
    code_version    TEXT,
    tools_digest    TEXT,
    prices          TEXT
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    journal_id   INTEGER NOT NULL,
    seq          INTEGER NOT NULL,
    name         TEXT NOT NULL,
    title        TEXT,
    input        TEXT,
    result       TEXT,
    is_error     INTEGER,
    duration_s   REAL,
    sub          INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS trade_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    time         INTEGER NOT NULL,
    actor        TEXT NOT NULL,
    kind         TEXT NOT NULL,
    symbol       TEXT,
    position_id  INTEGER,
    order_id     INTEGER,
    alert_id     INTEGER,
    price        REAL,
    message      TEXT NOT NULL,
    detail       TEXT
);

CREATE TABLE IF NOT EXISTS equity (
    time    INTEGER PRIMARY KEY,
    cash    REAL NOT NULL,
    equity  REAL NOT NULL,
    margin  REAL NOT NULL,
    upnl    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS system_events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    time     INTEGER NOT NULL,
    kind     TEXT NOT NULL,
    message  TEXT NOT NULL,
    detail   TEXT
);

CREATE TABLE IF NOT EXISTS interventions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    INTEGER NOT NULL,
    kind          TEXT NOT NULL,
    weight        TEXT,
    params        TEXT NOT NULL DEFAULT '{}',
    note          TEXT NOT NULL DEFAULT '',
    call_now      INTEGER NOT NULL DEFAULT 0,
    due_at        INTEGER,
    fired_at      INTEGER,
    ends_at       INTEGER,
    removed_at    INTEGER,
    result        TEXT,
    delivered_at  INTEGER
);
"""


# Bağlantı satırları sözlük gibi okunabilsin diye Row fabrikasıyla açılır.
def connect(path: Path) -> sqlite3.Connection:
    """Veritabanını açar, tablolar yoksa kurar.

    Args:
        path: Profilin veritabanı dosyası. Klasörü yoksa açılır.

    Returns:
        Açık SQLite bağlantısı.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(_SCHEMA)
    con.commit()
    return con


# Değerler JSON olarak saklanır, böylece sayı, metin ya da None aynı
# tabloda tipini kaybetmeden durur.
def get_state(con: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = con.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_state(con: sqlite3.Connection, key: str, value: Any) -> None:
    con.execute(
        "INSERT INTO state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value, ensure_ascii=False)),
    )
    con.commit()


# Row nesneleri JSON'a çevrilemediği için web katmanına gitmeden önce düz
# sözlüğe çevrilir.
def rows(con: sqlite3.Connection, query: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in con.execute(query, params).fetchall()]
