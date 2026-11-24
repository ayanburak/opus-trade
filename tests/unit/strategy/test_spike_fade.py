"""
file: test_spike_fade.py
importable: yok

İşlem patlaması tetikleyicisinin ve İğne dönüşü stratejisinin testleri.
Strateji Opus'a sorulmadan, milisaniyeler içinde para riske eder. Eşikteki,
büyüklükteki ya da kaldıraçtaki bir sapma doğrudan yanlış işlem demektir.
Beklenen rakamlar kuraldan elle hesaplandı.

description: triggers.trade_burst ve strategy.rules.spike_fade için birim testleri.
"""

import asyncio

import pytest

from opus_trade import plugins
from opus_trade.domain.models import Side
from opus_trade.domain.trading import (AccountEvent, AccountState, Actor, CancelOrder, ClosePosition, EventKind, Order,
                                       OrderType, PlaceOrder, Position, Protection, Ref, SetLeverage)
from opus_trade.market.flow import TradeFlow
from opus_trade.market.models import DailyStats, Trade
from opus_trade.market.screen import MarketScreen
from opus_trade.market.stream import Feed
from opus_trade.strategy.base import Context, Wake
from opus_trade.strategy.rules.spike_fade import SpikeFade
from opus_trade.triggers.base import Fire, Setup
from opus_trade.triggers.trade_burst import Burst, TradeBurst
from tests.fakes import FakeClock, FakeMarket, catalog

plugins.load()
NOW = 1_795_000_000
SOL, XRP = "SOLUSDT", "XRPUSDT"


# --- Tetikleyici ---

class Market(FakeMarket):
    async def all_daily_stats(self):
        return [DailyStats(SOL, 30, 130, 100, 1e9), DailyStats(XRP, 1, 1.01, 1, 1e9)]


def burst_trigger(**kw) -> tuple[TradeBurst, TradeFlow]:
    flow = TradeFlow()
    screen = MarketScreen(Market(), FakeClock(NOW))
    asyncio.run(screen.refresh())
    t = TradeBurst(Setup((), frozenset({SOL, XRP}), screen, flow), list_size=1, ranking="range", **kw)
    return t, flow


def feed(t: TradeBurst, flow: TradeFlow, trades: list[Trade]) -> list[Fire]:
    out = []
    for x in trades:
        flow.add(x)
        out += t.on_trade(x)
    return out


# 400 saniye boyunca beş saniyede bir 1,00'dan işlem, normal saniyede 0,2
# işlem. Sonra 55 saniye yavaş yükseliş 1,03'e, beş saniyede bir işlem. Son
# 5 saniyede saniyede 20 alış, fiyat 1,04'ten 1,08'e.
def quiet_then_burst(t0: int = NOW) -> list[Trade]:
    out = [Trade(SOL, (t0 + s) * 1000, 1.00, 10, s % 10 == 0) for s in range(0, 400, 5)]
    out += [Trade(SOL, (t0 + s) * 1000, 1.03, 10, True) for s in range(400, 455, 5)]
    out += [Trade(SOL, (t0 + 455) * 1000 + i * 50, round(1.04 + 0.04 * i / 99, 6), 10, True) for i in range(100)]
    return out


def test_list_comes_from_the_screen_with_trades_and_candles():
    """İzleme listesi saat başı özetten kurulur. Liste değişmedikçe istek aynı kalmalı, yoksa her saniye abonelik
    mesajı gider. Sinyal işlemden hesaplanır, mum da istenir: panelin grafiği canlı fiyatı mumdan alır, mum
    dinlenmezse listedeki coin'lerin grafiği donar."""
    t, _ = burst_trigger()
    assert t.wanted() == {SOL: frozenset({Feed.TRADE, Feed.KLINE})}
    assert t.wanted() == t.wanted()


def test_quiet_symbol_bursting_one_way_fires_once():
    """Metindeki tipik patlama: fiyat 5 saniyede 1,04'ten 1,08'e çıkar, hacmin hepsi alıcıdan gelir. Eşiklerin
    hepsi geçildiği ilk işlemde bildirilmeli, tepeyi beklememeli: 1,0646'da 5 saniyelik hareket %3,4, 60 saniyelik
    %6,5, ortalamadan kopma %6. Bekleme süresince tekrar etmemeli."""
    t, flow = burst_trigger()
    t.wanted()
    fires = feed(t, flow, quiet_then_burst())
    assert len(fires) == 1
    f = fires[0]
    b: Burst = f.data
    assert b.up and b.start == 1.00 and f.symbol == SOL
    assert b.tip == pytest.approx(1.064646) and b.move_5s == pytest.approx(1.064646 / 1.03 - 1)
    assert b.move_60s == pytest.approx(0.064646) and b.side_share == pytest.approx(1.0)
    assert b.rate_mult >= 5 and b.mean_gap >= 0.06
    assert f.ms == f.time * 1000 + (f.ms % 1000)


