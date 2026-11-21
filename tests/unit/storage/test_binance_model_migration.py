"""
file: test_binance_model_migration.py
importable: yok

Hesap defterinin Binance modeline göçünün testleri. Göç canlı profillerin
açık pozisyonlarını ve bekleyen emirlerini dönüştürür, bir hata sessizce para
kaybettirir ya da korumasız bir pozisyon bırakır. Veritabanı eski şemayla
elle kurulur, satırlar eski kodun yazacağı biçimdedir.

description: 0008_binance_model taşıması için birim testleri.
"""

import json
import shutil

import pytest

from opus_trade.domain import rules
from opus_trade.domain.models import Side
from opus_trade.domain.trading import OrderType
from opus_trade.storage.database import connect
from opus_trade.storage.ledger import MIGRATIONS, Ledger
from opus_trade.storage.migrate import migrate
from tests.fakes import catalog

T = 1_795_000_000
SOL = catalog().get("SOLUSDT")


def _row(con, table: str, **row) -> int:
    for k, v in row.items():
        if isinstance(v, list):
            row[k] = json.dumps(v)
    cur = con.execute(f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                      tuple(row.values()))
    return cur.lastrowid


def _position(con, account: int, number: int, symbol: str, side: str, lev: float, qty: float, entry: float,
              t: int, **rest) -> int:
    row = dict(account_id=account, number=number, symbol=symbol, side=side, leverage=lev, qty=qty,
               margin=qty * entry / lev, entry_price=entry, entry_time=t, best_price=entry, best_time=t,
               worst_price=entry, worst_time=t, liq_price=1, fee=0.1, funding=0.01, tp_levels=[])
    return _row(con, "positions", **{**row, **rest})


def _order(con, account: int, number: int, symbol: str, side: str, trigger: float, below: bool, maker: bool,
           lev: float = 10, margin: float = 10, **rest) -> int:
    row = dict(account_id=account, number=number, symbol=symbol, side=side, trigger_price=trigger, below=int(below),
               maker=int(maker), leverage=lev, margin=margin, tp_levels=[], created_at=T)
    return _row(con, "orders", **{**row, **rest})


# Eski şema 0007'ye kadar kurulur, satırlar yazılır, sonra 0008 uygulanır.
# Göçten önceki toplamlar karşılaştırma için döner.
@pytest.fixture
def db(tmp_path):
    old = tmp_path / "old"
    old.mkdir()
    for f in sorted(MIGRATIONS.glob("000[1-7]_*.sql")):
        shutil.copy(f, old)
    con = connect(tmp_path / "core.db")
    migrate(con, old, tmp_path / "bak")
    con.execute("INSERT INTO accounts (id, kind, start_balance, cash, created_at) VALUES (1, 'paper', 1000, 900, ?)",
                (T,))
    ids = {
        "a": _position(con, 1, 1, "SOLUSDT", "long", 10, 1, 100, T, best_price=105, best_time=T + 5,
                       worst_price=95, worst_time=T + 9, sl=90,
                       tp_levels=[{"price": 110, "pct": 50, "qty": 0.5}, {"price": 120, "pct": 50, "qty": None}]),
        "b": _position(con, 1, 2, "SOLUSDT", "long", 12.6, 3, 120, T + 100, best_price=125, best_time=T + 150,
                       worst_price=118, worst_time=T + 120, sl=110, trail_pct=2),
        "c": _position(con, 1, 4, "BTCUSDT", "short", 7.5, 0.01, 100_000, T, sl=101_000.04,
                       tp_levels=[{"price": 95_000.03, "pct": 100, "qty": None}]),
        "gone": _position(con, 1, 9, "SOLUSDT", "long", 5, 1, 90, T - 900, status="closed"),
    }
    _row(con, "trades", account_id=1, position_id=ids["b"], number=2, symbol="SOLUSDT", side="long", leverage=12.6,
         qty=1, margin=9.5, entry_price=120, entry_time=T + 100, exit_price=121, exit_time=T + 130, reason="tp",
         pnl=1, fee=0.1, funding=0, best_price=122, worst_price=119, sl=110,
         tp_levels=[{"price": 121, "pct": 25, "qty": 1}])
    ids["limit"] = _order(con, 1, 3, "SOLUSDT", "long", 95.004, True, True,
                          tp_levels=[{"price": 100.003, "pct": 50}, {"price": 105, "pct": 50}], sl=89.996)
    ids["dip"] = _order(con, 1, 5, "XRPUSDT", "long", 2, True, False, lev=5, margin=20)
    ids["breakout"] = _order(con, 1, 6, "XRPUSDT", "long", 3, False, False, lev=4, margin=20, created_at=T + 50)
    ids["breakdown"] = _order(con, 1, 7, "ETHUSDT", "short", 3000, True, False, trail_pct=1.5)
    con.commit()
    before = _totals(con)
    migrate(con, MIGRATIONS, tmp_path / "bak", catalog=catalog())
    return con, ids, before


