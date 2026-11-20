-- Çekirdeğin ilk şeması: hesap defteri. Bütün hesaplar, kâğıt ve canlı, aynı
-- tablolardadır ve her satır hesabının kimliğini taşır. Profiller ve sürümler
-- kendi taşımalarıyla gelir ve hesaplara bağlanır. Zamanlar unix saniyedir,
-- TP kademeleri JSON metnidir.

CREATE TABLE accounts (
    id             INTEGER PRIMARY KEY,
    kind           TEXT NOT NULL CHECK (kind IN ('paper', 'live')),
    start_balance  REAL NOT NULL,
    cash           REAL NOT NULL,
    created_at     INTEGER NOT NULL,
    closed_at      INTEGER
);

-- Açık ve kapanmış pozisyonlar. Kapanan her parça ayrıca trades tablosuna
-- yazılır, pozisyon satırı tamamen kapanınca status closed olur.
CREATE TABLE positions (
    id           INTEGER PRIMARY KEY,
    account_id   INTEGER NOT NULL REFERENCES accounts (id),
    number       INTEGER NOT NULL,
    symbol       TEXT NOT NULL,
    side         TEXT NOT NULL,
    leverage     REAL NOT NULL,
    qty          REAL NOT NULL,
    margin       REAL NOT NULL,
    entry_price  REAL NOT NULL,
    entry_time   INTEGER NOT NULL,
    tp_levels    TEXT NOT NULL DEFAULT '[]',
    sl           REAL,
    trail_pct    REAL,
    best_price   REAL NOT NULL,
    best_time    INTEGER NOT NULL,
    worst_price  REAL NOT NULL,
    worst_time   INTEGER NOT NULL,
    liq_price    REAL NOT NULL,
    fee          REAL NOT NULL DEFAULT 0,
    funding      REAL NOT NULL DEFAULT 0,
    order_id     INTEGER,
    status       TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed'))
);
CREATE INDEX positions_open ON positions (account_id, status);

CREATE TABLE orders (
    id           INTEGER PRIMARY KEY,
    account_id   INTEGER NOT NULL REFERENCES accounts (id),
    number       INTEGER NOT NULL,
    symbol       TEXT NOT NULL,
    side         TEXT NOT NULL,
    trigger_price REAL NOT NULL,
    below        INTEGER NOT NULL,
    maker        INTEGER NOT NULL,
    leverage     REAL NOT NULL,
    margin       REAL NOT NULL,
    tp_levels    TEXT NOT NULL DEFAULT '[]',
    sl           REAL,
    trail_pct    REAL,
    expires_at   INTEGER,
    created_at   INTEGER NOT NULL,
    resolved_at  INTEGER,
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'filled', 'cancelled', 'expired', 'failed')),
    note         TEXT,
    position_id  INTEGER REFERENCES positions (id)
);
CREATE INDEX orders_pending ON orders (account_id, status);

CREATE TABLE trades (
    id           INTEGER PRIMARY KEY,
    account_id   INTEGER NOT NULL REFERENCES accounts (id),
    position_id  INTEGER NOT NULL REFERENCES positions (id),
    number       INTEGER NOT NULL,
    symbol       TEXT NOT NULL,
    side         TEXT NOT NULL,
    leverage     REAL NOT NULL,
    qty          REAL NOT NULL,
    margin       REAL NOT NULL,
    entry_price  REAL NOT NULL,
    entry_time   INTEGER NOT NULL,
    exit_price   REAL NOT NULL,
    exit_time    INTEGER NOT NULL,
    reason       TEXT NOT NULL,
    pnl          REAL NOT NULL,
    fee          REAL NOT NULL,
    funding      REAL NOT NULL,
    best_price   REAL NOT NULL,
    worst_price  REAL NOT NULL
);
CREATE INDEX trades_account_time ON trades (account_id, exit_time);

-- Hesaptaki her değişiklik, yapanıyla. detail olayın JSON halidir.
CREATE TABLE account_events (
    id           INTEGER PRIMARY KEY,
    account_id   INTEGER NOT NULL REFERENCES accounts (id),
    time         INTEGER NOT NULL,
    actor        TEXT NOT NULL,
    kind         TEXT NOT NULL,
    number       INTEGER,
    symbol       TEXT,
    position_id  INTEGER,
    order_id     INTEGER,
    detail       TEXT NOT NULL
);
CREATE INDEX account_events_time ON account_events (account_id, time);

CREATE TABLE equity (
    account_id  INTEGER NOT NULL REFERENCES accounts (id),
    time        INTEGER NOT NULL,
    cash        REAL NOT NULL,
    equity      REAL NOT NULL,
    margin      REAL NOT NULL,
    PRIMARY KEY (account_id, time)
) WITHOUT ROWID;
