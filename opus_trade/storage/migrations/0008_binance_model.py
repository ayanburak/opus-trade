"""
file: 0008_binance_model.py
importable: apply

Hesap defterini Binance'in hedge modu modeline taşır. Aynı hesap, parite ve
yöndeki açık pozisyonlar tek pozisyonda birleşir. Pozisyonların TP, SL ve
izleri miktarlı kapatma emirleri olur. Bekleyen emirler türü açık giriş
emirleri olur. Kaldıraç pozisyondan paritenin ayarına geçer. Geçmiş
işlemler silinmez, yalnızca numara sütunları düşer. migrate() bunu çekirdeğin
parite kurallarıyla, catalog ile çağırır.

description: Hesap defterinin Binance modeline göçü.
"""

# NOTE: Hesabın nakdi değişmez. Birleşen pozisyonun marjini, komisyonu ve
# fonlaması parçalarının toplamıdır, giriş fiyatı miktarla ağırlıklı
# ortalamadır. Böylece açık kâr ve özkaynak kuruşuna kadar aynı kalır. Yalnızca
# likidasyon fiyatı değişir, kademe toplam büyüklükten seçilir.
#
# NOTE: Birleşmeyen pozisyonun koruması miktarsız kurulur, eskisi gibi kalanın
# tamamını kapatır. Birleşen pozisyonda her parçanın koruması kendi
# miktarıyla kurulur, bir parçanın SL'si diğerinin miktarını kapatmasın.
#
# NOTE: Paritenin kaldıracı birleşenlerden en son açılanınkidir, Burak'ın 21.11
# kararı. Açık pozisyonu olmayan paritede en son verilen bekleyen emrinkidir,
# ikisi de yoksa parite ayarsız kalır ve varsayılanı kullanır. Bekleyen emrin
# miktarı kendi kaldıracıyla hesaplanır, emrin niyeti büyüklüğüdür.
#
# NOTE: Bekleyen emrin ve korumaların fiyatı paritenin fiyat adımına
# yuvarlanır, kapı yeni fiyatları da böyle yuvarlar. Eski kod adıma uymayan
# fiyat kabul ediyordu, canlı borsa onu reddeder. Kapanmış işlemlerin
# fiyatları geçmiştir, değişmez.
#
# XXX: Bu dosya bir kez uygulanır ve sonradan değişmez. İçe aktardığı kurallar
# değişirse yeni kurulan boş bir veritabanında sonuç etkilenmez, dönüştürülecek
# satır yoktur.

import json
import sqlite3

from opus_trade.domain import rules
from opus_trade.domain.catalog import Catalog
from opus_trade.domain.models import Side

_SCHEMA = """
CREATE TABLE positions_new (
    id           INTEGER PRIMARY KEY,
    account_id   INTEGER NOT NULL REFERENCES accounts (id),
    symbol       TEXT NOT NULL,
    side         TEXT NOT NULL,
    qty          REAL NOT NULL,
    margin       REAL NOT NULL,
    entry_price  REAL NOT NULL,
    entry_time   INTEGER NOT NULL,
    best_price   REAL NOT NULL,
    best_time    INTEGER NOT NULL,
    worst_price  REAL NOT NULL,
    worst_time   INTEGER NOT NULL,
    liq_price    REAL NOT NULL,
    fee          REAL NOT NULL DEFAULT 0,
    funding      REAL NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed'))
);
CREATE TABLE orders_new (
    id           INTEGER PRIMARY KEY,
    account_id   INTEGER NOT NULL REFERENCES accounts (id),
    number       INTEGER NOT NULL,
    symbol       TEXT NOT NULL,
    side         TEXT NOT NULL,
    type         TEXT NOT NULL CHECK (type IN ('limit', 'stop', 'take_profit', 'trailing_stop')),
    reduce       INTEGER NOT NULL,
    qty          REAL,
    price        REAL,
    trail_pct    REAL,
    best         REAL,
    margin       REAL NOT NULL DEFAULT 0,
    protection   TEXT NOT NULL DEFAULT '[]',
    expires_at   INTEGER,
    created_at   INTEGER NOT NULL,
    resolved_at  INTEGER,
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'filled', 'cancelled', 'expired', 'failed')),
    note         TEXT,
    position_id  INTEGER REFERENCES positions (id)
);
CREATE TABLE trades_new (
    id           INTEGER PRIMARY KEY,
    account_id   INTEGER NOT NULL REFERENCES accounts (id),
    position_id  INTEGER NOT NULL REFERENCES positions (id),
    symbol       TEXT NOT NULL,
    side         TEXT NOT NULL,
    qty          REAL NOT NULL,
    margin       REAL NOT NULL,
    entry_price  REAL NOT NULL,
    entry_time   INTEGER NOT NULL,
    exit_price   REAL NOT NULL,
    exit_time    INTEGER NOT NULL,
    reason       TEXT NOT NULL,
    pnl          REAL NOT NULL,
    fee          REAL NOT NULL,
    funding      REAL NOT NULL,
    best_price   REAL NOT NULL,
    worst_price  REAL NOT NULL,
    tp_prices    TEXT NOT NULL DEFAULT '[]',
    sl           REAL,
    order_id     INTEGER REFERENCES orders (id)
);
"""