def test_no_signal_before_the_normal_is_known():
    """Liste yeni değişmişse paritenin geçmişi kısadır. Eksik geçmişle ölçülen normal patlamayı uydurur."""
    t, flow = burst_trigger()
    t.wanted()
    trades = [x for x in quiet_then_burst() if x.ms >= (NOW + 300) * 1000]
    assert feed(t, flow, trades) == []


def test_sellers_driving_a_rise_do_not_fire():
    """Hacmin çoğu satıcıdan gelen bir yükseliş tek yönlü patlama değildir."""
    t, flow = burst_trigger()
    t.wanted()
    trades = [Trade(x.symbol, x.ms, x.price, x.qty, x.buy if x.ms < (NOW + 455) * 1000 else False)
              for x in quiet_then_burst()]
    assert feed(t, flow, trades) == []


def test_symbols_outside_the_list_are_ignored():
    """Açık pozisyonu olan ama listede olmayan paritenin işlemleri de gelir. Onlardan sinyal çıkmamalı."""
    t, flow = burst_trigger()
    t.wanted()
    trades = [Trade(XRP, x.ms, x.price, x.qty, x.buy) for x in quiet_then_burst()]
    assert feed(t, flow, trades) == []


# --- Strateji ---

def rule(**kw) -> SpikeFade:
    p = dict(size_pct=1, max_risk_pct=3, max_positions=4, max_margin_pct=15, entry_pct=0.5, add_pct=1.5,
             order_ttl_s=15, retrace=0.5, min_tp_pct=1.2, sl_pct=3, hold_s=120, max_leverage=20)
    p.update(kw)
    return SpikeFade(**p, catalog=catalog())


def ctx(wakes, equity=10.0, positions=(), orders=(), leverages=None) -> Context:
    state = AccountState(1, equity, equity, tuple(positions), tuple(orders), leverages or {})
    return Context(now=NOW, wakes=wakes, state=state, prices={SOL: 108.0}, symbols=(SOL, XRP), market=None)


def signal(symbol=SOL, up=True, start=100.0, tip=108.0) -> Wake:
    b = Burst(symbol, up, start, tip, 0.04, 0.08, 100, 0.9, 0.07)
    return Wake("trigger", NOW, Fire("trade_burst", NOW, symbol, b, NOW * 1000))


def position(symbol=SOL, entry_time=NOW - 30, margin=0.36) -> Position:
    return Position(1, 1, symbol, Side.SHORT, 0.05, margin, 108.5, entry_time, 108.5, entry_time, 108.5, entry_time,
                    150, 0.001, 0)


def entry(number=1, symbol=SOL, created_at=NOW - 5) -> Order:
    return Order(1, 1, number, symbol, Side.SHORT, OrderType.LIMIT, False, 0.05, 109.6, None, None, 0.37, (), None,
                 created_at)


def test_ten_dollar_account_opens_one_leg_at_the_minimum_size():
    """10 dolarda %1 hedef 0,10 dolardır, en küçük büyüklüğün altında kalır. Giriş en küçük miktar olan 0,05 SOL
    ile tek bacaktır: 108 × 1,005 = 108,54. SL girişin %3 ötesi 111,7962, olası kayıp 0,168 dolar, sınır 0,30.
    TP sıçramanın yarısı geri alınınca 104, en küçük TP uzaklığı 107,24'ten daha uzak. Kaldıraç pozisyon başı
    marj payı 0,375 dolara sığan en küçük tamsayı: 5,427 / 0,375 = 14,5, yani 15x. Güvenli sınır 1 / 0,06 = 16."""
    d = asyncio.run(rule().decide(ctx([signal()])))
    assert d.commands[0] == SetLeverage(SOL, 15)
    order = d.commands[1]
    assert (order.symbol, order.side, order.type, order.qty) == (SOL, Side.SHORT, OrderType.LIMIT, 0.05)
    assert order.price == pytest.approx(108.54)
    tp, sl = order.protection
    assert tp.type is OrderType.TAKE_PROFIT and tp.price == pytest.approx(104)
    assert sl.type is OrderType.STOP and sl.by_last and sl.price == pytest.approx(111.7962)
    assert d.chained == [False, True] and d.wait_s == 15
    assert d.record["signals"][0]["result"] == "açıldı"


