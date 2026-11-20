-- Yöneticinin bir profile yaptığı müdahaleler. weight teklif için offer,
-- kesin için firm, ağırlığı olmayan türlerde boştur. params türe göre JSON'dur.
-- delivered_at müdahalenin stratejiye gösterildiği an, withdrawn_at gösterilmeden
-- geri çekildiği andır. Kısıtlarda ends_at bitiş, removed_at kaldırılma anıdır.
-- Planlı çağrıda due_at çağrının zamanı, fired_at uyandırmanın yapıldığı andır.
-- result kesin bir işlemin sonucudur.
CREATE TABLE interventions (
    id            INTEGER PRIMARY KEY,
    profile_id    INTEGER NOT NULL REFERENCES profiles (id),
    created_at    INTEGER NOT NULL,
    kind          TEXT NOT NULL,
    weight        TEXT,
    params        TEXT NOT NULL,
    note          TEXT NOT NULL DEFAULT '',
    call_now      INTEGER NOT NULL DEFAULT 0,
    due_at        INTEGER,
    fired_at      INTEGER,
    ends_at       INTEGER,
    removed_at    INTEGER,
    delivered_at  INTEGER,
    withdrawn_at  INTEGER,
    result        TEXT
);
CREATE INDEX interventions_profile ON interventions (profile_id, id);