# Eski tablolar silinip yenileri adlarını alınca kurulan dizinler. Açık
# pozisyonun ve bekleyen emir numarasının tekliğini veritabanı da zorlar.
_INDEXES = """
CREATE INDEX positions_open ON positions (account_id, status);
CREATE UNIQUE INDEX positions_one_open ON positions (account_id, symbol, side) WHERE status = 'open';
CREATE INDEX orders_pending ON orders (account_id, status);
CREATE UNIQUE INDEX orders_number ON orders (account_id, number) WHERE status = 'pending';
CREATE INDEX trades_account_time ON trades (account_id, exit_time);
CREATE TABLE leverages (
    account_id  INTEGER NOT NULL REFERENCES accounts (id),
    symbol      TEXT NOT NULL,
    leverage    INTEGER NOT NULL,
    PRIMARY KEY (account_id, symbol)
) WITHOUT ROWID;
"""

_POSITION_COLS = ("id", "account_id", "symbol", "side", "qty", "margin", "entry_price", "entry_time", "best_price",
                  "best_time", "worst_price", "worst_time", "liq_price", "fee", "funding", "status")


def _run(con: sqlite3.Connection, script: str) -> None:
    for stmt in script.split(";"):
        if stmt.strip():
            con.execute(stmt)


def _insert(con: sqlite3.Connection, table: str, row: dict) -> int:
    cur = con.execute(f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                      tuple(row.values()))
    return cur.lastrowid


# --- Pozisyonlar ---

# Birleşen pozisyonun en iyi ve en kötü fiyatı parçalarınkinin en lehte ve en
# aleyhte olanıdır, zamanı o fiyatı gören parçanınkidir.
def _merge(catalog: Catalog, group: list[dict]) -> dict:
    first, side = group[0], Side(group[0]["side"])
    better = max if side is Side.LONG else min
    worse = min if side is Side.LONG else max
    qty = sum(p["qty"] for p in group)
    margin = sum(p["margin"] for p in group)
    entry = sum(p["qty"] * p["entry_price"] for p in group) / qty
    best = better(group, key=lambda p: p["best_price"])
    worst = worse(group, key=lambda p: p["worst_price"])
    spec = catalog.get(first["symbol"])
    return {**{k: first[k] for k in _POSITION_COLS}, "qty": rules.round_qty(spec, qty), "margin": margin,
            "entry_price": entry, "best_price": best["best_price"], "best_time": best["best_time"],
            "worst_price": worst["worst_price"], "worst_time": worst["worst_time"],
            "liq_price": rules.liq_price(spec, side, entry, qty, margin),
            "fee": sum(p["fee"] for p in group), "funding": sum(p["funding"] for p in group)}


