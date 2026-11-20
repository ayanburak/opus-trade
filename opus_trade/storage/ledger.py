"""
file: ledger.py
importable: MIGRATIONS, Ledger

Ledger hesap defterinin deposudur: hesaplar, pozisyonlar, emirler, kapanmış
işlemler, hesap olayları ve özkaynak. Broker'lar hesabın durumunu yalnızca
bunun üzerinden okur ve yazar. Yazan metotlar işlemi kendileri kapatmaz,
birbirine bağlı birkaç yazma atomic() içinde yapılır ve ya hepsi ya hiçbiri
kalıcı olur. MIGRATIONS çekirdek veritabanının taşıma klasörüdür.

description: Hesap defterinin SQLite deposu.
"""

# NOTE: Kâğıt broker'da doğruluğun tek kaynağı bu defterdir. Canlı broker'da
# doğruluğun kaynağı Binance'tir ve defter onun kaydıdır, aynı tablolar
# kullanılır.
#
# NOTE: Görünen numara açık pozisyonlar ile bekleyen emirlerin birlikte
# kullanmadığı en küçük sayıdır. Veritabanında bunu tek bir kısıtla
# zorlamak iki tabloya yayıldığı için mümkün değil, free_number() ile
# atomic() içinde seçilir. Program tek yazarlı olduğu için yarış olmaz.

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Iterator

from ..domain.models import Side, TpLevel
from ..domain.trading import AccountEvent, Order, Position, Trade

MIGRATIONS = Path(__file__).parent / "migrations"

_POSITION_FIELDS = ("number", "symbol", "side", "leverage", "qty", "margin", "entry_price", "entry_time", "tp_levels",
                    "sl", "trail_pct", "best_price", "best_time", "worst_price", "worst_time", "liq_price", "fee",
                    "funding", "order_id")


def _levels_json(levels: tuple[TpLevel, ...]) -> str:
    return json.dumps([asdict(k) for k in levels])


def _levels(text: str) -> tuple[TpLevel, ...]:
    return tuple(TpLevel(**k) for k in json.loads(text))


def _position(r: sqlite3.Row) -> Position:
    return Position(id=r["id"], account_id=r["account_id"], number=r["number"], symbol=r["symbol"],
                    side=Side(r["side"]), leverage=r["leverage"], qty=r["qty"], margin=r["margin"],
                    entry_price=r["entry_price"], entry_time=r["entry_time"], tp_levels=_levels(r["tp_levels"]),
                    sl=r["sl"], trail_pct=r["trail_pct"], best_price=r["best_price"], best_time=r["best_time"],
                    worst_price=r["worst_price"], worst_time=r["worst_time"], liq_price=r["liq_price"],
                    fee=r["fee"], funding=r["funding"], order_id=r["order_id"])


def _order(r: sqlite3.Row) -> Order:
    return Order(id=r["id"], account_id=r["account_id"], number=r["number"], symbol=r["symbol"],
                 side=Side(r["side"]), trigger=r["trigger_price"], below=bool(r["below"]), maker=bool(r["maker"]),
                 leverage=r["leverage"], margin=r["margin"], tp_levels=_levels(r["tp_levels"]), sl=r["sl"],
                 trail_pct=r["trail_pct"], expires_at=r["expires_at"], created_at=r["created_at"])


def _trade(r: sqlite3.Row) -> Trade:
    rest = {k: r[k] for k in r.keys() if k not in ("side", "tp_levels")}
    return Trade(**rest, side=Side(r["side"]), tp_levels=_levels(r["tp_levels"]))


# Olayın detayı JSON'a çevrilirken Trade gibi iç içe tipler de sözlük olur,
# Side ve Actor gibi StrEnum'lar metin olarak kalır.
def _event_json(e: AccountEvent) -> str:
    return json.dumps(asdict(e), ensure_ascii=False)