def _totals(con) -> tuple:
    q = "SELECT SUM(qty), SUM(margin), SUM(fee), SUM(funding), SUM(qty * entry_price) FROM positions WHERE status = 'open'"
    return tuple(round(x, 9) for x in con.execute(q).fetchone()) + (con.execute("SELECT cash FROM accounts").fetchone()[0],)


def test_money_is_unchanged_to_the_cent(db):
    """Göç hiçbir hesabın nakdini, marjinini, açık miktarını ve giriş değerini değiştirmemeli. Açık kâr giriş
    değerinden hesaplandığı için özkaynak da aynı kalır."""
    con, _, before = db
    assert _totals(con) == before


def test_same_side_positions_merge_into_the_oldest(db):
    """Hedge modunda parite ve yön başına tek pozisyon olur. Birleşen pozisyon en eskinin kimliğini alır, giriş
    ağırlıklı ortalanır, en iyi ve en kötü fiyat en lehte ve en aleyhte olandır, likidasyon toplamdan hesaplanır."""
    con, ids, _ = db
    sol = [p for p in Ledger(con).positions(1) if p.symbol == "SOLUSDT"]
    assert len(sol) == 1
    p = sol[0]
    assert (p.id, p.qty, p.entry_price, p.entry_time) == (ids["a"], 4, pytest.approx(115), T)
    assert (p.best_price, p.best_time, p.worst_price, p.worst_time) == (125, T + 150, 95, T + 9)
    assert p.margin == pytest.approx(10 + 3 * 120 / 12.6)
    assert p.liq_price == pytest.approx(rules.liq_price(SOL, Side.LONG, 115, 4, p.margin))
    assert p.fee == pytest.approx(0.2) and p.funding == pytest.approx(0.02)


def test_history_of_merged_positions_moves_to_the_kept_one(db):
    """Birleşen pozisyonun kapanmış parçaları kalan pozisyona bağlanır, işlem geçmişi kaybolmaz. Kapanmış
    pozisyonlar ve TP fiyatları yerinde kalır."""
    con, ids, _ = db
    t = Ledger(con).trades(1)[0]
    assert (t.position_id, t.tp_prices, t.sl) == (ids["a"], (121,), 110)
    assert t.leverage == pytest.approx(120 / 9.5)
    assert con.execute("SELECT status FROM positions WHERE id = ?", (ids["gone"],)).fetchone()[0] == "closed"
    assert con.execute("SELECT COUNT(*) FROM positions WHERE id = ?", (ids["b"],)).fetchone()[0] == 0


def test_merged_parts_keep_their_own_protection_by_qty(db):
    """Birleşen pozisyonda her parçanın TP'si, SL'si ve izi kendi miktarıyla emir olur. Miktarsız kalsa bir
    parçanın SL'si bütün pozisyonu kapatırdı."""
    con, ids, _ = db
    sol = [o for o in Ledger(con).orders(1) if o.symbol == "SOLUSDT" and o.reduce]
    got = {(o.type, o.price, o.trail_pct, o.qty) for o in sol}
    assert got == {(OrderType.TAKE_PROFIT, 110, None, 0.5), (OrderType.TAKE_PROFIT, 120, None, 0.5),
                   (OrderType.STOP, 90, None, 1), (OrderType.STOP, 110, None, 3),
                   (OrderType.TRAILING_STOP, None, 2, 3)}
    trail = next(o for o in sol if o.type is OrderType.TRAILING_STOP)
    assert trail.best == 125 and trail.side is Side.LONG and trail.margin == 0
    rows = con.execute("SELECT position_id FROM orders WHERE reduce = 1 AND symbol = 'SOLUSDT'").fetchall()
    assert {r[0] for r in rows} == {ids["a"]}


