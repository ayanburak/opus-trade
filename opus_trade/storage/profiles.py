"""
file: profiles.py
importable: ProfileRepo

ProfileRepo profillerin ve sürümlerinin deposudur. Profil kurar, okur ve
alanlarını günceller, bir profile yeni sürüm ekler ve sürümleri okur. Kurala
dair hiçbir karar vermez, doğrulama ve durum geçişleri runtime.profiles'tadır.

description: Profillerin ve sürümlerinin SQLite deposu.
"""

import json
import sqlite3

from ..domain.profile import Area, Profile, ProfileConfig, State, Version


def _profile(r: sqlite3.Row) -> Profile:
    return Profile(id=r["id"], name=r["name"], short=r["short"], area=Area(r["area"]), state=State(r["state"]),
                   account_id=r["account_id"], version_id=r["version_id"], published=bool(r["published"]),
                   created_at=r["created_at"])


def _version(r: sqlite3.Row) -> Version:
    return Version(id=r["id"], profile_id=r["profile_id"], number=r["number"],
                   config=ProfileConfig.from_dict(json.loads(r["config"])), note=r["note"], created_at=r["created_at"])


class ProfileRepo:
    def __init__(self, con: sqlite3.Connection) -> None:
        self.con = con

    def create(self, name: str, short: str, area: Area, now: int) -> Profile:
        with self.con:
            cur = self.con.execute("INSERT INTO profiles (name, short, area, created_at) VALUES (?, ?, ?, ?)",
                                   (name, short, str(area), now))
        return self.get(cur.lastrowid)

    def get(self, profile_id: int) -> Profile:
        r = self.con.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()
        if r is None:
            raise KeyError(profile_id)
        return _profile(r)

    def all(self) -> list[Profile]:
        return [_profile(r) for r in self.con.execute("SELECT * FROM profiles ORDER BY id")]

    # Alan adları kodun kendisinden gelir, dışarıdan bir ad sorguya girmez.
    def update(self, profile_id: int, **fields) -> Profile:
        fields = {k: str(v) if isinstance(v, (Area, State)) else v for k, v in fields.items()}
        with self.con:
            self.con.execute(f"UPDATE profiles SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                             (*fields.values(), profile_id))
        return self.get(profile_id)

    # Yayındaki profil tektir. Yenisi seçilince öncekinin işareti aynı işlemde kalkar.
    def publish(self, profile_id: int | None) -> None:
        with self.con:
            self.con.execute("UPDATE profiles SET published = 0")
            if profile_id is not None:
                self.con.execute("UPDATE profiles SET published = 1 WHERE id = ?", (profile_id,))

    def add_version(self, profile_id: int, config: ProfileConfig, note: str, now: int) -> Version:
        with self.con:
            n = self.con.execute("SELECT COALESCE(MAX(number), 0) + 1 FROM profile_versions WHERE profile_id = ?",
                                 (profile_id,)).fetchone()[0]
            cur = self.con.execute("INSERT INTO profile_versions (profile_id, number, config, note, created_at) "
                                   "VALUES (?, ?, ?, ?, ?)",
                                   (profile_id, n, json.dumps(config.to_dict(), ensure_ascii=False), note, now))
            self.con.execute("UPDATE profiles SET version_id = ? WHERE id = ?", (cur.lastrowid, profile_id))
        return self.version(cur.lastrowid)

    def version(self, version_id: int) -> Version:
        return _version(self.con.execute("SELECT * FROM profile_versions WHERE id = ?", (version_id,)).fetchone())

    def versions(self, profile_id: int) -> list[Version]:
        return [_version(r) for r in self.con.execute(
            "SELECT * FROM profile_versions WHERE profile_id = ? ORDER BY number", (profile_id,))]
