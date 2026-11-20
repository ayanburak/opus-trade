-- Yöneticiye giden bildirimler. level critical, important ya da info olur.
-- kind olayın türüdür, panel sesi ve simgeyi seviyeden, metni title ve
-- body'den alır. profile_id sistemin kendi bildiriminde boştur. read_at
-- okundu yapıldığı andır, okundu bilgisi bütün cihazlarda aynı olsun diye
-- burada tutulur.
CREATE TABLE notifications (
    id          INTEGER PRIMARY KEY,
    time        INTEGER NOT NULL,
    profile_id  INTEGER REFERENCES profiles (id),
    level       TEXT NOT NULL CHECK (level IN ('critical', 'important', 'info')),
    kind        TEXT NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL DEFAULT '',
    read_at     INTEGER
);
CREATE INDEX notifications_unread ON notifications (read_at, profile_id);
