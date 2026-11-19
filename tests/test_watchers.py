"""
file: test_watchers.py
importable: yok

Gözcülerin testleri. Likidasyon dalgası gözcüsünün dalgayı ne zaman bitmiş
saydığı, eşiğin altındaki dalgayı atması, bekleme süresi ve katalogdan
kurulumun hataları denenir. Arayüzdeki sayacın durumu ve bu durumun yalnızca
değişince yayınlanması da denenir. Son testler bir Engine'in gözcü mesajıyla
Opus'u çağırmaya kalktığını ve gözcüsü olmayan profilin hiçbir şey yapmadığını sınar.

description: Gözcülerin ve Engine'e bağlanmalarının testleri.
"""

import pytest

from opus_trade import settings
from opus_trade.engine import scheduler, watchers

T0 = 1794916800


def liq(t: int, side: str, usd: float, price: float = 64000.0, symbol: str = "BTCUSDT") -> dict:
    return {"time": t, "symbol": symbol, "side": side, "price": price, "qty": usd / price, "usd": usd}


# --- Likidasyon dalgası ---

def test_wave_fires_after_quiet_period():
    w = watchers.LiquidationWave(["BTCUSDT"], min_usd=300_000, quiet_s=120)
    assert w.on_liquidation(liq(T0, "long", 200_000, 64200)) is None
    assert w.on_liquidation(liq(T0 + 60, "long", 150_000, 64000)) is None
    assert w.on_liquidation(liq(T0 + 70, "short", 20_000, 64050)) is None
    assert w.tick(T0 + 70 + 120) is None
    msg = w.tick(T0 + 70 + 121)
    assert msg and "350 bin dolarlık long ve 20 bin dolarlık short" in msg
    assert "en düşük 64000.0, en yüksek 64200.0" in msg


# Aralık dalganın başladığı dakikanın mumundan itibaren ölçülür. Önceki
# dakikanın mumu ve başka paritenin mumu aralığa girmez.
def test_wave_range_uses_candles():
    w = watchers.LiquidationWave(["BTCUSDT"], min_usd=100_000, quiet_s=60)
    m0 = T0 // 60 * 60
    w.on_candle({"s": "BTCUSDT", "t": m0 - 60, "o": 0, "h": 65000.0, "l": 63000.0, "c": 64000.0})
    w.on_candle({"s": "BTCUSDT", "t": m0, "o": 0, "h": 64300.0, "l": 64100.0, "c": 64200.0})
    w.on_liquidation(liq(m0 + 10, "long", 200_000, 64150.0))
    w.on_candle({"s": "BTCUSDT", "t": m0 + 60, "o": 0, "h": 64250.0, "l": 63800.0, "c": 63900.0})
    w.on_candle({"s": "ETHUSDT", "t": m0 + 60, "o": 0, "h": 9999.0, "l": 1.0, "c": 3000.0})
    msg = w.tick(m0 + 10 + 61)
    assert "en düşük 63800.0, en yüksek 64300.0" in msg


def test_small_wave_is_dropped():
    w = watchers.LiquidationWave(["BTCUSDT"], min_usd=300_000, quiet_s=120)
    w.on_liquidation(liq(T0, "short", 100_000))
    assert w.tick(T0 + 500) is None
    assert w.tick(T0 + 1000) is None


# Yeni dalganın ilk kaydı geldiğinde tick henüz çalışmamış olabilir. Eski
# dalga o anda kapanır, yeni kayıt ona karışmaz.
def test_new_liquidation_closes_old_wave():
    w = watchers.LiquidationWave(["BTCUSDT"], min_usd=300_000, quiet_s=120)
    w.on_liquidation(liq(T0, "long", 400_000))
    msg = w.on_liquidation(liq(T0 + 300, "short", 50_000))
    assert msg and "400 bin dolarlık long" in msg
    assert w.tick(T0 + 1000) is None


def test_cooldown_drops_second_wave():
    w = watchers.LiquidationWave(["BTCUSDT"], min_usd=100_000, quiet_s=60, cooldown_s=600)
    w.on_liquidation(liq(T0, "long", 200_000))
    assert w.tick(T0 + 61)
    w.on_liquidation(liq(T0 + 100, "long", 200_000))
    assert w.tick(T0 + 161) is None
    w.on_liquidation(liq(T0 + 700, "long", 200_000))
    assert w.tick(T0 + 761)


def test_other_symbols_are_ignored():
    w = watchers.LiquidationWave(["BTCUSDT"], min_usd=100_000, quiet_s=60)
    w.on_liquidation(liq(T0, "long", 500_000, symbol="ETHUSDT"))
    assert w.tick(T0 + 100) is None


# Her paritenin kendi eşiği ve kendi bekleme süresi vardır. Aynı saniyede
# biten dalgaların hepsi tek mesajda bildirilir.
def test_per_symbol_threshold_and_cooldown():
    w = watchers.LiquidationWave(["BTCUSDT", "SOLUSDT"], min_usd={"BTCUSDT": 600_000, "SOLUSDT": 80_000},
                                 quiet_s=60, cooldown_s=600)
    w.on_liquidation(liq(T0, "long", 500_000))
    assert w.tick(T0 + 61) is None
    w.on_liquidation(liq(T0 + 100, "long", 900_000))
    w.on_liquidation(liq(T0 + 100, "short", 200_000, price=73.0, symbol="SOLUSDT"))
    msg = w.tick(T0 + 161)
    assert msg.startswith("BTC/USDT") and "SOL/USDT" in msg
    w.on_liquidation(liq(T0 + 200, "long", 900_000))
    assert w.tick(T0 + 261) is None
    w.on_liquidation(liq(T0 + 300, "long", 100_000, price=73.0, symbol="SOLUSDT"))
    assert w.tick(T0 + 361) is None
    w.on_liquidation(liq(T0 + 800, "long", 100_000, price=73.0, symbol="SOLUSDT"))
    assert w.tick(T0 + 861).startswith("SOL/USDT")


