"""
file: test_collector_api.py
importable: yok

Toplayıcı uçlarının testleri. Uygulama gerçek depoyla ve sahte piyasayla
kurulur, ağa çıkmadan çağrılır. Burada bir hata ya izlenen paritelere
Binance'in tanımadığı bir ad yazar, ya çıkarılan paritenin verisini siler,
ya da indirmeyi yanlış biçimde ya da yarım verir.

description: collector.api için entegrasyon testleri.
"""

import asyncio
import json

import httpx
import pytest

from opus_trade.collector.api import create_collector_app
from opus_trade.collector.store import MIGRATIONS, CollectorStore
from opus_trade.market.models import OpenInterest
from opus_trade.storage.database import connect
from opus_trade.storage.migrate import migrate
from tests.fakes import FakeClock, FakeMarket

NOW = 1_795_000_000 // 300 * 300


class Collector:
    def __init__(self, tmp_path):
        con = connect(tmp_path / "market.db")
        migrate(con, MIGRATIONS, tmp_path / "bak")
        self.store = CollectorStore(con)
        for sym in self.store.symbols():
            self.store.remove_symbol(sym)
        self.store.add_symbol("BTCUSDT", NOW - 600)
        self.market = FakeMarket()
        app = create_collector_app(self.store, tmp_path / "market.db", self.market, FakeClock(NOW))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://collector")


@pytest.fixture
def c(tmp_path):
    return Collector(tmp_path)


def go(coro):
    return asyncio.run(coro)


def test_add_checks_binance_and_normalizes(c):
    """Parite büyük harfe çevrilip Binance'e sorulduktan sonra eklenir. Rapor yeni pariteyi ve eklenme anını gösterir."""
    r = go(c.client.post("/symbols", json={"symbol": " ethusdt "}))
    assert r.status_code == 200 and r.json()["symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert ("premium", "ETHUSDT", None, None) in c.market.calls
    report = go(c.client.get("/summary")).json()
    assert [(s["symbol"], s["added_at"]) for s in report["symbols"]] == [("BTCUSDT", NOW - 600), ("ETHUSDT", NOW)]


@pytest.mark.parametrize("symbol, text", [
    ("NOPEUSDT", "Binance'te doğrulanamadı"),
    ("BTCUSDT", "zaten izleniyor"),
    ("BTC/USDT", "harf ve rakam"),
])
def test_bad_symbol_is_not_written(c, symbol, text):
    """Binance'in tanımadığı, zaten izlenen ya da biçimi bozuk parite yazılmaz, Türkçe 400 döner."""
    c.market.fail.add("NOPEUSDT")
    r = go(c.client.post("/symbols", json={"symbol": symbol}))
    assert r.status_code == 400 and text in r.json()["detail"]
    assert c.store.symbols() == ["BTCUSDT"]


def test_remove_keeps_data(c):
    """Çıkarılan paritenin toplanmış verisi silinmez, raporda izlenmeyen olarak kalır. İzlenmeyeni çıkarmak 404'tür."""
    c.store.add_open_interest([OpenInterest(NOW, "BTCUSDT", 1.0, 2.0)])
    r = go(c.client.post("/symbols/remove", json={"symbol": "btcusdt"}))
    assert r.status_code == 200 and r.json()["symbols"] == []
    [sym] = go(c.client.get("/summary")).json()["symbols"]
    assert sym["symbol"] == "BTCUSDT" and sym["tracked"] is False
    assert go(c.client.post("/symbols/remove", json={"symbol": "BTCUSDT"})).status_code == 404


def test_export_streams_file_with_name(c):
    """İndirme dosya adıyla gelir ve seçilen paritenin bütün satırlarını taşır. Bozuk istek indirme başlamadan 400 alır."""
    c.store.add_open_interest([OpenInterest(t, s, 1.0, 2.0) for t in range(NOW, NOW + 3000, 300)
                               for s in ("BTCUSDT", "ETHUSDT")])
    r = go(c.client.get("/export", params={"table": "open_interest", "format": "json", "symbol": "ethusdt"}))
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    assert 'filename="open_interest-ETHUSDT.json"' in r.headers["content-disposition"]
    data = json.loads(r.text)
    assert len(data) == 10 and {d["symbol"] for d in data} == {"ETHUSDT"}
    r = go(c.client.get("/export", params={"table": "coverage"}))
    assert r.status_code == 400 and "Bilinmeyen tablo" in r.json()["detail"]


def test_health_answers_with_symbol_count(c):
    """Sistem sayfası toplayıcının soketine ulaşılıp ulaşılamadığını bu uçtan anlar."""
    r = go(c.client.get("/health"))
    assert r.status_code == 200 and r.json() == {"ok": True, "time": NOW, "symbols": 1}