class Ledger:
    """Hesap defteri.

    Args:
        con: Çekirdek veritabanına migrate() ile güncellenmiş, yazılabilir bağlantı.
    """

    def __init__(self, con: sqlite3.Connection) -> None:
        self.con = con

    @contextmanager
    def atomic(self) -> Iterator[None]:
        """İçindeki yazmaları tek işlemde yapar, hata olursa hepsini geri alır."""
        with self.con:
            yield

    # --- Hesap ---

    def create_account(self, kind: str, balance: float, now: int) -> int:
        with self.con:
            cur = self.con.execute("INSERT INTO accounts (kind, start_balance, cash, created_at) VALUES (?, ?, ?, ?)",
                                   (kind, balance, balance, now))
        return cur.lastrowid

    def cash(self, account_id: int) -> float:
        return self.con.execute("SELECT cash FROM accounts WHERE id = ?", (account_id,)).fetchone()[0]

    def set_cash(self, account_id: int, value: float) -> None:
        self.con.execute("UPDATE accounts SET cash = ? WHERE id = ?", (round(value, 10), account_id))

    # --- Okuma ---

    def positions(self, account_id: int, symbol: str | None = None) -> tuple[Position, ...]:
        sql = "SELECT * FROM positions WHERE account_id = ? AND status = 'open'"
        args: tuple = (account_id,)
        if symbol:
            sql, args = sql + " AND symbol = ?", (account_id, symbol)
        return tuple(_position(r) for r in self.con.execute(sql + " ORDER BY number", args))

    def orders(self, account_id: int, symbol: str | None = None) -> tuple[Order, ...]:
        sql = "SELECT * FROM orders WHERE account_id = ? AND status = 'pending'"
        args: tuple = (account_id,)
        if symbol:
            sql, args = sql + " AND symbol = ?", (account_id, symbol)
        return tuple(_order(r) for r in self.con.execute(sql + " ORDER BY number", args))

    def position(self, position_id: int) -> Position:
        return _position(self.con.execute("SELECT * FROM positions WHERE id = ?", (position_id,)).fetchone())

    def trades(self, account_id: int, since: int | None = None) -> tuple[Trade, ...]:
        rows = self.con.execute("SELECT * FROM trades WHERE account_id = ? AND exit_time >= ? ORDER BY id",
                                (account_id, since or 0))
        return tuple(_trade(r) for r in rows)

    def free_number(self, account_id: int) -> int:
        used = {r[0] for r in self.con.execute(
            "SELECT number FROM positions WHERE account_id = ? AND status = 'open' "
            "UNION SELECT number FROM orders WHERE account_id = ? AND status = 'pending'", (account_id, account_id))}
        n = 1
        while n in used:
            n += 1
        return n

    # --- Yazma ---

    def insert_position(self, account_id: int, **fields) -> Position:
        fields["tp_levels"] = _levels_json(fields["tp_levels"])
        fields["side"] = str(fields["side"])
        cols = ("account_id",) + _POSITION_FIELDS
        cur = self.con.execute(f"INSERT INTO positions ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                               (account_id, *(fields[c] for c in _POSITION_FIELDS)))
        return self.position(cur.lastrowid)

    def update_position(self, position_id: int, **fields) -> Position:
        if "tp_levels" in fields:
            fields["tp_levels"] = _levels_json(fields["tp_levels"])
        self.con.execute(f"UPDATE positions SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                         (*fields.values(), position_id))
        return self.position(position_id)

    def mark_closed(self, position_id: int) -> None:
        self.con.execute("UPDATE positions SET status = 'closed' WHERE id = ?", (position_id,))

    def insert_trade(self, t: Trade) -> Trade:
        row = asdict(t)
        row.pop("id")
        row["side"] = str(row["side"])
        row["tp_levels"] = _levels_json(t.tp_levels)
        cur = self.con.execute(f"INSERT INTO trades ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                               tuple(row.values()))
        return _trade(self.con.execute("SELECT * FROM trades WHERE id = ?", (cur.lastrowid,)).fetchone())

    def insert_order(self, o: Order) -> Order:
        cur = self.con.execute(
            "INSERT INTO orders (account_id, number, symbol, side, trigger_price, below, maker, leverage, margin, "
            "tp_levels, sl, trail_pct, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (o.account_id, o.number, o.symbol, str(o.side), o.trigger, int(o.below), int(o.maker), o.leverage,
             o.margin, _levels_json(o.tp_levels), o.sl, o.trail_pct, o.expires_at, o.created_at))
        return _order(self.con.execute("SELECT * FROM orders WHERE id = ?", (cur.lastrowid,)).fetchone())

    def resolve_order(self, order_id: int, status: str, now: int, note: str | None = None,
                      position_id: int | None = None) -> None:
        self.con.execute("UPDATE orders SET status = ?, resolved_at = ?, note = ?, position_id = ? WHERE id = ?",
                         (status, now, note, position_id, order_id))

    def log_event(self, e: AccountEvent) -> None:
        self.con.execute(
            "INSERT INTO account_events (account_id, time, actor, kind, number, symbol, position_id, order_id, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (e.account_id, e.time, str(e.actor), str(e.kind), e.number, e.symbol, e.position_id, e.order_id,
             _event_json(e)))

    # Aynı saniyede ikinci kayıt öncekinin üstüne yazılır.
    def record_equity(self, account_id: int, time: int, cash: float, equity: float, margin: float) -> None:
        with self.con:
            self.con.execute("INSERT OR REPLACE INTO equity VALUES (?, ?, ?, ?, ?)",
                             (account_id, time, cash, equity, margin))
