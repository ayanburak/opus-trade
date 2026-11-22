"""
file: notes.py
importable: EntryNote, NotesRepo

NotesRepo Opus'un kararlarına bağlı notları saklar: girişlerin gerekçesi ve
geçersizlik seviyesi, kapanan pozisyonların sınıflandırması ve fiyatın bir
geçersizlik seviyesine değdiği anlar. add_entry() bir girişin notunu emre ya
da pozisyona bağlı yazar, attach_position() dolan emrin notunu pozisyona
bağlar. pending_invalidations() henüz işlenmemiş notları verir, mark_hit()
bir notu işlenmiş yapar. add_review() bir pozisyonu sınıflandırır.

description: Giriş notlarının, pozisyon sınıflandırmalarının ve geçersizlik kayıtlarının deposu.
"""

# NOTE: Bu notlar hesap defterinde değil burada durur. Broker Binance'in
# modelidir ve canlı broker geldiğinde hesabın doğrusu Binance olacak.
# Binance bir emrin gerekçesini ya da geçersizlik seviyesini tutmaz. Defter
# ve broker bu yüzden bu bilgiye hiç dokunmaz, notu stratejinin ve
# çalıştırıcının katmanı yazar ve okur.
#
# NOTE: Bir pozisyonun birden çok notu olabilir, aynı yönde iki giriş tek
# pozisyonda birleşir. for_position() hepsini eskiden yeniye verir.

import json
import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class EntryNote:
    """Bir girişin notu. reason Opus'un giriş alanlarıdır: karsi_kanit, risk_beyani ve varsa son_stoptan_farki."""

    id: int
    profile_id: int
    account_id: int
    order_id: int | None
    position_id: int | None
    symbol: str
    side: str
    reason: dict
    invalid_price: float
    invalid_text: str
    created_at: int
    journal_id: int | None


def _note(r: sqlite3.Row) -> EntryNote:
    return EntryNote(**{**dict(r), "reason": json.loads(r["reason"])})


class NotesRepo:
    def __init__(self, con: sqlite3.Connection) -> None:
        self.con = con

    # --- Giriş notları ---

    def add_entry(self, profile_id: int, account_id: int, symbol: str, side: str, reason: dict,
                  invalid_price: float, invalid_text: str, now: int, journal_id: int | None = None,
                  order_id: int | None = None, position_id: int | None = None) -> int:
        """Bir girişin notunu yazar ve kimliğini döndürür."""
        with self.con:
            cur = self.con.execute(
                "INSERT INTO entry_notes (profile_id, account_id, order_id, position_id, symbol, side, reason, "
                "invalid_price, invalid_text, created_at, journal_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (profile_id, account_id, order_id, position_id, symbol, side, json.dumps(reason, ensure_ascii=False),
                 invalid_price, invalid_text, now, journal_id))
        return cur.lastrowid

    # Emir dolunca çağrılır. Notu olmayan emirde hiçbir şey değişmez.
    def attach_position(self, order_id: int, position_id: int) -> None:
        with self.con:
            self.con.execute("UPDATE entry_notes SET position_id = ? WHERE order_id = ?", (position_id, order_id))

    def for_order(self, order_id: int) -> EntryNote | None:
        r = self.con.execute("SELECT * FROM entry_notes WHERE order_id = ? ORDER BY id DESC LIMIT 1",
                             (order_id,)).fetchone()
        return _note(r) if r else None

    def for_position(self, position_id: int) -> list[EntryNote]:
        return [_note(r) for r in self.con.execute(
            "SELECT * FROM entry_notes WHERE position_id = ? ORDER BY id", (position_id,))]

    def for_positions(self, position_ids: list[int]) -> dict[int, list[EntryNote]]:
        out: dict[int, list[EntryNote]] = {}
        for pid in position_ids:
            if found := self.for_position(pid):
                out[pid] = found
        return out

    # --- Geçersizlik ---

    # Yalnızca hâlâ bekleyen emirlerin ve açık pozisyonların notları işe
    # yarar. Hangilerinin açık olduğunu defter bilir, çağıran kimlikleri
    # verir. İşlenmiş notlar dönmez.
    def pending_invalidations(self, profile_id: int, order_ids: list[int],
                              position_ids: list[int]) -> list[EntryNote]:
        """Verilen emir ve pozisyonların henüz işlenmemiş notlarını döndürür."""
        if not order_ids and not position_ids:
            return []
        marks_o = ",".join("?" * len(order_ids)) or "NULL"
        marks_p = ",".join("?" * len(position_ids)) or "NULL"
        rows = self.con.execute(
            f"SELECT n.* FROM entry_notes n LEFT JOIN invalidation_hits h ON h.note_id = n.id "
            f"WHERE n.profile_id = ? AND h.note_id IS NULL AND ((n.position_id IS NULL AND n.order_id IN ({marks_o})) "
            f"OR n.position_id IN ({marks_p})) ORDER BY n.id", (profile_id, *order_ids, *position_ids))
        return [_note(r) for r in rows]

    def mark_hit(self, note_id: int, time: int, action: str) -> bool:
        """Notu işlenmiş yapar. Not zaten işlenmişse False döndürür."""
        with self.con:
            cur = self.con.execute("INSERT OR IGNORE INTO invalidation_hits VALUES (?, ?, ?)", (note_id, time, action))
        return cur.rowcount == 1

    # Son çağrıdan beri sistemin geçersizlik yüzünden iptal ettiği emirlerin
    # notları, Opus'a hesap bölümünde gösterilir.
    def cancelled_since(self, profile_id: int, since: int) -> list[tuple[EntryNote, int]]:
        rows = self.con.execute(
            "SELECT n.*, h.time AS hit_time FROM entry_notes n JOIN invalidation_hits h ON h.note_id = n.id "
            "WHERE n.profile_id = ? AND h.action = 'cancelled' AND h.time > ? ORDER BY h.time", (profile_id, since))
        return [(_note({k: r[k] for k in r.keys() if k != "hit_time"}), r["hit_time"]) for r in rows]

    # --- Sınıflandırma ---

    def add_review(self, position_id: int, profile_id: int, journal_id: int | None, verdict: str, text: str,
                   now: int) -> None:
        """Bir pozisyonu sınıflandırır. Daha önce sınıflandırılmışsa ilk sınıflandırma kalır."""
        with self.con:
            self.con.execute("INSERT OR IGNORE INTO position_reviews VALUES (?, ?, ?, ?, ?, ?)",
                             (position_id, profile_id, journal_id, verdict, text, now))

    def reviewed_ids(self, profile_id: int) -> set[int]:
        return {r[0] for r in self.con.execute("SELECT position_id FROM position_reviews WHERE profile_id = ?",
                                               (profile_id,))}

    def reviews(self, position_ids: list[int]) -> dict[int, dict]:
        """Verilen pozisyonların sınıflandırmalarını position_id ile döndürür."""
        if not position_ids:
            return {}
        marks = ",".join("?" * len(position_ids))
        return {r["position_id"]: dict(r) for r in self.con.execute(
            f"SELECT * FROM position_reviews WHERE position_id IN ({marks})", position_ids)}
