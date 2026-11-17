"""
file: test_stream.py
importable: yok

Canlı akış koptuğunda kaçan mumların işlenmesini sınar. Engine geçici bir
veri klasörüyle kurulur, Binance'in dakikalık mumları sahte bir
minutes_between ile verilir ve Opus çağrılmaz. Proje klasöründe uv run pytest
ile çalışır.

description: Engine.catch_up'ın kopuş süresindeki mumları doğru aralıkla işlediğini sınayan testler.
"""

# NOTE: Engine bir olay görünce Opus'u çağırmak için trigger'ı kullanır.
# Testte trigger yalnızca gelen olayları kaydeden bir fonksiyonla
# değiştirilir, böylece hiçbir çağrı başlamaz.

import asyncio

import pytest

from opus_trade import settings
from opus_trade.engine import scheduler
from opus_trade.market import data as market_data

from conftest import NOW

BTC = "BTCUSDT"


def _candle(t: int, h: float, l: float, c: float) -> dict:
    return {"t": t, "o": 50000.0, "h": h, "l": l, "c": c, "v": 1.0}


# Kopuştan önce görülen son mum NOW dakikasındadır. Akış iki buçuk dakika
# sonra geri gelir, aradaki mumlar sahte REST cevabından okunur.
@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(scheduler.time, "time", lambda: NOW + 150)
    p = settings.Profile(id="test", name="Test Opus", short="Test", balance=100.0,
                         symbols={BTC: settings.SYMBOLS[BTC]}, period_s=86400, min_wait=5,
                         max_wait=240, style="")
    p.work_dir.mkdir(parents=True)
    e = scheduler.Engine(p, None, lambda: None)
    e.triggered = []
    e.trigger = lambda kind, message: e.triggered.append((kind, message))
    e.account.open_position(BTC, "long", 10, 50, 50000, NOW + 10, sl=49000)
    asyncio.run(e.on_candle({**_candle(NOW, 50100, 49900, 50000), "s": BTC}))
    yield e
    e.con.close()


def _rest(monkeypatch, candles: list[dict]) -> list:
    asked = []

    async def minutes_between(symbol, start, end):
        asked.append((symbol, start, end))
        return [m for m in candles if start <= m["t"] < end]
    monkeypatch.setattr(market_data, "minutes_between", minutes_between)
    return asked


# Kopuştan sonraki dakikada fiyat SL'nin altına iner. Olay mumun sonuna
# tarihlenir ve mesajın başında saati yazar.
def test_missed_minute_hits_sl(engine, monkeypatch):
    asked = _rest(monkeypatch, [_candle(NOW, 50100, 49900, 49950), _candle(NOW + 60, 49950, 48900, 49100),
                                _candle(NOW + 120, 49200, 49000, 49150)])
    asyncio.run(engine.catch_up())
    assert asked == [(BTC, NOW, NOW + 180)]
    assert [k for k, _ in engine.triggered] == ["sl"]
    assert "civarında" in engine.triggered[0][1]
    t = engine.account._trade(1)
    assert t["exit_reason"] == "sl" and t["exit_time"] == NOW + 119
    assert engine.prices[BTC] == 49150


# Son görülen dakikanın kopuştan sonraki kısmında fiyat SL'ye iner. Aynı
# dakikanın tam mumu tekrar gelir ve yalnızca yeni görülen ucu sayılır.
def test_rest_of_last_seen_minute_is_checked(engine, monkeypatch):
    _rest(monkeypatch, [_candle(NOW, 50100, 48900, 49500)])
    asyncio.run(engine.catch_up())
    assert [k for k, _ in engine.triggered] == ["sl"]
    assert engine.account._trade(1)["exit_time"] == NOW + 59


# Kopuştan önce görülmüş aralık ikinci kez sayılmaz. Görülen düşük 49900,
# kopuştan sonra kurulan 49920 alarmı aynı dakikanın tam mumuyla tetiklenmez.
def test_seen_part_is_not_replayed(engine, monkeypatch):
    engine.account.set_alert(BTC, 49920, "", 50000, NOW + 30)
    _rest(monkeypatch, [_candle(NOW, 50100, 49900, 50000)])
    asyncio.run(engine.catch_up())
    assert engine.triggered == []