def test_lone_position_keeps_protection_for_the_whole(db):
    """Birleşmeyen pozisyonun koruması eskisi gibi kalanın tamamını kapatır."""
    con, _, _ = db
    btc = {(o.type, o.price, o.qty) for o in Ledger(con).orders(1) if o.symbol == "BTCUSDT"}
    assert btc == {(OrderType.TAKE_PROFIT, 95_000, None), (OrderType.STOP, 101_000, None)}


def test_pending_orders_keep_their_number_and_meaning(db):
    """Bekleyen emir numarasını korur, Opus onu numarasıyla bilir. Maker emir limit olur. Taker emir fiyat
    düşerek geldiğinde alışta take-profit, satışta stop, yükselerek geldiğinde tersi olur."""
    con, _, _ = db
    entries = {o.number: o for o in Ledger(con).orders(1) if not o.reduce}
    assert {n: o.type for n, o in entries.items()} == {
        3: OrderType.LIMIT, 5: OrderType.TAKE_PROFIT, 6: OrderType.STOP, 7: OrderType.STOP}
    limit = entries[3]
    assert (limit.price, limit.qty, limit.margin) == (95, 1.05, pytest.approx(1.05 * 95 / 10))
    assert [(x.type, x.price, x.qty) for x in limit.protection] == [
        (OrderType.TAKE_PROFIT, 100, 0.52), (OrderType.TAKE_PROFIT, 105, None), (OrderType.STOP, 90, None)]
    assert [(x.type, x.trail_pct) for x in entries[7].protection] == [(OrderType.TRAILING_STOP, 1.5)]


def test_prices_snap_to_tick(db):
    """Eski kod adıma uymayan fiyat kabul ediyordu. Göçten sonra bekleyen emirlerin ve korumaların bütün fiyatları
    paritenin fiyat adımındadır, canlı borsa onları reddetmez."""
    con, _, _ = db
    cat = catalog()
    for o in Ledger(con).orders(1):
        for price in [o.price] + [x.price for x in o.protection]:
            if price:
                rules.check_ticks(cat.get(o.symbol), price)


def test_protection_orders_take_free_numbers(db):
    """Yeni koruma emirleri bekleyen emirlerin kullanmadığı en küçük numaraları alır, iki emir aynı numarayı
    taşımaz."""
    con, _, _ = db
    numbers = sorted(o.number for o in Ledger(con).orders(1))
    assert numbers == list(range(1, 12))


def test_leverage_moves_to_the_symbol(db):
    """Paritenin kaldıracı birleşenlerden en son açılanınkidir ve tam sayıya yuvarlanır. Pozisyonu olmayan
    paritede en son verilen bekleyen emrinkidir."""
    con, _, _ = db
    assert Ledger(con).leverages(1) == {"SOLUSDT": 13, "BTCUSDT": 8, "XRPUSDT": 4, "ETHUSDT": 10}


def test_database_enforces_one_open_position_per_side(db):
    """Aynı yönde ikinci açık pozisyonu veritabanı da reddeder. Broker'daki bir hata modeli sessizce bozamaz."""
    con, _, _ = db
    with pytest.raises(Exception, match="UNIQUE"):
        con.execute(
            "INSERT INTO positions (account_id, symbol, side, qty, margin, entry_price, entry_time, best_price, "
            "best_time, worst_price, worst_time, liq_price) VALUES (1, 'SOLUSDT', 'long', 1, 1, 1, 1, 1, 1, 1, 1, 1)")
