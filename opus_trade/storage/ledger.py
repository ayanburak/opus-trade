"""
file: ledger.py
importable: MIGRATIONS, Ledger

Ledger hesap defterinin deposudur: hesaplar, paritelerin kaldıracı,
pozisyonlar, emirler, kapanmış işlemler, hesap olayları ve özkaynak.
Broker'lar hesabın durumunu yalnızca bunun üzerinden okur ve yazar. Yazan
metotlar işlemi kendileri kapatmaz, birbirine bağlı birkaç yazma atomic()
içinde yapılır ve ya hepsi ya hiçbiri kalıcı olur. MIGRATIONS çekirdek veritabanının taşıma klasörüdür.

description: Hesap defterinin SQLite deposu.
"""

# NOTE: Kâğıt broker'da doğruluğun tek kaynağı bu defterdir. Canlı broker'da
# doğruluğun kaynağı Binance'tir ve defter onun kaydıdır, aynı tablolar
# kullanılır.
#
# NOTE: Görünen numara bekleyen emirlerin kullanmadığı en küçük sayıdır,
# free_number() ile atomic() içinde seçilir. Veritabanı da bekleyen emirlerde
# numaranın, açık pozisyonlarda parite ve yönün tekliğini dizinle zorlar.
#
# NOTE: Emrin koruma istekleri ve işlemin TP fiyatları JSON metni olarak
# tutulur. İkisi de yalnızca birlikte okunur, ayrı tabloya değmez.

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Iterator

from ..domain.models import Side
from ..domain.trading import AccountEvent, Order, OrderType, Position, Protection, Trade

MIGRATIONS = Path(__file__).parent / "migrations"

_POSITION_FIELDS = ("symbol", "side", "qty", "margin", "entry_price", "entry_time", "best_price", "best_time",
                    "worst_price", "worst_time", "liq_price", "fee", "funding")

_ORDER_FIELDS = ("account_id", "number", "symbol", "side", "type", "reduce", "qty", "price", "trail_pct", "best",
                 "margin", "protection", "expires_at", "created_at", "by_last")


def _protection_json(items: tuple[Protection, ...]) -> str:
    return json.dumps([asdict(x) for x in items])


def _protection(text: str) -> tuple[Protection, ...]:
    return tuple(Protection(**{**x, "type": OrderType(x["type"])}) for x in json.loads(text))


def _position(r: sqlite3.Row) -> Position:
    return Position(id=r["id"], account_id=r["account_id"], **{k: r[k] for k in _POSITION_FIELDS if k != "side"},
                    side=Side(r["side"]))


def _order(r: sqlite3.Row) -> Order:
    return Order(id=r["id"], account_id=r["account_id"], number=r["number"], symbol=r["symbol"],
                 side=Side(r["side"]), type=OrderType(r["type"]), reduce=bool(r["reduce"]), qty=r["qty"],
                 price=r["price"], trail_pct=r["trail_pct"], best=r["best"], margin=r["margin"],
                 protection=_protection(r["protection"]), expires_at=r["expires_at"], created_at=r["created_at"],
                 by_last=bool(r["by_last"]))


