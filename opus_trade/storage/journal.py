"""
file: journal.py
importable: JournalRepo

JournalRepo bir profilin karar günlüğünü, araç çağrılarını ve strateji
hafızasını saklar. add() bir uyanmanın kaydını araçlarıyla birlikte yazar,
recent() son kararların stratejiye giden kısa halini, entries() günlüğün
satırlarını döndürür.

description: Karar günlüğünün ve strateji hafızasının SQLite deposu.
"""

# NOTE: Araçların sonuçları büyük olabildiği için günlük satırına girmez,
# tool_calls tablosuna her araç bir satır olarak yazılır. Günlükteki record
# alanında araçların sonuçsuz kısa listesi durur.

import json
import sqlite3
from typing import Any


class JournalRepo:
    def __init__(self, con: sqlite3.Connection) -> None:
        self.con = con

    def memory(self, profile_id: int) -> dict[str, Any]:
        r = self.con.execute("SELECT data FROM memory WHERE profile_id = ?", (profile_id,)).fetchone()
        return json.loads(r[0]) if r else {}

    def set_memory(self, profile_id: int, data: dict[str, Any]) -> None:
        with self.con:
            self.con.execute("INSERT OR REPLACE INTO memory VALUES (?, ?)",
                             (profile_id, json.dumps(data, ensure_ascii=False)))

    def add(self, profile_id: int, version_id: int, time: int, wakes: list[dict], results: list[dict],
            record: dict, error: str | None, next_wake: int | None) -> int:
        """Bir uyanmanın kaydını yazar ve günlük satırının kimliğini döndürür."""
        record = dict(record)
        tools = record.pop("tools", [])
        record["tools"] = [{k: v for k, v in t.items() if k not in ("result", "id")} for t in tools]
        with self.con:
            cur = self.con.execute(
                "INSERT INTO journal (profile_id, version_id, time, wakes, results, record, error, next_wake) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (profile_id, version_id, time, json.dumps(wakes, ensure_ascii=False),
                 json.dumps(results, ensure_ascii=False), json.dumps(record, ensure_ascii=False), error, next_wake))
            self.con.executemany(
                "INSERT INTO tool_calls (journal_id, seq, name, title, input, result, is_error, duration_s, sub) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(cur.lastrowid, i, t["name"], t.get("title"), json.dumps(t.get("input"), ensure_ascii=False),
                  t.get("result"), t.get("is_error"), t.get("duration_s"), int(bool(t.get("sub"))))
                 for i, t in enumerate(tools)])
        return cur.lastrowid

    # Stratejiye giden son kararlar yalnızca başarılı uyanmalardır. Özet
    # kaydın summary alanından, sonuçlar komutların metinlerinden gelir.
    def recent(self, profile_id: int, limit: int) -> list[dict]:
        rows = self.con.execute("SELECT time, results, record FROM journal WHERE profile_id = ? AND error IS NULL "
                                "ORDER BY id DESC LIMIT ?", (profile_id, limit)).fetchall()
        return [{"time": t, "summary": json.loads(rec).get("summary", ""),
                 "results": [x["message"] for x in json.loads(res)]} for t, res, rec in reversed(rows)]

    def entries(self, profile_id: int, limit: int = 300, before: int | None = None) -> list[dict]:
        sql = "SELECT * FROM journal WHERE profile_id = ?"
        args: tuple = (profile_id,)
        if before is not None:
            sql, args = sql + " AND id < ?", (profile_id, before)
        rows = self.con.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
        return [{**dict(r), "wakes": json.loads(r["wakes"]), "results": json.loads(r["results"]),
                 "record": json.loads(r["record"])} for r in rows]

    def tool_calls(self, journal_id: int) -> list[dict]:
        return [dict(r) for r in self.con.execute("SELECT * FROM tool_calls WHERE journal_id = ? ORDER BY seq",
                                                 (journal_id,))]
