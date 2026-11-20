"""
file: test_report.py
importable: yok

Kapsama raporunun ve dışa aktarımın testleri. Rapor yanlışsa olmayan bir
boşluk görünür ya da gerçek bir boşluk saklanır, hangi aralığın verisine
güvenilebileceği yanlış bilinir. İndirme yanlışsa eksik ya
da başka bir paritenin satırları dosyaya girer.

description: collector.report için birim testleri.
"""

import csv
import io
import json

import pytest

from opus_trade.collector.report import plan_export, render, summary
from opus_trade.collector.store import MIGRATIONS, CollectorStore
from opus_trade.domain.models import Side
from opus_trade.market.models import Liquidation, LongShortRatio, OpenInterest, Premium
from opus_trade.market.source import RatioKind
from opus_trade.storage.database import connect
from opus_trade.storage.migrate import migrate

NOW = 1_795_000_000 // 300 * 300


@pytest.fixture
def store(tmp_path) -> CollectorStore:
    con = connect(tmp_path / "market.db")
    migrate(con, MIGRATIONS, tmp_path / "bak")
    s = CollectorStore(con)
    for sym in s.symbols():
        s.remove_symbol(sym)
    s.add_symbol("BTCUSDT", NOW - 86400)
    return s


def series(report: dict, symbol: str, name: str) -> dict:
    [sym] = [x for x in report["symbols"] if x["symbol"] == symbol]
    [one] = [x for x in sym["series"] if x["series"] == name]
    return one


def test_live_gaps_are_between_coverage_ranges(store):
    """Canlı kaynağın boşluğu bir aralığın sonuyla sonrakinin başı arasıdır. Kesintisiz bir aralık boşluk sayılmaz."""
    for t in (NOW, NOW + 30, NOW + 500, NOW + 530):
        store.touch("liquidations", t, 60)
    store.touch("premium", NOW, 120)
    store.add_liquidations([Liquidation(NOW + 10, "ETHUSDT", Side.SHORT, 100.0, 2.0)])
    r = summary(store.con, NOW + 600)
    liq, prem = r["live"]
    assert liq["source"] == "liquidations" and liq["ranges"] == [[NOW, NOW + 30], [NOW + 500, NOW + 530]]
    assert liq["gaps"] == [[NOW + 30, NOW + 500]]
    assert (liq["rows"], liq["first"], liq["last"]) == (1, NOW + 10, NOW + 10)
    assert prem["gaps"] == [] and prem["rows"] == 0 and r["now"] == NOW + 600


def test_stats_gap_is_found_per_series_and_symbol(store):
    """İstatistikte beş dakikadan uzun ara boşluktur. Bir türün boşluğu diğer türe ya da başka pariteye yazılmaz."""
    store.add_symbol("ETHUSDT", NOW)
    times = [NOW, NOW + 300, NOW + 1500, NOW + 1800]
    store.add_open_interest([OpenInterest(t, "BTCUSDT", 1.0, 2.0) for t in times])
    store.add_open_interest([OpenInterest(t, "ETHUSDT", 1.0, 2.0) for t in range(NOW, NOW + 1801, 300)])
    store.add_long_short(RatioKind.ACCOUNTS,
                         [LongShortRatio(t, "BTCUSDT", 60.0, 1.5) for t in range(NOW, NOW + 1801, 300)])
    r = summary(store.con, NOW + 2000)
    oi = series(r, "BTCUSDT", "open_interest")
    assert oi["gaps"] == [[NOW + 300, NOW + 1500]] and (oi["rows"], oi["first"], oi["last"]) == (4, NOW, NOW + 1800)
    assert series(r, "ETHUSDT", "open_interest")["gaps"] == []
    assert series(r, "BTCUSDT", "long_short:accounts")["gaps"] == []
    assert series(r, "BTCUSDT", "long_short:top_accounts")["rows"] == 0


def test_removed_symbol_with_data_is_still_listed(store):
    """İzlenmeyen ama verisi olan parite listede kalır, eski verisi indirilebilsin. İzlenenler önce gelir."""
    store.add_premiums([Premium(NOW, "AAAUSDT", 1.0, 1.0, 0.0001, NOW + 3600)])
    r = summary(store.con, NOW)
    assert [(s["symbol"], s["tracked"], s["added_at"]) for s in r["symbols"]] == [
        ("BTCUSDT", True, NOW - 86400), ("AAAUSDT", False, None)]
    assert series(r, "AAAUSDT", "premium")["rows"] == 1


def export(store, table, fmt, chunk=2, **kw) -> str:
    sql, args, cols = plan_export(table, fmt, **kw)
    return "".join(render(store.con.execute(sql, args), cols, fmt, chunk=chunk))


def test_export_filters_symbol_kind_and_time_range(store):
    """İndirme yalnızca seçilen paritenin, türün ve aralığın satırlarını, zamana göre sıralı verir. Bitiş hariçtir."""
    for sym in ("BTCUSDT", "ETHUSDT"):
        for kind in (RatioKind.ACCOUNTS, RatioKind.TOP_POSITIONS):
            store.add_long_short(kind, [LongShortRatio(t, sym, 60.0, 1.5) for t in range(NOW, NOW + 1500, 300)])
    rows = list(csv.reader(io.StringIO(export(store, "long_short", "csv", symbol="BTCUSDT", kind="accounts",
                                              start=NOW + 300, end=NOW + 1200))))
    assert rows[0] == ["kind", "symbol", "time", "long_pct", "ratio"]
    assert [r[2] for r in rows[1:]] == [str(NOW + 300), str(NOW + 600), str(NOW + 900)]
    assert {(r[0], r[1]) for r in rows[1:]} == {("accounts", "BTCUSDT")}


@pytest.mark.parametrize("n", [0, 1, 5])
def test_json_export_is_valid_across_chunks(store, n):
    """JSON parçalar halinde yazılsa da, boş olsa da tek ve geçerli bir dizidir. Parça sınırında satır kaybolmaz."""
    store.add_liquidations([Liquidation(NOW + i, "BTCUSDT", Side.LONG, 10.0 + i, 1.0) for i in range(n)])
    data = json.loads(export(store, "liquidations", "json"))
    assert [d["price"] for d in data] == [10.0 + i for i in range(n)]
    assert n == 0 or set(data[0]) == {"time", "symbol", "side", "price", "qty"}


@pytest.mark.parametrize("kw, text", [
    ({"table": "tracked_symbols", "fmt": "csv"}, "Bilinmeyen tablo"),
    ({"table": "premium", "fmt": "xlsx"}, "Bilinmeyen biçim"),
    ({"table": "premium", "fmt": "csv", "kind": "accounts"}, "yalnızca long-short"),
    ({"table": "long_short", "fmt": "csv", "kind": "x; DROP"}, "Bilinmeyen oran türü"),
    ({"table": "premium", "fmt": "csv", "start": 10, "end": 10}, "Bitiş"),
])
def test_bad_export_request_is_refused_before_query(kw, text):
    """Listede olmayan tablo, biçim ya da tür sorguya hiç girmez, Türkçe bir hatayla reddedilir."""
    with pytest.raises(ValueError, match=text):
        plan_export(**kw)