# Sayaç süren dalgayı, biten dalganın sonucunu ve bekleme süresine denk gelen
# dalgayı ayrı gösterir. Yeni dalga başlasa da biten son dalga kalır.
def test_snapshot_tracks_wave_and_outcome():
    w = watchers.LiquidationWave(["BTCUSDT"], min_usd=300_000, quiet_s=60, cooldown_s=600)
    snap = w.snapshot()
    assert snap["quiet_s"] == 60
    assert snap["symbols"]["BTCUSDT"] == {"min_usd": 300_000, "wave": None, "last": None}
    w.on_liquidation(liq(T0, "long", 200_000))
    w.on_liquidation(liq(T0 + 10, "short", 30_000))
    assert w.snapshot()["symbols"]["BTCUSDT"]["wave"] == {"start": T0, "end": T0 + 10, "long": 200_000, "short": 30_000}
    w.tick(T0 + 71)
    btc = w.snapshot()["symbols"]["BTCUSDT"]
    assert btc["wave"] is None and btc["last"]["outcome"] == "below"
    w.on_liquidation(liq(T0 + 100, "long", 400_000))
    assert w.tick(T0 + 161)
    assert w.snapshot()["symbols"]["BTCUSDT"]["last"]["outcome"] == "fired"
    w.on_liquidation(liq(T0 + 200, "long", 400_000))
    assert w.snapshot()["symbols"]["BTCUSDT"]["last"]["outcome"] == "fired"
    assert w.tick(T0 + 261) is None
    assert w.snapshot()["symbols"]["BTCUSDT"]["last"]["outcome"] == "cooldown"


# --- Katalog ---

def test_large_amounts_are_written_in_millions():
    w = watchers.LiquidationWave(["BTCUSDT"], min_usd=300_000, quiet_s=60)
    w.on_liquidation(liq(T0, "short", 1_250_000))
    assert "1,25 milyon dolarlık short" in w.tick(T0 + 61)


def test_build_from_spec():
    w = watchers.build({"type": "liquidation_wave", "min_usd": 1e6}, ["BTCUSDT"])
    assert isinstance(w, watchers.LiquidationWave) and w.min_usd == 1e6


@pytest.mark.parametrize("spec", [{"type": "yok"}, {"min_usd": 1}, {"type": "liquidation_wave", "esik": 1},
                                  {"type": "liquidation_wave", "min_usd": {"ETHUSDT": 1}}])
def test_build_rejects_bad_spec(spec):
    with pytest.raises(ValueError):
        watchers.build(spec, ["BTCUSDT"])


# --- Engine bağlantısı ---

def _engine(tmp_path, monkeypatch, specs: tuple) -> scheduler.Engine:
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    profile = settings.Profile(
        id="test", name="Test Opus", short="Test", balance=10.0, symbols={"BTCUSDT": settings.SYMBOLS["BTCUSDT"]},
        period_s=4 * 3600, min_wait=3, max_wait=8, style="", watchers=specs)
    return scheduler.Engine(profile, None, lambda: None)


def test_engine_triggers_on_wave(tmp_path, monkeypatch):
    e = _engine(tmp_path, monkeypatch, ({"type": "liquidation_wave", "min_usd": 100_000, "quiet_s": 60},))
    calls = []
    monkeypatch.setattr(e, "trigger", lambda kind, msg: calls.append((kind, msg)))
    e.on_liquidation(liq(T0, "long", 200_000))
    e._watched(e._watchers[0], e._watchers[0].tick(T0 + 61))
    assert len(calls) == 1 and calls[0][0] == "liquidation_wave"


# Gözcünün durumu hesap durumuna girer ve yalnızca değişince yayınlanır.
def test_engine_broadcasts_watch_on_change(tmp_path, monkeypatch):
    e = _engine(tmp_path, monkeypatch, ({"type": "liquidation_wave", "min_usd": 100_000, "quiet_s": 60},))
    q = e.listen()
    e.on_liquidation(liq(T0, "long", 50_000))
    e.on_liquidation(liq(T0, "long", 50_000, symbol="ETHUSDT"))
    sent = [q.get_nowait() for _ in range(q.qsize())]
    assert [m["kind"] for m in sent] == ["watch"]
    assert sent[0]["data"]["watch"]["liquidation_wave"]["symbols"]["BTCUSDT"]["wave"]["long"] == 50_000
    assert e._watch_snapshot() == sent[0]["data"]["watch"]


def test_engine_without_watchers_does_nothing(tmp_path, monkeypatch):
    e = _engine(tmp_path, monkeypatch, ())
    monkeypatch.setattr(e, "trigger", lambda *a: pytest.fail("çağrılmamalıydı"))
    e.on_liquidation(liq(T0, "long", 5_000_000))
    assert e._watchers == []


def test_engine_rejects_bad_watcher(tmp_path, monkeypatch):
    with pytest.raises(SystemExit):
        _engine(tmp_path, monkeypatch, ({"type": "yok"},))
