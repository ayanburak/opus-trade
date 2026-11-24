"""
file: test_watching.py
importable: yok

Dinlenen paritelerin, tekil işlemle fiyatlamanın ve Supervisor'ın tekil
işlem dağıtımının testleri. Burada bir hata ya pozisyonu olan bir paritenin
fiyatını kaybettirir, ya bir emri geçmişin ya da iki kez görülmüş bir fiyatın
üzerinden doldurur, ya da saniyede yüzlerce işlemi ilgisiz profillere taşır.

description: Dinlenen küme, tekil işlemle fiyatlama ve evren için birim testleri.
"""

import asyncio

import pytest

from opus_trade.domain.models import Side
from opus_trade.domain.profile import ProfileConfig, Universe
from opus_trade.domain.trading import Actor, ClosePosition, OrderType, PlaceOrder
from opus_trade.market.models import Candle, Trade
from opus_trade.market.stream import Feed
from opus_trade.registry import Param
from opus_trade.runtime.profiles import ProfileError
from opus_trade.strategy.base import STRATEGIES, Decision, Strategy
from opus_trade.triggers.base import TRIGGERS, Trigger
from tests.unit.runtime.world import SOL, T, World

XRP = "XRPUSDT"


# İstediği pariteleri sınıf alanından okuyan tetikleyici. Testler listeyi
# değiştirerek izleme listesinin saat başı değişmesini taklit eder.
class Probe(Trigger):
    kind = "probe"
    wanted_now: dict = {}

    def wanted(self):
        return dict(Probe.wanted_now)


if "probe" not in TRIGGERS.entries:
    TRIGGERS.register("probe", "Test", "", (), lambda setup: Probe())


class Thinker(Strategy):
    uses_model = True

    async def decide(self, ctx):
        return Decision()


if "thinker" not in STRATEGIES.entries:
    STRATEGIES.register("thinker", "Test", "", (Param("x", "int", "", default=0),), lambda deps, x: Thinker(), Thinker)


def probing(w: World, wanted: dict):
    Probe.wanted_now = wanted
    p = w.service.create("Akış", "A", ProfileConfig("scripted", {}, (SOL,), 100.0, ({"type": "probe"},)))
    w.service.move(p.id, "start")
    return p, w.sup.runners[p.id]


def trade(symbol: str, price: float, ms: int = T * 1000) -> Trade:
    return Trade(symbol, ms, price, 1, True)


def test_profile_without_a_list_listens_to_its_own_candles(tmp_path):
    """Bugünkü profillerin hiçbir tetikleyicisi liste istemez. Onların dinlediği şey değişirse TP, SL ve emirleri
    fiyat almaz."""
    w = World(tmp_path)
    _, r = w.running()
    assert r.wants == {SOL: frozenset({Feed.KLINE})}
    assert w.sup._wants() == {SOL: (Feed.KLINE,)}