def _trade(r: sqlite3.Row) -> Trade:
    rest = {k: r[k] for k in r.keys() if k not in ("side", "tp_prices")}
    return Trade(**rest, side=Side(r["side"]), tp_prices=tuple(json.loads(r["tp_prices"])))


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

    def close_account(self, account_id: int, now: int) -> None:
        with self.con:
            self.con.execute("UPDATE accounts SET closed_at = ? WHERE id = ?", (now, account_id))

    def start_balance(self, account_id: int) -> float:
        return self.con.execute("SELECT start_balance FROM accounts WHERE id = ?", (account_id,)).fetchone()[0]

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
        return tuple(_position(r) for r in self.con.execute(sql + " ORDER BY id", args))

    def orders(self, account_id: int, symbol: str | None = None) -> tuple[Order, ...]:
        sql = "SELECT * FROM orders WHERE account_id = ? AND status = 'pending'"
        args: tuple = (account_id,)
        if symbol:
            sql, args = sql + " AND symbol = ?", (account_id, symbol)
        return tuple(_order(r) for r in self.con.execute(sql + " ORDER BY number", args))

    def position(self, position_id: int) -> Position:
        return _position(self.con.execute("SELECT * FROM positions WHERE id = ?", (position_id,)).fetchone())

    def order(self, order_id: int) -> Order:
        return _order(self.con.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone())

    def trades(self, account_id: int, since: int | None = None) -> tuple[Trade, ...]:
        rows = self.con.execute("SELECT * FROM trades WHERE account_id = ? AND exit_time >= ? ORDER BY id",
                                (account_id, since or 0))
        return tuple(_trade(r) for r in rows)

    # Hesap gerçekleri olayların kimin yaptığını ve hangi emrin hangi
    # pozisyonu açtığını buradan okur. detail olayın tamamıdır.
    def events(self, account_id: int, since: int = 0, kinds: tuple[str, ...] | None = None) -> list[dict]:
        """since anından sonraki hesap olaylarını eskiden yeniye döndürür, kinds verilirse yalnızca o türleri."""
        sql, args = "SELECT * FROM account_events WHERE account_id = ? AND time >= ?", [account_id, since]
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            args += list(kinds)
        return [{**dict(r), "detail": json.loads(r["detail"])} for r in self.con.execute(sql + " ORDER BY id", args)]

    # Bir pozisyonun bütün parçaları birlikte döner, kısmi kapanışlar da.
    # Pozisyon tamamen kapandığında status closed olur, son parçanın zamanı
    # kapanma anıdır.
    def closed_positions(self, account_id: int, since: int = 0) -> list[tuple[int, tuple[Trade, ...]]]:
        """since anından sonra tamamen kapanan pozisyonları kapanma sırasıyla, parçalarıyla döndürür."""
        rows = self.con.execute(
            "SELECT t.* FROM trades t JOIN positions p ON p.id = t.position_id WHERE t.account_id = ? "
            "AND p.status = 'closed' AND t.position_id IN (SELECT position_id FROM trades WHERE account_id = ? "
            "GROUP BY position_id HAVING MAX(exit_time) >= ?) ORDER BY t.id", (account_id, account_id, since))
        grouped: dict[int, list[Trade]] = {}
        for r in rows:
            grouped.setdefault(r["position_id"], []).append(_trade(r))
        return sorted(((pid, tuple(ts)) for pid, ts in grouped.items()), key=lambda x: (x[1][-1].exit_time, x[0]))

    # Dolma oranı için çözülmüş giriş emirleri. Piyasa girişi defterde
    # beklemez, sayılmaz.
    def resolved_entries(self, account_id: int, limit: int) -> list[dict]:
        """Son çözülmüş bekleyen giriş emirlerini yeniden eskiye id, status ve resolved_at ile döndürür."""
        return [dict(r) for r in self.con.execute(
            "SELECT id, status, resolved_at FROM orders WHERE account_id = ? AND reduce = 0 AND type != 'market' "
            "AND status != 'pending' ORDER BY resolved_at DESC, id DESC LIMIT ?", (account_id, limit))]

    def leverages(self, account_id: int) -> dict[str, int]:
        rows = self.con.execute("SELECT symbol, leverage FROM leverages WHERE account_id = ?", (account_id,))
        return {r[0]: r[1] for r in rows}

    def free_number(self, account_id: int) -> int:
        used = {r[0] for r in self.con.execute(
            "SELECT number FROM orders WHERE account_id = ? AND status = 'pending'", (account_id,))}
        n = 1
        while n in used:
            n += 1
        return n

    # --- Yazma ---

    def set_leverage(self, account_id: int, symbol: str, leverage: int) -> None:
        self.con.execute("INSERT OR REPLACE INTO leverages VALUES (?, ?, ?)", (account_id, symbol, leverage))

    def insert_position(self, account_id: int, **fields) -> Position:
        fields["side"] = str(fields["side"])
        cols = ("account_id",) + _POSITION_FIELDS
        cur = self.con.execute(f"INSERT INTO positions ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                               (account_id, *(fields[c] for c in _POSITION_FIELDS)))
        return self.position(cur.lastrowid)

    def update_position(self, position_id: int, **fields) -> Position:
        self.con.execute(f"UPDATE positions SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                         (*fields.values(), position_id))
        return self.position(position_id)

    def mark_closed(self, position_id: int) -> None:
        self.con.execute("UPDATE positions SET status = 'closed' WHERE id = ?", (position_id,))

    def insert_trade(self, t: Trade) -> Trade:
        row = asdict(t)
        row.pop("id")
        row["side"] = str(row["side"])
        row["tp_prices"] = json.dumps(list(t.tp_prices))
        cur = self.con.execute(f"INSERT INTO trades ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                               tuple(row.values()))
        return _trade(self.con.execute("SELECT * FROM trades WHERE id = ?", (cur.lastrowid,)).fetchone())

    def insert_order(self, o: Order) -> Order:
        row = {**{k: getattr(o, k) for k in _ORDER_FIELDS}, "side": str(o.side), "type": str(o.type),
               "reduce": int(o.reduce), "protection": _protection_json(o.protection), "by_last": int(o.by_last)}
        cur = self.con.execute(f"INSERT INTO orders ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                               tuple(row.values()))
        return self.order(cur.lastrowid)

    # Fiyatı değişen limit emir ve en iyi fiyatı ilerleyen iz bunu kullanır.
    def update_order(self, order_id: int, **fields) -> Order:
        self.con.execute(f"UPDATE orders SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                         (*fields.values(), order_id))
        return self.order(order_id)

    def resolve_order(self, order_id: int, status: str, now: int, note: str | None = None,
                      position_id: int | None = None) -> None:
        self.con.execute("UPDATE orders SET status = ?, resolved_at = ?, note = ?, "
                         "position_id = COALESCE(?, position_id) WHERE id = ?",
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
