-- Opus'un kararlarına bağlı notlar. Broker Binance'in modelidir ve Binance
-- bunları tutamaz, bu yüzden hesap defterinin tablolarına değil buraya yazılır.

-- Bir girişin gerekçesi ve geçersizliği. Bekleyen girişte order_id, piyasa
-- girişinde position_id dolu başlar. Emir dolunca position_id de yazılır.
-- reason girişin karşı kanıtını, risk beyanını, geçersizlik metnini ve varsa
-- son stoptan farkını taşıyan JSON'dur.
CREATE TABLE entry_notes (
    id            INTEGER PRIMARY KEY,
    profile_id    INTEGER NOT NULL REFERENCES profiles (id),
    account_id    INTEGER NOT NULL REFERENCES accounts (id),
    order_id      INTEGER REFERENCES orders (id),
    position_id   INTEGER REFERENCES positions (id),
    symbol        TEXT NOT NULL,
    side          TEXT NOT NULL CHECK (side IN ('long', 'short')),
    reason        TEXT NOT NULL,
    invalid_price REAL NOT NULL,
    invalid_text  TEXT NOT NULL,
    created_at    INTEGER NOT NULL,
    journal_id    INTEGER REFERENCES journal (id)
);
CREATE INDEX entry_notes_order ON entry_notes (order_id);
CREATE INDEX entry_notes_position ON entry_notes (position_id);
CREATE INDEX entry_notes_profile ON entry_notes (profile_id, id);

-- Kapanan bir pozisyonun Opus'un yaptığı sınıflandırması. Her pozisyon bir
-- kez sınıflandırılır. verdict Opus'a görünen Türkçe sınıftır.
CREATE TABLE position_reviews (
    position_id  INTEGER PRIMARY KEY REFERENCES positions (id),
    profile_id   INTEGER NOT NULL REFERENCES profiles (id),
    journal_id   INTEGER REFERENCES journal (id),
    verdict      TEXT NOT NULL,
    text         TEXT NOT NULL,
    created_at   INTEGER NOT NULL
);

-- Fiyatın bir girişin geçersizlik seviyesine değdiği an. Aynı notun ikinci
-- kez işlenmesini engeller. action cancelled ya da woke olur.
CREATE TABLE invalidation_hits (
    note_id  INTEGER PRIMARY KEY REFERENCES entry_notes (id),
    time     INTEGER NOT NULL,
    action   TEXT NOT NULL CHECK (action IN ('cancelled', 'woke'))
);

-- Alarmın amacı. opus Opus'un, admin yöneticinin kurduğudur. expectation
-- Opus'un rejimi uygun bulmadığı çağrıda beklediği fiyata sistemin kurduğu
-- alarmdır. Eski alarmlar kuranına göre doldurulur.
ALTER TABLE alerts ADD COLUMN purpose TEXT NOT NULL DEFAULT 'opus'
    CHECK (purpose IN ('opus', 'admin', 'expectation'));
UPDATE alerts SET purpose = 'admin' WHERE actor = 'admin';
