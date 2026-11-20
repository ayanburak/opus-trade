-- Profiller ve onları çalıştıran parçaların kayıtları.

-- Bir profil bir strateji türünün bir hesapla çalışan halidir. Ayarları
-- profile_versions'tadır, version_id yürürlükteki sürümdür. state draft,
-- running, paused, stopped ya da archived olur. Herkese açık sayfa yalnızca
-- published işaretli tek profili gösterir.
CREATE TABLE profiles (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    short        TEXT NOT NULL,
    area         TEXT NOT NULL CHECK (area IN ('paper', 'live')),
    state        TEXT NOT NULL DEFAULT 'draft'
                 CHECK (state IN ('draft', 'running', 'paused', 'stopped', 'archived')),
    account_id   INTEGER REFERENCES accounts (id),
    version_id   INTEGER,
    published    INTEGER NOT NULL DEFAULT 0,
    created_at   INTEGER NOT NULL
);

-- Sürüm değişmez. Ayar değişince yeni satır eklenir, eskisi silinmez. config
-- strateji türünü, parametrelerini, pariteleri, bakiyeyi, tetikleyicileri ve
-- risk sınırlarını taşıyan JSON'dur.
CREATE TABLE profile_versions (
    id          INTEGER PRIMARY KEY,
    profile_id  INTEGER NOT NULL REFERENCES profiles (id),
    number      INTEGER NOT NULL,
    config      TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    created_at  INTEGER NOT NULL,
    UNIQUE (profile_id, number)
);

-- Stratejinin uyanmalar arasında taşıdığı hafıza, örneğin Opus'un not defteri.
CREATE TABLE memory (
    profile_id  INTEGER PRIMARY KEY REFERENCES profiles (id),
    data        TEXT NOT NULL
);

-- Her uyanmanın kaydı. record stratejiye özgü ayrıntıdır: istem, ham cevap,
-- karar ve kullanım. results komutların sonuçlarıdır.
CREATE TABLE journal (
    id          INTEGER PRIMARY KEY,
    profile_id  INTEGER NOT NULL REFERENCES profiles (id),
    version_id  INTEGER NOT NULL REFERENCES profile_versions (id),
    time        INTEGER NOT NULL,
    wakes       TEXT NOT NULL,
    results     TEXT NOT NULL DEFAULT '[]',
    record      TEXT NOT NULL DEFAULT '{}',
    error       TEXT,
    next_wake   INTEGER
);
CREATE INDEX journal_profile ON journal (profile_id, id);

-- Bir uyanmadaki araç çağrıları, sonuçlarının tamamıyla.
CREATE TABLE tool_calls (
    id          INTEGER PRIMARY KEY,
    journal_id  INTEGER NOT NULL REFERENCES journal (id),
    seq         INTEGER NOT NULL,
    name        TEXT NOT NULL,
    title       TEXT,
    input       TEXT,
    result      TEXT,
    is_error    INTEGER,
    duration_s  REAL,
    sub         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX tool_calls_journal ON tool_calls (journal_id);

-- Fiyat alarmları. Tetiklenen ya da iptal edilen alarm silinmez, status değişir.
CREATE TABLE alerts (
    id          INTEGER PRIMARY KEY,
    profile_id  INTEGER NOT NULL REFERENCES profiles (id),
    number      INTEGER NOT NULL,
    symbol      TEXT NOT NULL,
    price       REAL NOT NULL,
    below       INTEGER NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    actor       TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'triggered', 'cancelled')),
    resolved_at INTEGER
);

-- Stratejiye henüz verilmemiş uyandırmalar. Program kapanırsa ya da model
-- erişimi kapalıysa burada bekler, verilince silinir. data uyandırmanın
-- verisinin türünü de taşıyan JSON'dur.
CREATE TABLE pending_wakes (
    id          INTEGER PRIMARY KEY,
    profile_id  INTEGER NOT NULL REFERENCES profiles (id),
    time        INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    data        TEXT
);

CREATE TABLE system_events (
    id          INTEGER PRIMARY KEY,
    profile_id  INTEGER,
    time        INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    message     TEXT NOT NULL,
    detail      TEXT
);
CREATE INDEX system_events_time ON system_events (time);

-- Bütün profillerde ortak ayarlar, örneğin model erişiminin açılacağı saat.
CREATE TABLE settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
