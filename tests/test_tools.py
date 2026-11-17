"""
file: test_tools.py
importable: yok

Opus'un piyasa araçlarının testleri. Araç listesi MCP sunucusunun kendi
list_tools çıktısından okunur, yani Opus'un gördüğü listenin aynısıdır. Her
araç sahte Binance cevaplarıyla çalıştırılır, geçmiş araçları geçici bir
profil veritabanından okur. Proje klasöründe uv run pytest ile çalışır.

description: Araç havuzundaki her aracın belgelendiğini, çalıştığını ve sınırlarını koruduğunu sınayan testler.
"""

# NOTE: SAMPLES her aracın deneme argümanlarını tutar. Havuza yeni bir araç
# eklenince test_every_tool_has_a_sample düşer ve buraya bir satır eklenmesini
# ister. Böylece hiçbir araç çalıştırılmadan havuza giremez.

import asyncio
import json
import time

import pytest

from opus_trade.db import connect
from opus_trade.engine.account import Account
from opus_trade.market import data as market_data
from opus_trade.market import liquidations
from opus_trade.tools import market

from conftest import NOW

SAMPLES = {
    "mumlar": {"sembol": "btcusdt", "aralik": "1h", "adet": 5},
    "gostergeler_hesapla": {"sembol": "BTCUSDT", "aralik": "4h"},
    "emir_defteri": {"sembol": "BTCUSDT", "derinlik": 7},
    "fonlama_gecmisi": {"sembol": "BTCUSDT", "adet": 3},
    "acik_pozisyon_gecmisi": {"sembol": "BTCUSDT", "periyot": "1h", "adet": 3},
    "long_short_oranlari": {"sembol": "BTCUSDT", "periyot": "1h", "adet": 3},
    "gosterge": {"sembol": "BTCUSDT", "ad": "rsi", "aralik": "1h", "adet": 3},
    "seviyeler": {"sembol": "BTCUSDT", "aralik": "1h", "adet": 300},
    "hacim_profili": {"sembol": "BTCUSDT", "aralik": "1h", "adet": 48, "kademe": 10},
    "korelasyon": {"semboller": ["BTCUSDT", "ETHUSDT"], "aralik": "1h", "adet": 48},
    "likidasyonlar": {"sembol": "BTCUSDT", "saat": 24},
    "gecmis_kararlarim": {"adet": 5},
    "gecmis_islemlerim": {"adet": 5},
    "cikis_karnesi": {"adet": 5},
    "ny_acilisi": {"sembol": "zecusdt"},
    "acilis_gecmisi": {"sembol": "ZECUSDT", "gun": 2},
}


def _tools() -> list:
    return asyncio.run(market.server.list_tools())


def _run(name: str, **args) -> str:
    result = getattr(market, name)(**args)
    return asyncio.run(result) if asyncio.iscoroutine(result) else result


# Geçmiş araçları profilin veritabanını TRADE_DB, likidasyonları ise
# TRADE_MARKET_DB yolundan okur. Gerçek programda bu yolları agent.py verir.
@pytest.fixture
def history(tmp_path, monkeypatch, account):
    p = account.open_position("BTCUSDT", "long", 10, 50, 50000, NOW - 7200, tp=51000)
    account.check_price("BTCUSDT", 51100, 50500, NOW - 3600)
    account.con.execute(
        "INSERT INTO journal (time, trigger, decision, action_results) VALUES (?, ?, ?, ?)",
        (NOW - 7200, "timer", json.dumps({"gorus": "yukarı", "guven": 6, "ozet": "Long açıldı.", "analiz": "Uzun analiz."}),
         json.dumps([{"message": f"#{p['id']} pozisyonu açıldı."}])),
    )
    account.con.commit()
    db = account.con.execute("PRAGMA database_list").fetchone()["file"]
    monkeypatch.setenv("TRADE_DB", db)

    con = liquidations.connect(tmp_path / "market.db")
    for i, side in enumerate(("long", "long", "short")):
        liquidations.record(con, {"time": int(time.time()) - 600 + i * 30, "symbol": "BTCUSDT",
                                  "side": side, "price": 50000 - i * 10, "qty": 0.1, "usd": 5000.0 * (i + 1)})
    con.close()
    monkeypatch.setenv("TRADE_MARKET_DB", str(tmp_path / "market.db"))


# --- Belgeleme ---

def test_every_tool_has_a_sample():
    assert sorted(t.name for t in _tools()) == sorted(SAMPLES)


# Opus bir parametrenin ne işe yaradığını yalnızca açıklamadan öğrenir.
# Açıklamada adı geçmeyen parametre Opus için belgelenmemiş demektir.
@pytest.mark.parametrize("tool", _tools(), ids=lambda t: t.name)
def test_every_parameter_is_documented(tool):
    assert tool.description and len(tool.description) > 40
    for name in tool.input_schema.get("properties", {}):
        assert f"{name}:" in tool.description, f"{tool.name} aracında {name} açıklanmamış"


# --- Çalışma ---

