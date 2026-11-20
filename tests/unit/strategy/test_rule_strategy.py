"""
file: test_rule_strategy.py
importable: yok

Kural stratejisinin, likidasyon dalgası tetikleyicisinin ve kayıt defterinin
testleri. Kural stratejisi Opus'a sorulmadan para riske eder, kuraldaki bir
sapma doğrudan yanlış işlem demektir.

description: Likidasyon kural stratejisi, likidasyon tetikleyicisi ve registry için birim testleri.
"""

import asyncio

import pytest

from opus_trade import plugins
from opus_trade.domain.models import Side, TpLevel
from opus_trade.domain.trading import AccountState, ClosePosition, OpenPosition, Position
from opus_trade.market.models import Candle, Liquidation
from opus_trade.registry import validate
from opus_trade.strategy.base import STRATEGIES, Context, Wake
from opus_trade.strategy.rules.liquidation_reversal import LiquidationReversal
from opus_trade.triggers.base import TRIGGERS, Fire
from opus_trade.triggers.liquidation_wave import LiquidationWave, Wave

NOW = 1_795_000_020
SOL, ETH = "SOLUSDT", "ETHUSDT"


def rule() -> LiquidationReversal:
    return LiquidationReversal(notional=75, leverage=20, tp_range=0.5, min_tp_pct=0.3, sl_range=0.5, hold_min=60)


def ctx(wakes, positions=(), cash=10.0, price=100.0) -> Context:
    return Context(now=NOW, wakes=wakes, state=AccountState(1, cash, cash, tuple(positions), ()),
                   prices={SOL: price, ETH: 2000.0}, symbols=(SOL, ETH), market=None)


def wave_wake(symbol=SOL, low=96.0, high=100.0, long_usd=500_000.0, short_usd=1_000.0) -> Wake:
    w = Wave(symbol, NOW - 200, NOW - 130, long_usd, short_usd, low, high)
    return Wake("trigger", NOW, Fire("liquidation_wave", NOW, symbol, w))


def position(symbol=SOL, entry_time=NOW - 600) -> Position:
    return Position(1, 1, 1, symbol, Side.LONG, 20, 0.75, 3.75, 100, entry_time, (), None, None, 100, entry_time, 100,
                    entry_time, 95, 0.03, 0, None)


# --- Kural stratejisi ---

def test_long_wave_opens_long_with_tp_and_sl_from_the_range():
    """Long'ların likide olduğu dalga sonrası long açılır. TP yarım aralık ileride, SL dibin yarım aralık altında."""
    d = asyncio.run(rule().decide(ctx([wave_wake()])))
    assert d.commands == [OpenPosition(SOL, Side.LONG, 20, 3.75, (TpLevel(102.0, 100),), 94.0)]
    assert d.wait_s == 3600


def test_wave_is_skipped_when_tp_is_too_close():
    """TP yüzde 0,3'ten yakınsa komisyon kârı yer, kural bu dalgayı atlar."""
    d = asyncio.run(rule().decide(ctx([wave_wake(low=99.6, high=100.0)])))
    assert d.commands == [] and "TP uzaklığı" in d.record["waves"][0]["result"]


def test_one_position_per_symbol():
    """Paritede açık pozisyon varken gelen dalga atlanır, aynı paritede pozisyonlar üst üste binmez."""
    d = asyncio.run(rule().decide(ctx([wave_wake()], positions=[position()])))
    assert d.commands == [] and d.record["waves"][0]["result"] == "paritede açık pozisyon var"


def test_position_is_closed_when_hold_time_is_over():
    """Süresi dolan pozisyon kapanır. Bu kural olmadan geri dönüş gelmeyen pozisyon süresiz açık kalırdı."""
    d = asyncio.run(rule().decide(ctx([Wake("timer", NOW)], positions=[position(entry_time=NOW - 3600)])))
    assert isinstance(d.commands[0], ClosePosition) and d.commands[0].ref.number == 1
    assert d.wait_s is None


# --- Likidasyon dalgası tetikleyicisi ---

def liq(t, symbol=SOL, side=Side.LONG, price=100.0, qty=5000.0) -> Liquidation:
    return Liquidation(t, symbol, side, price, qty)


def test_wave_fires_after_quiet_time_with_range_from_candles():
    """Aralık likidasyon fiyatlarından değil mumlardan ölçülür. 18.11 testinde fark kuralın kârını belirledi."""
    trig = LiquidationWave((SOL,), {SOL: 300_000}, quiet_s=120)
    trig.on_liquidation(liq(NOW))
    trig.on_candle(Candle(SOL, NOW // 60 * 60, 100, 101, 97, 98, 1, 1, 1, 1))
    assert trig.tick(NOW + 120) == []
    fire = trig.tick(NOW + 121)[0]
    assert (fire.data.low, fire.data.high) == (97, 101) and fire.data.dominant is Side.LONG


def test_cooldown_is_per_symbol_and_same_second_waves_fire_together():
    """Bir paritenin bildirimi başka bir paritenin aynı anda biten dalgasını atmamalı."""
    trig = LiquidationWave((SOL, ETH), {SOL: 300_000, ETH: 300_000}, quiet_s=120, cooldown_s=60)
    trig.on_liquidation(liq(NOW, SOL))
    trig.on_liquidation(liq(NOW, ETH))
    assert {f.symbol for f in trig.tick(NOW + 121)} == {SOL, ETH}


def test_wave_below_threshold_does_not_fire():
    """Eşiğin altındaki dalga stratejiyi uyandırmaz, uyandırırsa her küçük dalga bir Opus çağrısı eder."""
    trig = LiquidationWave((SOL,), {SOL: 1_000_000})
    trig.on_liquidation(liq(NOW))
    assert trig.tick(NOW + 200) == [] and trig.snapshot()["symbols"][SOL]["last"]["outcome"] == "below"


# --- Kayıt defteri ---

def test_registered_types_validate_their_params():
    """Panelden gelen ayar şemaya göre denetlenir, aralık dışı bir değer profili sessizce bozamaz."""
    plugins.load()
    entry = STRATEGIES.get("liquidation_reversal")
    assert validate(entry.params, {})["hold_min"] == 60
    with pytest.raises(ValueError, match="Kaldıraç 1 ile 125 arasında"):
        validate(entry.params, {"leverage": 500})
    with pytest.raises(ValueError, match="Bilinmeyen parametre"):
        validate(entry.params, {"lever": 5})
    with pytest.raises(ValueError, match="Parite başına eşik, dolar verilmeli"):
        validate(TRIGGERS.get("liquidation_wave").params, {})
    assert {"llm", "liquidation_reversal"} <= set(STRATEGIES.entries)
