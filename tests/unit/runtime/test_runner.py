"""
file: test_runner.py
importable: yok

Profil servisinin, çalıştırıcının ve Supervisor'ın testleri. Burada bir hata
ya bir uyandırmayı kaybeder, ya stratejiyi gereksiz yere tekrar tekrar
çağırıp limiti harcar, ya da kapalı geçen sürede bir SL'yi atlar. Testler
sahte bir stratejiyle, sahte saatle ve ağa çıkmadan çalışır.

description: runtime.profiles, runtime.runner ve runtime.supervisor için birim testleri.
"""

import asyncio
from datetime import datetime

import pytest

from opus_trade import plugins
from opus_trade.domain.clock import HOME_TZ
from opus_trade.domain.fees import BINANCE_STANDARD
from opus_trade.domain.models import Side
from opus_trade.domain.profile import ProfileConfig, State
from opus_trade.domain.trading import (AccountEvent, Actor, ClosePosition, EventKind, OrderType, PlaceOrder,
                                       Protection, SetLeverage, Trade)
from opus_trade.market.models import Candle
from opus_trade.market.stream import MarketStream
from opus_trade.registry import Param
from opus_trade.runtime import wakes
from opus_trade.runtime.bus import EventBus
from opus_trade.runtime.model_access import ModelAccess
from opus_trade.runtime.profiles import ProfileError, ProfileService
from opus_trade.runtime.runner import describe
from opus_trade.runtime.supervisor import Supervisor
from opus_trade.storage.database import connect
from opus_trade.storage.journal import JournalRepo
from opus_trade.storage.ledger import MIGRATIONS, Ledger
from opus_trade.storage.migrate import migrate
from opus_trade.storage.profiles import ProfileRepo
from opus_trade.storage.runtime_state import StateRepo
from opus_trade.strategy.base import STRATEGIES, ActionResult, Decision, Deps, Strategy
from tests.fakes import FakeClock, FakeMarket, catalog

T = 1_795_000_020
SOL = "SOLUSDT"


# Kararları sırayla verilen listeden döndürür, her bağlamı kaydeder. fail
# doluysa decide hata fırlatır. hold verilmişse karar o olay kurulana kadar
# bekler, düşünme süren bir çağrıyı taklit eder. Düzeltme ve model
# yetenekleri ayarlanabilir.
class Scripted(Strategy):
    script: list = []
    seen: list = []
    fail: list = []
    hold: asyncio.Event | None = None

    def __init__(self, corrects: bool = True, uses_model: bool = False):
        self.corrects = corrects
        self.uses_model = uses_model

    async def decide(self, ctx):
        Scripted.seen.append(ctx)
        if Scripted.hold:
            await Scripted.hold.wait()
        if Scripted.fail:
            raise RuntimeError(Scripted.fail.pop(0))
        return Scripted.script.pop(0) if Scripted.script else Decision()


if "scripted" not in STRATEGIES.entries:
    STRATEGIES.register("scripted", "Test", "", (Param("corrects", "int", "", default=1),
                                                 Param("uses_model", "int", "", default=0)),
                        lambda deps, corrects, uses_model: Scripted(bool(corrects), bool(uses_model)))


class NullStream(MarketStream):
    async def watch(self, symbols):
        self.symbols = symbols

    async def run(self, listener):
        await asyncio.Event().wait()


