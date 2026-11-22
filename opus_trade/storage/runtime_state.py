"""
file: runtime_state.py
importable: StateRepo

StateRepo çalıştırıcının kalıcı küçük kayıtlarını saklar: fiyat alarmları,
stratejiye henüz verilmemiş uyandırmalar, bütün profillerde ortak ayarlar ve
sistem olayları.

description: Alarmların, bekleyen uyandırmaların, ayarların ve sistem olaylarının SQLite deposu.
"""

import json
import sqlite3
from typing import Any

from ..triggers.price_alert import Alert


def _alert(r: sqlite3.Row) -> Alert:
    return Alert(number=r["number"], symbol=r["symbol"], price=r["price"], below=bool(r["below"]), note=r["note"],
                 actor=r["actor"], created_at=r["created_at"], purpose=r["purpose"])


class StateRepo:
    def __init__(self, con: sqlite3.Connection) -> None:
        self.con = con

    # --- Alarmlar ---

    def alerts(self, profile_id: int) -> list[Alert]:
        return [_alert(r) for r in self.con.execute(
            "SELECT * FROM alerts WHERE profile_id = ? AND status = 'pending' ORDER BY number", (profile_id,))]

    def add_alert(self, profile_id: int, a: Alert) -> None:
        with self.con:
            self.con.execute("INSERT INTO alerts (profile_id, number, symbol, price, below, note, actor, created_at, "
                             "purpose) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (profile_id, a.number, a.symbol, a.price, int(a.below), a.note, a.actor, a.created_at,
                              a.purpose))

    def resolve_alert(self, profile_id: int, number: int, status: str, now: int) -> None:
        with self.con:
            self.con.execute("UPDATE alerts SET status = ?, resolved_at = ? WHERE profile_id = ? AND number = ? "
                             "AND status = 'pending'", (status, now, profile_id, number))

    # --- Bekleyen uyandırmalar ---

    def push_wake(self, profile_id: int, time: int, kind: str, data: str | None) -> None:
        with self.con:
            self.con.execute("INSERT INTO pending_wakes (profile_id, time, kind, data) VALUES (?, ?, ?, ?)",
                             (profile_id, time, kind, data))

    def wakes(self, profile_id: int) -> list[tuple[int, int, str, str | None]]:
        return [tuple(r) for r in self.con.execute(
            "SELECT id, time, kind, data FROM pending_wakes WHERE profile_id = ? ORDER BY id", (profile_id,))]

    def drop_wakes(self, ids: list[int]) -> None:
        with self.con:
            self.con.executemany("DELETE FROM pending_wakes WHERE id = ?", [(i,) for i in ids])

    # --- Ayarlar ---

    def setting(self, key: str, default: Any = None) -> Any:
        r = self.con.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def set_setting(self, key: str, value: Any) -> None:
        with self.con:
            self.con.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, json.dumps(value)))

    # --- Sistem olayları ---

    def system_event(self, profile_id: int | None, time: int, kind: str, message: str,
                     detail: dict | None = None) -> None:
        with self.con:
            self.con.execute("INSERT INTO system_events (profile_id, time, kind, message, detail) "
                             "VALUES (?, ?, ?, ?, ?)",
                             (profile_id, time, kind, message,
                              json.dumps(detail, ensure_ascii=False) if detail is not None else None))

    def system_events(self, profile_id: int | None = None, limit: int = 300) -> list[dict]:
        if profile_id is None:
            rows = self.con.execute("SELECT * FROM system_events ORDER BY id DESC LIMIT ?", (limit,))
        else:
            rows = self.con.execute("SELECT * FROM system_events WHERE profile_id = ? ORDER BY id DESC LIMIT ?",
                                    (profile_id, limit))
        return [dict(r) for r in rows]
