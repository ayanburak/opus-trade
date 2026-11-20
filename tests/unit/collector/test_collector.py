"""
file: test_collector.py
importable: yok

Toplayıcının testleri. Toplanan veri yalnızca canlıyken alınabildiği için
kaybolan ya da ikilenen her kayıt geri getirilemez. Testler geriye dönük
doldurmanın sınırını ve devamını, kapanışta tamponun yazılmasını, kesintilerin
kayda geçmesini, eski verinin aktarımını ve düşen bir kaynağın yeniden
başlamasını korur.

description: collector paketi için birim testleri.
"""

import asyncio
import sqlite3

import pytest

from opus_trade.collector import service
from opus_trade.collector.sources import HISTORY_S, LiquidationSource, PremiumSource, StatsSource
from opus_trade.collector.store import MIGRATIONS, CollectorStore
from opus_trade.domain.models import Side
from opus_trade.market.models import Liquidation, Premium
from opus_trade.market.stream import MarketStream
from opus_trade.storage.database import connect
from opus_trade.storage.migrate import migrate
from tests.fakes import FakeClock, FakeMarket

NOW = 1_795_000_000 // 300 * 300


@pytest.fixture
def store(tmp_path) -> CollectorStore:
    con = connect(tmp_path / "market.db")
    migrate(con, MIGRATIONS, tmp_path / "bak")
    s = CollectorStore(con)
    for sym in s.symbols():
        s.remove_symbol(sym)
    s.add_symbol("BTCUSDT", NOW)
    return s


def count(store: CollectorStore, table: str) -> int:
    return store.con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# --- İstatistikler ---

def test_stats_backfill_starts_at_binance_limit_and_then_resumes(store):
    """İlk tur Binance'in verdiği en eski andan başlar. Daha eskisi 400 alır ve hiçbir şey dolmazdı.
    Sonraki tur yalnızca eksik kısmı ister ve hiçbir kayıt ikilenmez."""
    market, clock = FakeMarket(), FakeClock(NOW)
    market.oldest = NOW - 30 * 86400
    src = StatsSource(market, store, clock)
    asyncio.run(src.collect())
    assert {c[2] for c in market.calls} == {NOW - HISTORY_S}
    first = count(store, "open_interest")
    assert first == HISTORY_S // 300

    market.calls.clear()
    clock.t = NOW + 600
    asyncio.run(src.collect())
    assert {c[2] for c in market.calls} == {NOW}
    assert count(store, "open_interest") == first + 2
    assert count(store, "long_short") == 3 * (first + 2)


def test_stats_failure_of_one_symbol_does_not_stop_the_others(store):
    """Altın gibi bir paritede Binance hata verirse diğer paritelerin verisi yine toplanmalı."""
    store.add_symbol("XAUUSDT", NOW)
    market = FakeMarket()
    market.fail.add("XAUUSDT")
    asyncio.run(StatsSource(market, store, FakeClock(NOW)).collect())
    assert store.last_time("taker_volume", "BTCUSDT") == NOW - 300
    assert store.last_time("taker_volume", "XAUUSDT") is None


# --- Premium ---

def test_premium_keeps_only_tracked_symbols(store):
    """Bütün piyasa dakikada dokuz yüzden fazla satır eder, yalnızca izlenen pariteler yazılmalı."""
    market = FakeMarket()
    market.premiums = [Premium(NOW, s, 100, 99, 0.0001, NOW + 3600) for s in ("BTCUSDT", "PEPEUSDT")]
    asyncio.run(PremiumSource(market, store, FakeClock(NOW)).collect())
    assert [r[0] for r in store.con.execute("SELECT symbol FROM premium")] == ["BTCUSDT"]


# --- Likidasyonlar ---

# Akış bağlanınca iki likidasyon gönderir ve iptal edilene kadar bekler.
class OneShotStream(MarketStream):
    def __init__(self):
        self.sent = asyncio.Event()

    async def watch(self, symbols):
        pass

    async def run(self, listener):
        await listener.on_connect()
        for i in range(2):
            await listener.on_liquidation(Liquidation(NOW + i, "ETHUSDT", Side.LONG, 2000.0, 1.0 + i))
        self.sent.set()
        await asyncio.Event().wait()


def test_buffered_liquidations_are_written_when_service_stops(store):
    """Servis tampon diske yazılmadan durdurulursa son saniyelerin likidasyonları kalıcı olarak kaybolur."""
    async def main():
        stream = OneShotStream()
        task = asyncio.create_task(LiquidationSource(stream, store, FakeClock(NOW), flush_s=3600).run())
        await stream.sent.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(main())
    assert count(store, "liquidations") == 2
    assert store.coverage("liquidations") == [(NOW, NOW)]


def test_coverage_shows_a_gap_after_silence(store):
    """Kesinti kayıtta boşluk olarak görünmeli, yoksa analizde o aralık sıfır likidasyon sanılır."""
    for t in (NOW, NOW + 30, NOW + 200, NOW + 230):
        store.touch("liquidations", t, max_gap=60)
    assert store.coverage("liquidations") == [(NOW, NOW + 30), (NOW + 200, NOW + 230)]


def test_legacy_import_does_not_duplicate_collected_rows(store, tmp_path):
    """Eski sistem ve toplayıcı aynı akışı bir süre birlikte dinler. Aktarım ortak kayıtları ikilememeli."""
    old = sqlite3.connect(tmp_path / "old.db")
    old.execute("CREATE TABLE liquidations (time INTEGER, symbol TEXT, side TEXT, price REAL, qty REAL, usd REAL)")
    old.executemany("INSERT INTO liquidations VALUES (?, ?, ?, ?, ?, ?)",
                    [(NOW - 60, "BTCUSDT", "short", 100.0, 1.0, 100.0), (NOW, "BTCUSDT", "long", 99.0, 2.0, 198.0)])
    old.commit()
    old.close()
    store.add_liquidations([Liquidation(NOW, "BTCUSDT", Side.LONG, 99.0, 2.0)])
    assert service.import_legacy_liquidations(store, tmp_path / "old.db") == 1
    assert count(store, "liquidations") == 2


# --- Servis ---

class FlakySource:
    name = "flaky"

    def __init__(self):
        self.runs = 0

    async def run(self):
        self.runs += 1
        if self.runs == 1:
            raise RuntimeError("ilk turda hata")
        await asyncio.Event().wait()


def test_crashed_source_is_restarted(monkeypatch):
    """Kendi kodundaki bir hatayla düşen kaynak sessizce durursa o verinin toplanması fark edilmeden biter."""
    monkeypatch.setattr(service, "_RESTART_S", 0)

    async def main():
        src, stop = FlakySource(), asyncio.Event()
        runner = asyncio.create_task(service.run_sources([src], stop))
        for _ in range(100):
            if src.runs >= 2:
                break
            await asyncio.sleep(0.01)
        stop.set()
        await runner
        return src.runs

    assert asyncio.run(main()) == 2
