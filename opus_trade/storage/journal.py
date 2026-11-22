"""
file: journal.py
importable: JournalRepo

JournalRepo bir profilin karar günlüğünü, araç çağrılarını ve strateji
hafızasını saklar. add() bir uyanmanın kaydını araçlarıyla birlikte yazar,
recent() son kararların stratejiye giden kısa halini, entries() günlüğün
satırlarını döndürür. usage_rows() tüketim hesabı için bütün profillerin
çağrılarını yalnızca gereken alanlarıyla verir.

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
    # regimes parite başına rejim ve uygunluktur, şablon 6'dan önceki
    # kayıtlarda yoktur ve boş döner. rejected kontrol hatasıyla reddedilmiş
    # karardır.
    def recent(self, profile_id: int, limit: int) -> list[dict]:
        rows = self.con.execute("SELECT time, results, record FROM journal WHERE profile_id = ? AND error IS NULL "
                                "ORDER BY id DESC LIMIT ?", (profile_id, limit)).fetchall()
        out = []
        for t, res, rec in reversed(rows):
            r = json.loads(rec)
            pairs = (r.get("decision") or {}).get("pariteler") or {}
            regimes = {s: (p["rejim"]["rejim"], p["rejim"]["uygun"]) for s, p in pairs.items()
                       if isinstance(p, dict) and isinstance(p.get("rejim"), dict)}
            out.append({"time": t, "summary": r.get("summary", ""), "results": [x["message"] for x in json.loads(res)],
                        "regimes": regimes, "rejected": bool(r.get("rejection"))})
        return out

    # Kayıt alanı büyük olduğu için yalnızca an ve istenen uyanma okunur.
    def last_success(self, profile_id: int) -> tuple[int, int | None] | None:
        """Son başarılı kararın anını ve istediği uyanma anını döndürür, hiç yoksa None."""
        r = self.con.execute("SELECT time, next_wake FROM journal WHERE profile_id = ? AND error IS NULL "
                             "ORDER BY id DESC LIMIT 1", (profile_id,)).fetchone()
        return (r[0], r[1]) if r else None

    def entries(self, profile_id: int, limit: int = 300, before: int | None = None) -> list[dict]:
        sql = "SELECT * FROM journal WHERE profile_id = ?"
        args: tuple = (profile_id,)
        if before is not None:
            sql, args = sql + " AND id < ?", (profile_id, before)
        rows = self.con.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
        return [{**dict(r), "wakes": json.loads(r["wakes"]), "results": json.loads(r["results"]),
                 "record": json.loads(r["record"])} for r in rows]

    def get(self, journal_id: int) -> dict:
        r = self.con.execute("SELECT * FROM journal WHERE id = ?", (journal_id,)).fetchone()
        return {**dict(r), "wakes": json.loads(r["wakes"]), "results": json.loads(r["results"]),
                "record": json.loads(r["record"])}

    def profile_of(self, journal_id: int) -> int | None:
        r = self.con.execute("SELECT profile_id FROM journal WHERE id = ?", (journal_id,)).fetchone()
        return r[0] if r else None

    def tool_calls(self, journal_id: int) -> list[dict]:
        return [dict(r) for r in self.con.execute("SELECT * FROM tool_calls WHERE journal_id = ? ORDER BY seq",
                                                 (journal_id,))]

    # Kayıt alanında Opus'a giden metin ve ham cevap da durur, büyüktür.
    # Yalnızca gereken alanlar SQLite'ın içinde çıkarılır, kayıt Python'a
    # hiç taşınmaz.
    def usage_rows(self, since: int) -> list[tuple]:
        """since anından sonraki günlük satırlarını (profile_id, time, error, usage, cost_usd, duration_s) olarak döndürür.

        usage claude komutunun usage sözlüğünün JSON metnidir, çağrı yoksa None. error hata varsa 1'dir.
        """
        return [tuple(r) for r in self.con.execute(
            "SELECT profile_id, time, error IS NOT NULL, json_extract(record, '$.usage.usage'), "
            "json_extract(record, '$.cost_usd'), json_extract(record, '$.duration_s') FROM journal WHERE time >= ?",
            (since,))]