# Açık pozisyonları hesap, parite ve yöne göre gruplar, en eskisi başta.
# Kapanmış pozisyonlar olduğu gibi kalır.
def _move_positions(con: sqlite3.Connection, catalog: Catalog) -> tuple[dict[int, int], dict[tuple, list[dict]]]:
    rows = [dict(r) for r in con.execute("SELECT * FROM positions ORDER BY entry_time, id")]
    groups: dict[tuple, list[dict]] = {}
    for p in rows:
        if p["status"] == "open":
            groups.setdefault((p["account_id"], p["symbol"], p["side"]), []).append(p)
        else:
            _insert(con, "positions_new", {k: p[k] for k in _POSITION_COLS})
    moved = {}
    for group in groups.values():
        merged = _merge(catalog, group) if len(group) > 1 else {k: group[0][k] for k in _POSITION_COLS}
        _insert(con, "positions_new", merged)
        moved.update({p["id"]: group[0]["id"] for p in group})
    return moved, groups


# --- Emirler ---

# Eski emir yalnızca giriş emriydi. Maker emir lehe tarafta bekleyen limittir.
# Taker emir alışta fiyat düşerek tetiğe gelince take-profit, yükselerek
# gelince stop olur, satışta tersi.
def _entry_type(o: dict) -> str:
    if o["maker"]:
        return "limit"
    buy = o["side"] == Side.LONG
    return "stop" if buy != bool(o["below"]) else "take_profit"


# Eski emrin yüzdeli TP'leri girişin miktarıyla kademe miktarına çevrilir.
# Yüzdeler toplamı yüzse son kademe girişin kalanıdır ve miktarsız kalır.
def _protection(spec, o: dict, qty: float) -> list[dict]:
    out, levels = [], json.loads(o["tp_levels"])
    total = sum(k["pct"] for k in levels)
    for i, k in enumerate(levels):
        last = i == len(levels) - 1 and total >= 100 - rules.TOLERANCE
        out.append({"type": "take_profit", "price": rules.to_tick(spec, k["price"]), "trail_pct": None,
                    "qty": None if last else rules.round_qty(spec, qty * k["pct"] / 100)})
    if o["sl"]:
        out.append({"type": "stop", "price": rules.to_tick(spec, o["sl"]), "trail_pct": None, "qty": None})
    if o["trail_pct"]:
        out.append({"type": "trailing_stop", "price": None, "trail_pct": o["trail_pct"], "qty": None})
    return out


def _move_orders(con: sqlite3.Connection, catalog: Catalog, moved: dict[int, int]) -> None:
    for r in con.execute("SELECT * FROM orders ORDER BY id").fetchall():
        o = dict(r)
        spec = catalog.get(o["symbol"])
        price = rules.to_tick(spec, o["trigger_price"])
        qty = rules.round_qty(spec, o["margin"] * o["leverage"] / price)
        _insert(con, "orders_new", {
            "id": o["id"], "account_id": o["account_id"], "number": o["number"], "symbol": o["symbol"],
            "side": o["side"], "type": _entry_type(o), "reduce": 0, "qty": qty, "price": price,
            "margin": qty * price / o["leverage"],
            "protection": json.dumps(_protection(spec, o, qty)), "expires_at": o["expires_at"],
            "created_at": o["created_at"], "resolved_at": o["resolved_at"], "status": o["status"], "note": o["note"],
            "position_id": moved.get(o["position_id"], o["position_id"])})


def _free_number(con: sqlite3.Connection, account_id: int) -> int:
    used = {r[0] for r in con.execute("SELECT number FROM orders_new WHERE account_id = ? AND status = 'pending'",
                                      (account_id,))}
    n = 1
    while n in used:
        n += 1
    return n