def test_crash_is_faded_with_a_long_below_the_tip():
    """Çöken iğneye long açılır. Giriş ucun altında, SL girişin altında, TP yukarıda olmalı. SL girişin altında
    olduğu için miktar SL'nin fiyatında da 5 dolarlık en küçük büyüklüğü geçmeli, yoksa SL emri reddedilir ve
    pozisyon korumasız kalır: 0,05 × 104,24 = 5,21."""
    d = asyncio.run(rule().decide(ctx([signal(up=False, start=116.0, tip=108.0)])))
    order = next(c for c in d.commands if isinstance(c, PlaceOrder))
    tp, sl = order.protection
    assert order.side is Side.LONG and order.price == pytest.approx(107.46)
    assert sl.price == pytest.approx(107.46 * 0.97) and tp.price == pytest.approx(112)
    assert order.qty == 0.05 and order.qty * sl.price >= 5


def test_large_account_splits_into_two_legs_with_the_stop_beyond_the_far_one():
    """Hedefin yarısı iki bacakta da en küçük büyüklüğü geçince ikinci limit verilir. SL en uzak girişin ötesinde
    durur, yoksa ikinci giriş dolar dolmaz SL'nin yanlış tarafında kalırdı: 108 × 1,015 × 1,03 = 112,9086."""
    d = asyncio.run(rule().decide(ctx([signal()], equity=2000)))
    legs = [c for c in d.commands if isinstance(c, PlaceOrder)]
    assert [(round(c.price, 2), c.qty) for c in legs] == [(108.54, 0.09), (109.62, 0.09)]
    assert legs[1].protection[1].price == pytest.approx(112.9086)
    assert d.chained[-1] is True


def test_signal_is_skipped_when_the_stop_would_lose_too_much():
    """10 dolarda olası kayıp 0,168 dolar. Sınır %1 yani 0,10 dolarsa işlem açılmamalı ve nedeni kayda geçmeli."""
    d = asyncio.run(rule(max_risk_pct=1).decide(ctx([signal()])))
    assert d.commands == [] and "olası kayıp 0.17" in d.record["signals"][0]["result"]


def test_leverage_beyond_the_safe_limit_is_refused():
    """SL %30 uzaktaysa güvenli kaldıraç 1 / 0,6 = 1x'tir. Marj payına sığmak için 15x gerekiyorsa izole marjinde
    likidasyon SL'den önce gelir, işlem açılmamalı."""
    d = asyncio.run(rule(sl_pct=30, max_risk_pct=50).decide(ctx([signal()])))
    assert d.commands == [] and d.record["signals"][0]["result"] == "marj payına 15x gerekiyor, en fazla 1x olabilir"


def test_busy_symbol_and_full_book_skip_the_signal():
    """Aynı paritede pozisyonlar üst üste binmez. Pozisyon sınırı doluyken yeni sinyal atlanır."""
    d = asyncio.run(rule().decide(ctx([signal()], positions=[position()])))
    assert not any(isinstance(c, PlaceOrder) for c in d.commands)
    assert d.record["signals"][0]["result"] == "paritede pozisyon ya da emir var"
    d = asyncio.run(rule(max_positions=1).decide(ctx([signal()], positions=[position(XRP)])))
    assert d.record["signals"][0]["result"] == "pozisyon sınırı dolu"


def test_time_limit_closes_and_stale_entries_are_cancelled():
    """İğne dönmezse pozisyon süre sınırında kapanır. Dolmayan giriş ömrü dolunca iptal edilir, sonradan dolup
    eski bir sinyalle pozisyon açmamalı. Bir sonraki uyanma en yakın sınıra göredir."""
    old, fresh = position(entry_time=NOW - 120), position(XRP, entry_time=NOW - 100)
    orders = [entry(1, SOL, NOW - 15), entry(2, XRP, NOW - 10)]
    d = asyncio.run(rule().decide(ctx([], positions=[old, fresh], orders=orders)))
    assert d.commands == [ClosePosition(SOL, Side.SHORT), CancelOrder(Ref(1, SOL, Side.SHORT))]
    assert d.wait_s == 5


def test_entry_left_after_the_position_closes_is_cancelled_at_once():
    """İlk bacak TP ile kapanınca ikinci bacak beklerse iğnenin ikinci ucu boş hesaba yeni pozisyon açar."""
    closed = AccountEvent(EventKind.CLOSED, 1, NOW, Actor.SYSTEM, SOL, Side.SHORT)
    d = asyncio.run(rule().decide(ctx([Wake("account", NOW, closed)], orders=[entry(2, SOL, NOW - 3)])))
    assert d.commands == [CancelOrder(Ref(2, SOL, Side.SHORT))]