@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_tool_runs_on_fixed_market(name, market, history):
    out = _run(name, **SAMPLES[name])
    assert isinstance(out, str) and out.strip()
    assert "Geçersiz" not in out and "verisi yok" not in out and "bulunamadı" not in out


def test_symbol_is_uppercased_and_limits_clamped(market):
    _run("mumlar", sembol="btcusdt", aralik="1h", adet=99999)
    assert market[-1] == ("/fapi/v1/klines", {"symbol": "BTCUSDT", "interval": "1h", "limit": 1500})
    _run("emir_defteri", sembol="BTCUSDT", derinlik=7)
    assert market[-1][1]["limit"] == 5


@pytest.mark.parametrize("name, args, fragment", [
    ("mumlar", {"sembol": "BTCUSDT", "aralik": "2m"}, "Geçersiz aralık"),
    ("acik_pozisyon_gecmisi", {"sembol": "BTCUSDT", "periyot": "1m"}, "Geçersiz periyot"),
    ("korelasyon", {"semboller": ["BTCUSDT"]}, "İki ile on arası"),
])
def test_invalid_input_returns_message(market, name, args, fragment):
    assert fragment in _run(name, **args)


# --- Geçmiş ---

def test_history_tools_read_profile_db(market, history):
    trades = _run("gecmis_islemlerim")
    assert "#1 BTCUSDT long" in trades and "neden tp" in trades
    decisions = _run("gecmis_kararlarim", analiz_dahil=True)
    assert "görüş yukarı, güven 6" in decisions and "analiz: Uzun analiz." in decisions
    assert "analiz:" not in _run("gecmis_kararlarim")
    liq = _run("likidasyonlar", sembol="btcusdt")
    assert "long likidasyonları 2 kayıt, 15.0 bin dolar" in liq


# 50000'den on kat elli dolarlık long, TP 50800, SL 49500. Fiyat önce 50700'e
# çıkıp TP'ye yüzde 0.20 kala döner, sonra SL'den kapanır. Giriş 50010,
# miktar 0.009, en iyi anda açık kâr 690 * 0.009 = 6.21 dolar. Çıkış 49500'ün
# kaymalısı 49490.1, net -4.6791 eksi iki komisyon 0.447750 = -5.1269 dolar,
# yakalama yüzde -83.
def test_exit_report_measures_missed_tp_and_capture(market, history, account):
    account.open_position("BTCUSDT", "long", 10, 50, 50000, NOW - 1800, tp=50800, sl=49500)
    account.check_price("BTCUSDT", 50700, 50000, NOW - 1200)
    account.check_price("BTCUSDT", 50100, 49400, NOW - 600)
    out = _run("cikis_karnesi")
    line = out.splitlines()[0]
    assert line.startswith("#2 BTCUSDT long") and "neden sl, süre 0 sa 20 dk" in line and "net -5.13 dolar" in line
    assert "açık kâr +6.21 dolar, yakalama yüzde -83" in line
    assert "Değmeyen en yakın TP 50800, fiyat yüzde 0.20 kala döndü" in line
    assert "SL'ye yüzde 0.00 kaldı" in line
    # Fikstürdeki ilk işlem TP'yle tamamen kapanmıştı, değmeyen TP'si yok.
    assert "Değmeyen" not in out.splitlines()[1]
    assert "varamayan işlem 1." in out.splitlines()[-1]


# Geçmiş araçları profilin veritabanını salt okunur açar. Opus'un bir aracı
# hesabı değiştiremez.
def test_history_tools_open_db_read_only(history):
    with pytest.raises(Exception, match="readonly"):
        market._db().execute("DELETE FROM trades")


# --- NY açılışı ---

# Gerçek Binance içinde bulunulan dakikanın mumunu dakikanın ilk saniyelerinde
# çoğu zaman vermiyor. Bu sahte cevap o durumu her zaman üretir. Eski kod
# fiyatı tam bu mumdan okuduğu için boş listede düşüyordu.
def test_ny_opening_reads_last_closed_minute(market, monkeypatch):
    fake = market_data.request

    async def request(path, **params):
        start = params.get("startTime")
        if start and params.get("limit") == 1 and -(-start // 60000) * 60 >= int(time.time()) // 60 * 60:
            return []
        return await fake(path, **params)

    monkeypatch.setattr(market_data, "request", request)
    out = _run("ny_acilisi", sembol="ZECUSDT")
    assert "Nasdaq vadelisi, QQQUSDT: son 6 saatte yüzde" in out
    assert "alınamadı" not in out


def test_ny_opening_survives_missing_prices(market, monkeypatch):
    fake = market_data.request

    async def request(path, **params):
        if params.get("limit") == 1:
            return []
        return await fake(path, **params)

    monkeypatch.setattr(market_data, "request", request)
    out = _run("ny_acilisi", sembol="ZECUSDT")
    assert "son 6 saatte alınamadı, önceki NY kapanışından bu yana alınamadı" in out
    assert "Beklenen av" in out