class World:
    def __init__(self, path):
        plugins.load()
        con = connect(path / "core.db")
        migrate(con, MIGRATIONS, path / "bak", catalog=catalog())
        self.clock = FakeClock(T)
        self.ledger, self.journal, self.state = Ledger(con), JournalRepo(con), StateRepo(con)
        self.repo = ProfileRepo(con)
        self.market = FakeMarket()
        self.access = ModelAccess(self.state, self.clock)
        self.sup = Supervisor(self.repo, self.ledger, self.journal, self.state, self.market, NullStream(), self.clock,
                              EventBus(), self.access, catalog(), BINANCE_STANDARD,
                              lambda p: Deps(self.clock, catalog()))
        self.service = ProfileService(self.repo, self.ledger, self.journal, catalog(), self.clock, self.sup.refresh)
        Scripted.script, Scripted.seen, Scripted.fail, Scripted.hold = [], [], [], None

    def config(self, **params) -> ProfileConfig:
        return ProfileConfig("scripted", params, (SOL,), 100.0)

    def running(self, **params):
        p = self.service.create("Test", "T", self.config(**params))
        self.service.move(p.id, "start")
        r = self.sup.runners[p.id]
        r.on_candle(Candle(SOL, T // 60 * 60, 100, 100, 100, 100, 1, 1, 1, 1), False)
        return p, r

    async def step(self, r, seconds: int = 0):
        self.clock.t += seconds
        r.tick(self.clock.t)
        await r.wait_idle()


# 10x kaldıraçla 1 SOL, 100 dolardan 10 dolar marjin. Likidasyon 95'in
# altında kalır, SL'li testler likidasyona takılmaz.
def opening(*protection, wait_s=None) -> Decision:
    return Decision([SetLeverage(SOL, 10), PlaceOrder(SOL, Side.LONG, OrderType.MARKET, qty=1, protection=protection)],
                    ["kaldıraç", "aç"], wait_s=wait_s)


def go(coro):
    return asyncio.run(coro)


# --- Profil servisi ---

def test_command_result_writes_numbers_and_unit_the_turkish_way():
    """Komutun sonucu Opus'a ve panelin Onay kartına gider. Sayılar virgüllü, miktar paritenin birimiyle yazılır.
    Emir türüyle anılır: stop hem kırılım girişi hem SL olabildiği için kapatan emir olup olmadığı ayrıca okunur.
    Miktarsız koruma kalanın tamamını kapatır."""
    t = Trade(1, 1, 1, "ETHUSDT", Side.LONG, 0.041, 6.6, 1600, 0, 1608.913, 60, "admin", 0.37, 0.07, 0.0, 1610, 1599)
    ev = lambda kind, **kw: AccountEvent(kind, 1, 0, Actor.ADMIN, "ETHUSDT", Side.LONG, **kw)
    events = [ev(EventKind.OPENED, price=1608.913, qty=0.041),
              ev(EventKind.ORDER_PLACED, number=1, price=1590, order_type=OrderType.STOP, reduce=True),
              ev(EventKind.CLOSED, price=1608.913, trade=t),
              ev(EventKind.ORDER_CANCELLED, number=1, order_type=OrderType.STOP, reduce=True, reason="position_closed"),
              ev(EventKind.ORDER_CANCELLED, number=3, order_type=OrderType.STOP)]
    assert describe(events) == (
        "ETHUSDT long pozisyonu 0,041 ETH ile 1608,9 fiyatından açıldı. #1 ETHUSDT long SL emri 1590,0 kuruldu, "
        "miktar kalanın tamamı. ETHUSDT long pozisyonundan 0,041 ETH 1608,9 fiyatından kapandı, net +0,30 dolar. "
        "#1 ETHUSDT long SL emri pozisyon kapandığı için iptal oldu. #3 ETHUSDT long kırılım giriş emri iptal edildi.")


def test_new_version_keeps_the_old_and_is_filled_with_defaults(tmp_path):
    """Ayar değişince eski sürüm silinmez. Sonuçlar sürüme göre ayrılarak incelenir."""
    w = World(tmp_path)
    p = w.service.create("Test", "T", w.config())
    w.service.update_config(p.id, w.config(corrects=0), "düzeltme kapalı")
    versions = w.repo.versions(p.id)
    assert [v.number for v in versions] == [1, 2]
    assert versions[1].config.params == {"corrects": 0, "uses_model": 0}
    with pytest.raises(ProfileError, match="Bilinmeyen parametre"):
        w.service.update_config(p.id, w.config(bad=1), "")


def test_illegal_state_moves_are_refused(tmp_path):
    """Çalışan bir profil arşivlenemez, önce durdurulmalı. Yoksa açık pozisyonları sahipsiz kalır."""
    w = World(tmp_path)
    p = w.service.create("Test", "T", w.config())
    w.service.move(p.id, "start")
    with pytest.raises(ProfileError, match="çalışıyor, arşivlenemez"):
        w.service.move(p.id, "archive")


def test_clone_gets_its_own_account_and_the_memory(tmp_path):
    """Kopya kendi hesabıyla başlar, kaynağın hesabına dokunmaz. Not defteri kopyaya geçer."""
    w = World(tmp_path)
    src = w.service.create("Kaynak", "K", w.config())
    w.journal.set_memory(src.id, {"notebook": "not"})
    copy = w.service.clone(src.id, "Kopya", "C")
    assert copy.account_id != src.account_id and copy.state is State.DRAFT
    assert w.journal.memory(copy.id) == {"notebook": "not"}


def test_reset_keeps_old_trades_and_needs_a_stopped_profile(tmp_path):
    """Sıfırlama eski hesabın kaydını silmez. Çalışırken sıfırlanırsa açık pozisyonlar kaybolurdu."""
    w = World(tmp_path)
    p, r = w.running()
    with pytest.raises(ProfileError, match="önce durdurulmalı"):
        w.service.reset_account(p.id)
    old = p.account_id
    w.service.move(p.id, "stop")
    p = w.service.reset_account(p.id)
    assert p.account_id != old and w.ledger.cash(p.account_id) == 100.0


# --- Çalıştırıcı ---

def test_first_decision_is_taken_and_recorded_with_its_version(tmp_path):
    """Her karar yürürlükteki sürümün kimliğini taşır. Ayar değişse de hangi kararın hangi ayarla verildiği bilinir."""
    w = World(tmp_path)
    Scripted.script = [opening(wait_s=300)]
    p, r = w.running()
    go(w.step(r))
    entry = w.journal.entries(p.id)[0]
    assert [x["kind"] for x in entry["wakes"]] == ["start"]
    assert entry["version_id"] == w.repo.get(p.id).version_id and entry["results"][0]["ok"]
    assert r.next_wake == T + 300 and len(r.broker.state().positions) == 1


def test_corrections_stop_after_two(tmp_path):
    """Strateji aynı hatayı tekrarlarsa düzeltme sonsuza kadar sürmez, en fazla iki kez denenir."""
    w = World(tmp_path)
    bad = lambda: Decision([ClosePosition(SOL, Side.SHORT)], ["kapat"])
    Scripted.script = [bad(), bad(), bad(), bad()]
    p, r = w.running()
    go(w.step(r))
    assert [c.wakes[0].kind for c in Scripted.seen] == ["start", "correction", "correction"]
    rejected = Scripted.seen[1].wakes[0].data[0]
    assert isinstance(rejected, ActionResult) and "Açık SOLUSDT short pozisyonu yok" in rejected.message


def test_chained_command_is_not_sent_when_the_one_before_is_refused(tmp_path):
    """Kaldıraç reddedilirse giriş eski kaldıraçla açılmaz. Yeni koruma reddedilirse eskisinin iptali gitmez,
    pozisyon korumasız kalmaz. Zincirsiz komut önceki redden etkilenmez."""
    w = World(tmp_path)
    Scripted.script = [Decision([SetLeverage(SOL, 500), PlaceOrder(SOL, Side.LONG, OrderType.MARKET, qty=1),
                                 PlaceOrder(SOL, Side.SHORT, OrderType.MARKET, qty=1)],
                                ["giriş", "giriş", "short"], chained=[False, True, False])]
    p, r = w.running(corrects=0)
    go(w.step(r))
    results = w.journal.entries(p.id)[0]["results"]
    assert [x["ok"] for x in results] == [False, False, True]
    assert "uygulanmadı" in results[1]["message"]
    assert [x.side for x in r.broker.state().positions] == [Side.SHORT]


def test_model_closed_holds_wakes_and_releases_them_together(tmp_path):
    """Limit doluyken model çağrılmaz, uyandırmalar birikir ve erişim açılınca hepsi tek kararda verilir."""
    w = World(tmp_path)
    w.access.close_until(T + 600)
    p, r = w.running(uses_model=1)
    go(w.step(r))
    r.wake("admin")
    go(w.step(r, 300))
    assert Scripted.seen == []
    go(w.step(r, 301))
    assert [x.kind for x in Scripted.seen[0].wakes] == ["start", "admin"]


def test_failed_decision_keeps_wakes_and_waits_before_retrying(tmp_path):
    """Hata veren karar uyandırmaları kaybetmez ama her saniye yeniden denenip limiti eritmez. Limit hatasındaki
    yenilenme saati panele öneri olarak geçer."""
    w = World(tmp_path)
    Scripted.fail = ["You've hit your 5-hour limit · resets 6:40am (Europe/Istanbul)"]
    p, r = w.running()
    go(w.step(r))
    go(w.step(r, 1))
    assert len(Scripted.seen) == 1
    suggested = datetime.fromtimestamp(w.access.warning()["suggested_until"], HOME_TZ)
    assert (suggested.hour, suggested.minute) == (6, 40) and suggested.timestamp() > T
    go(w.step(r, 600))
    assert len(Scripted.seen) == 2 and Scripted.seen[1].wakes[0].kind == "start"


def test_pending_wakes_survive_a_restart(tmp_path):
    """Program kapanırken sırada bekleyen bir olay yeni süreçte de stratejiye verilir."""
    w = World(tmp_path)
    w.access.close_until(T + 600)
    p, _ = w.running(uses_model=1)
    w.sup.runners.clear()
    w.access.open()
    w.sup.refresh(p.id)
    go(w.step(w.sup.runners[p.id]))
    assert [x.kind for x in Scripted.seen[0].wakes] == ["start"]


def test_paused_profile_still_hits_tp_but_strategy_is_not_woken(tmp_path):
    """Duraklatılan profilin pozisyonu korumasız kalmaz, TP fiyat gelince çalışır. Profil yeniden çalışınca
    strateji duraklatıldığı sırada olanları eski olay olarak almaz, yeniden çalıştırıldığını öğrenir."""
    w = World(tmp_path)
    Scripted.script = [opening(Protection(OrderType.TAKE_PROFIT, 105))]
    p, r = w.running()
    go(w.step(r))
    w.service.move(p.id, "pause")
    r = w.sup.runners[p.id]
    r.on_candle(Candle(SOL, T // 60 * 60 + 60, 100, 106, 100, 105, 1, 1, 1, 1), False)
    go(w.step(r, 60))
    assert r.broker.state().positions == () and len(Scripted.seen) == 1
    w.service.move(p.id, "start")
    go(w.step(w.sup.runners[p.id], 1))
    assert [x.kind for x in Scripted.seen[-1].wakes] == ["resume"]


def test_position_opened_mid_minute_ignores_earlier_prices_of_that_minute(tmp_path):
    """Dakikalık mum dakika boyunca birikmiş tepeyi taşır. Bu tepe pozisyondan önce görüldüyse TP'yi
    tetiklememeli, yoksa pozisyon açıldığı saniyede gerçekte hiç görmediği bir fiyattan kapanır. Sonradan gelen
    yeni tepe ise tetiklemeli."""
    w = World(tmp_path)
    Scripted.script = [opening(Protection(OrderType.TAKE_PROFIT, 105))]
    p, r = w.running()
    m = T // 60 * 60
    r.on_candle(Candle(SOL, m, 100, 110, 100, 100, 1, 1, 1, 1), False)
    go(w.step(r))
    assert len(r.broker.state().positions) == 1
    r.on_candle(Candle(SOL, m, 100, 110, 99, 100, 1, 1, 1, 1), False)
    assert len(r.broker.state().positions) == 1
    r.on_candle(Candle(SOL, m, 100, 111, 99, 106, 1, 1, 1, 1), False)
    assert r.broker.state().positions == ()


def test_mark_price_drives_the_sl_and_fills_at_the_last_price(tmp_path):
    """Canlı mark fiyatı SL'ye değince pozisyon o anın son fiyatından kapanır. Son fiyattaki iğne tek başına SL'yi
    çalıştırmaz."""
    w = World(tmp_path)
    Scripted.script = [opening(Protection(OrderType.STOP, 95))]
    p, r = w.running()
    m = T // 60 * 60
    r.on_candle(Candle(SOL, m, 100, 100, 100, 100, 1, 1, 1, 1), False)
    go(w.step(r))
    r.on_candle(Candle(SOL, m, 100, 100, 90, 94.5, 1, 1, 1, 1), False)
    assert len(r.broker.state().positions) == 1
    r.on_mark(SOL, 94.9, T + 5)
    [t] = r.ledger.trades(r.broker.account_id)
    assert t.reason == "sl" and t.exit_price == pytest.approx(94.5 * (1 - 0.0002))


def test_alert_set_mid_minute_ignores_earlier_prices_of_that_minute(tmp_path):
    """Dakika içinde kurulan alarm, kurulmadan önce görülen dip yüzünden hemen tetiklenmemeli. Fiyat alarmın
    seviyesine sonradan inerse, dakikanın dibi değişmese bile kapanışla tetiklenmeli."""
    w = World(tmp_path)
    p, r = w.running()
    m = T // 60 * 60
    r.on_candle(Candle(SOL, m, 100, 100, 90, 100, 1, 1, 1, 1), False)
    r.alerts.add(SOL, 95, 100, "", "admin", w.clock.t)
    r.on_candle(Candle(SOL, m, 100, 100, 90, 99, 1, 1, 1, 1), False)
    assert len(r.alerts.alerts) == 1
    r.on_candle(Candle(SOL, m, 100, 100, 90, 94, 1, 1, 1, 1), False)
    assert r.alerts.alerts == []


def test_missed_minutes_are_replayed_after_a_gap(tmp_path):
    """Kapalı geçen dakikadaki SL atlanırsa pozisyon gerçekte kapanmış olduğu halde açık görünür."""
    w = World(tmp_path)
    Scripted.script = [opening(Protection(OrderType.STOP, 95))]
    p, r = w.running()
    go(w.step(r))
    r.on_candle(Candle(SOL, T // 60 * 60, 100, 100, 100, 100, 1, 1, 1, 1), True)
    base = T // 60 * 60
    w.market.minutes[SOL] = [Candle(SOL, base + 60 * k, 100, 100, 94 if k == 3 else 99, 99, 1, 1, 1, 1)
                             for k in range(1, 10)]
    w.market.marks[SOL] = w.market.minutes[SOL]
    w.clock.t = base + 600
    go(r.catch_up())
    go(w.step(r))
    assert r.broker.state().positions == ()
    kinds = [x.kind for x in Scripted.seen[-1].wakes]
    assert "account" in kinds and "restart" in kinds


def test_version_change_while_deciding_waits_for_the_decision(tmp_path):
    """Opus düşünürken sürüm değişirse süren karar canlı fiyatla biter, seçtiği sonraki çağrı saati yeni sürüme geçer
    ve strateji fazladan çağrılmaz. Yeni sürüm karar bitince yürürlüğe girer. Bozulursa işlem eski fiyatla açılır ya
    da Opus boşuna bir kez daha çağrılıp limit harcanır."""
    w = World(tmp_path)

    async def scenario():
        Scripted.hold = asyncio.Event()
        Scripted.script = [opening(wait_s=600)]
        p, r = w.running()
        r.tick(w.clock.t)
        await asyncio.sleep(0)
        assert r.busy
        w.service.update_config(p.id, w.config(corrects=0), "yeni sürüm")
        await w.sup.on_candle(Candle(SOL, T // 60 * 60, 120, 120, 120, 120, 1, 1, 1, 1), False)
        r.wake("admin")
        for _ in range(3):
            w.clock.t += 1
            for x in list(w.sup.runners.values()):
                x.tick(w.clock.t)
        Scripted.hold.set()
        Scripted.hold = None
        await r.wait_idle()
        r.tick(w.clock.t)
        assert not r.busy and len(Scripted.seen) == 1
        for _ in range(3):
            await asyncio.sleep(0)
        new = w.sup.runners[p.id]
        assert new.version.id == w.repo.get(p.id).version_id and new.version.config.params["corrects"] == 0
        assert new.broker.state().positions[0].entry_price > 110
        assert new.next_wake == T + 600
        w.clock.t += 1
        new.tick(w.clock.t)
        await new.wait_idle()
        assert len(Scripted.seen) == 2 and [x.kind for x in Scripted.seen[1].wakes] == ["admin"]

    go(scenario())


def test_wake_saved_before_the_binance_model_still_decodes():
    """Göçten önce sırada bekleyen kapanış olayı eski alanları taşır: numara, kaldıraç ve yüzdeli TP kademeleri.
    Okunamazsa yayından sonraki ilk açılışta profilin karar döngüsü hata verir."""
    old = ('{"type": "event", "kind": "closed", "account_id": 1, "time": 5, "actor": "system", "number": 3, '
           '"symbol": "SOLUSDT", "side": "long", "price": 97.0, "position_id": 2, "order_id": null, '
           '"reason": "sl", "amount": null, "trade": {"id": 1, "account_id": 1, "position_id": 2, "number": 3, '
           '"symbol": "SOLUSDT", "side": "long", "leverage": 10, "qty": 1, "margin": 10, "entry_price": 100, '
           '"entry_time": 0, "exit_price": 97, "exit_time": 5, "reason": "sl", "pnl": -3, "fee": 0.1, '
           '"funding": 0, "best_price": 101, "worst_price": 97, "sl": 97, '
           '"tp_levels": [{"price": 105, "pct": 100, "qty": null}]}}')
    e = wakes.decode(old)
    assert (e.kind, e.number, e.side, e.trade.tp_prices, e.trade.sl) == (EventKind.CLOSED, 3, Side.LONG, (105,), 97)
    assert wakes.decode(wakes.encode(e)) == e


def test_wake_keeps_the_order_type_and_whether_it_closes():
    """Bekleyen bir dolum olayı yeniden başlatmadan sonra türünü kaybederse Opus emri yanlış adla görür."""
    e = AccountEvent(EventKind.ORDER_FAILED, 1, 5, Actor.SYSTEM, SOL, Side.LONG, price=95.0, reason="geçti",
                     order_type=OrderType.STOP, reduce=True)
    back = wakes.decode(wakes.encode(e))
    assert back == e and back.order_type is OrderType.STOP