def test_traded_symbol_is_priced_by_trades_not_by_candles(tmp_path):
    """Limit emir fiyat seviyeyi bir tick geçince dolar. Tekil işlem dinlenen paritede mumun aralığı emirden önceki
    fiyatları da taşır, mumla fiyatlansa emir hiç görülmemiş bir fiyattan dolabilirdi."""
    w = World(tmp_path)
    _, r = probing(w, {XRP: frozenset({Feed.TRADE, Feed.KLINE})})
    r.on_trade(trade(XRP, 0.60))
    asyncio.run(r.gate.submit(PlaceOrder(XRP, Side.LONG, OrderType.LIMIT, qty=20, price=0.55), Actor.ADMIN, T))
    assert r.broker.live_symbols() == {XRP}
    r.on_candle(Candle(XRP, T // 60 * 60, 0.60, 0.60, 0.50, 0.60, 1, 1, 1, 1), False)
    assert r.broker.state().positions == () and r.prices[XRP] == 0.60
    r.on_trade(trade(XRP, 0.5499))
    st = r.broker.state()
    assert [(p.symbol, p.entry_price) for p in st.positions] == [(XRP, 0.55)]


def test_symbol_with_a_position_stays_watched_after_the_list_drops_it(tmp_path):
    """İzleme listesi saat başı değişir. Listeden düşen paritede açık pozisyon varsa fiyatı kesilmemeli, yoksa TP
    ve SL çalışmaz. Pozisyon kapanınca parite bırakılır."""
    w = World(tmp_path)
    _, r = probing(w, {XRP: frozenset({Feed.TRADE})})
    r.on_trade(trade(XRP, 0.60))
    asyncio.run(r.gate.submit(PlaceOrder(XRP, Side.LONG, OrderType.MARKET, qty=20), Actor.ADMIN, T))
    Probe.wanted_now = {}
    asyncio.run(w.step(r))
    assert r.wants == {XRP: frozenset({Feed.KLINE})}
    Probe.wanted_now = {"ZECUSDT": frozenset({Feed.TRADE})}
    asyncio.run(w.step(r))
    assert r.wants[XRP] == frozenset({Feed.KLINE, Feed.TRADE})
    asyncio.run(r.gate.submit(ClosePosition(XRP, Side.LONG), Actor.ADMIN, T))
    asyncio.run(w.step(r))
    assert XRP not in r.wants


def test_symbol_entering_the_watch_starts_from_now(tmp_path):
    """Daha önce dinlenip bırakılan paritenin görülen son dakikası eskidir. Yeniden bağlanmada aradaki dakikalar
    yeni emirlere karşı oynatılırsa emir geçmişin fiyatıyla dolar."""
    w = World(tmp_path)
    _, r = probing(w, {})
    w.state.set_setting(f"price_seen:{r.pid}:{XRP}", T - 6 * 3600)
    Probe.wanted_now = {XRP: frozenset({Feed.TRADE})}
    asyncio.run(w.step(r))
    assert w.state.setting(f"price_seen:{r.pid}:{XRP}") == T // 60 * 60 - 60


def test_trades_reach_only_the_profiles_that_listen_to_them(tmp_path):
    """Tekil işlem saniyede yüzlerce gelir. Onu istemeyen profile gitmesi boşa yük, mumla fiyatlanan bir profile
    gitmesi aynı fiyatın iki kez işlenmesi olurdu."""
    w = World(tmp_path)
    _, plain = w.running()
    _, listener = probing(w, {SOL: frozenset({Feed.TRADE})})
    w.sup._wants()
    asyncio.run(w.sup.on_trade(trade(SOL, 101.0)))
    assert listener.prices[SOL] == 101.0 and plain.prices[SOL] == 100


def test_whole_market_is_only_for_rule_strategies(tmp_path):
    """Bütün piyasada Opus'un istemine beş yüz paritenin kuralları girerdi."""
    w = World(tmp_path)
    with pytest.raises(ProfileError, match="yalnızca kural stratejilerinde"):
        w.service.validate(ProfileConfig("thinker", {}, (), 100.0, universe=Universe.MARKET))
    cfg = w.service.validate(ProfileConfig("scripted", {}, (), 100.0, universe=Universe.MARKET))
    assert cfg.universe is Universe.MARKET
    with pytest.raises(ProfileError, match="En az bir parite"):
        w.service.validate(ProfileConfig("scripted", {}, (), 100.0))


def test_whole_market_profile_trades_anywhere_in_the_catalog(tmp_path):
    """Bütün piyasa seçilmiş profilin stratejisi katalogun her paritesini görür. Eski sürümlerde alan yoktur ve
    liste sayılır."""
    w = World(tmp_path)
    p = w.service.create("Piyasa", "P", ProfileConfig("scripted", {}, (), 100.0, universe=Universe.MARKET))
    w.service.move(p.id, "start")
    r = w.sup.runners[p.id]
    assert set(r.tradable) == set(w.sup.catalog.symbols) and r.wants == {}
    old = ProfileConfig.from_dict({"strategy": "scripted", "symbols": [SOL], "balance": 100})
    assert old.universe is Universe.LIST
