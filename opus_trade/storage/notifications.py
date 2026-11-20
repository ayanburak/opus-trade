"""
file: notifications.py
importable: LEVELS, NotificationRepo

NotificationRepo bildirimleri saklar. add() yeni bir bildirimi yazar ve
satırını döndürür, list() profile ve okunmamışlara göre süzer, counts()
okunmamış kritik ve önemli bildirimleri profil başına sayar, mark_read()
tek tek, profile göre ya da hepsini birden okundu yapar.

description: Bildirimlerin ve okundu bilgisinin deposu.
"""

# NOTE: Bilgi seviyesindeki bildirimler sayılmaz, panelde yalnızca listede
# durur. Baloncuktaki sayı kritik ve önemlilerin toplamıdır.

import sqlite3

LEVELS = ("critical", "important", "info")


class NotificationRepo:
    def __init__(self, con: sqlite3.Connection) -> None:
        self.con = con

    def add(self, time: int, profile_id: int | None, level: str, kind: str, title: str, body: str = "") -> dict:
        if level not in LEVELS:
            raise ValueError(f"Bilinmeyen bildirim seviyesi: {level}")
        with self.con:
            cur = self.con.execute("INSERT INTO notifications (time, profile_id, level, kind, title, body) "
                                   "VALUES (?, ?, ?, ?, ?, ?)", (time, profile_id, level, kind, title, body))
        return self.get(cur.lastrowid)

    def get(self, nid: int) -> dict:
        return dict(self.con.execute("SELECT * FROM notifications WHERE id = ?", (nid,)).fetchone())

    def list(self, profile_id: int | None = None, unread: bool = False, before: int | None = None,
             limit: int = 100) -> list[dict]:
        """Bildirimleri yeniden eskiye döndürür. before verilirse o kimlikten eskileri getirir."""
        sql, args = "SELECT * FROM notifications WHERE 1 = 1", []
        if profile_id is not None:
            sql += " AND profile_id = ?"
            args.append(profile_id)
        if unread:
            sql += " AND read_at IS NULL"
        if before is not None:
            sql += " AND id < ?"
            args.append(before)
        rows = self.con.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit))
        return [dict(r) for r in rows]

    # Profil kimliği JSON'da anahtar olacağı için metne çevrilir. Sistemin
    # kendi bildirimleri system anahtarında sayılır.
    def counts(self) -> dict:
        """Okunmamış kritik ve önemli bildirimleri toplamda ve profil başına sayar."""
        total = {"critical": 0, "important": 0}
        profiles: dict[str, dict[str, int]] = {}
        for pid, level, n in self.con.execute(
                "SELECT profile_id, level, COUNT(*) FROM notifications WHERE read_at IS NULL "
                "AND level IN ('critical', 'important') GROUP BY profile_id, level"):
            total[level] += n
            key = "system" if pid is None else str(pid)
            profiles.setdefault(key, {"critical": 0, "important": 0})[level] = n
        return {"total": total, "profiles": profiles}

    def mark_read(self, now: int, ids: list[int] | None = None, profile_id: int | None = None,
                  everything: bool = False) -> int:
        """Bildirimleri okundu yapar ve değişen satır sayısını döndürür.

        Raises:
            ValueError: Ne kimlik, ne profil ne de hepsi seçilmişse.
        """
        if ids:
            marks = ",".join("?" * len(ids))
            sql, args = f"UPDATE notifications SET read_at = ? WHERE read_at IS NULL AND id IN ({marks})", ids
        elif profile_id is not None:
            sql, args = "UPDATE notifications SET read_at = ? WHERE read_at IS NULL AND profile_id = ?", [profile_id]
        elif everything:
            sql, args = "UPDATE notifications SET read_at = ? WHERE read_at IS NULL", []
        else:
            raise ValueError("Okundu yapılacak bildirim seçilmedi.")
        with self.con:
            return self.con.execute(sql, (now, *args)).rowcount