# Bir parçanın korumaları. Birleşmeyen pozisyonda miktarsız, birleşende
# parçanın kendi miktarıyla kurulur. Miktarsız son TP kademesi parçanın diğer
# kademelerden kalanıdır.
def _protect(con: sqlite3.Connection, catalog: Catalog, p: dict, kept: int, alone: bool) -> None:
    spec = catalog.get(p["symbol"])
    levels = json.loads(p["tp_levels"])
    own = None if alone else p["qty"]
    fixed = sum(k["qty"] for k in levels if k["qty"] is not None)
    asks = []
    for k in levels:
        qty = k["qty"] if k["qty"] is not None else (None if alone else rules.round_qty(spec, p["qty"] - fixed))
        asks.append(("take_profit", rules.to_tick(spec, k["price"]), None, None, qty))
    if p["sl"]:
        asks.append(("stop", rules.to_tick(spec, p["sl"]), None, None, own))
    if p["trail_pct"]:
        asks.append(("trailing_stop", None, p["trail_pct"], p["best_price"], own))
    for kind, price, trail, best, qty in asks:
        _insert(con, "orders_new", {
            "account_id": p["account_id"], "number": _free_number(con, p["account_id"]), "symbol": p["symbol"],
            "side": p["side"], "type": kind, "reduce": 1, "qty": qty, "price": price, "trail_pct": trail,
            "best": best, "created_at": p["entry_time"], "position_id": kept})


# --- Kaldıraç ---

# Eski tablolar düşmeden önce okunur, yazma yeni kaldıraç tablosu kurulunca
# yapılır.
def _leverages(con: sqlite3.Connection, catalog: Catalog, groups: dict[tuple, list[dict]]) -> list[tuple]:
    latest: dict[tuple, tuple[int | None, float]] = {}
    for group in groups.values():
        for p in group:
            key = (p["account_id"], p["symbol"])
            if key not in latest or p["entry_time"] >= latest[key][0]:
                latest[key] = (p["entry_time"], p["leverage"])
    for o in con.execute("SELECT account_id, symbol, leverage, created_at FROM orders WHERE status = 'pending' "
                         "ORDER BY created_at"):
        key = (o["account_id"], o["symbol"])
        if key not in latest or latest[key][0] is None:
            latest[key] = (None, o["leverage"])
    out = []
    for (account, symbol), (_, lev) in latest.items():
        top = catalog.get(symbol).brackets[0].max_leverage
        out.append((account, symbol, min(max(round(lev), 1), top)))
    return out


# --- Taşıma ---

def apply(con: sqlite3.Connection, catalog: Catalog) -> None:
    """Defteri yeni modele taşır.

    Args:
        con: İşlemi açılmış, yabancı anahtar denetimi kapalı bağlantı.
        catalog: Parite kuralları, miktar adımı ve kademe tablosu için.
    """
    _run(con, _SCHEMA)
    moved, groups = _move_positions(con, catalog)
    _move_orders(con, catalog, moved)
    for group in groups.values():
        for p in sorted(group, key=lambda x: x["number"]):
            _protect(con, catalog, p, group[0]["id"], len(group) == 1)
    for t in con.execute("SELECT * FROM trades ORDER BY id").fetchall():
        t = dict(t)
        _insert(con, "trades_new", {
            **{k: t[k] for k in ("id", "account_id", "symbol", "side", "qty", "margin", "entry_price", "entry_time",
                                 "exit_price", "exit_time", "reason", "pnl", "fee", "funding", "best_price",
                                 "worst_price", "sl")},
            "position_id": moved.get(t["position_id"], t["position_id"]),
            "tp_prices": json.dumps([k["price"] for k in json.loads(t["tp_levels"])])})
    for old, new in moved.items():
        if old != new:
            con.execute("UPDATE account_events SET position_id = ? WHERE position_id = ?", (new, old))
    leverages = _leverages(con, catalog, groups)
    _run(con, "DROP TABLE trades; DROP TABLE orders; DROP TABLE positions;"
              "ALTER TABLE positions_new RENAME TO positions; ALTER TABLE orders_new RENAME TO orders;"
              "ALTER TABLE trades_new RENAME TO trades;")
    _run(con, _INDEXES)
    con.executemany("INSERT INTO leverages VALUES (?, ?, ?)", leverages)
