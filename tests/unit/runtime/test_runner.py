"""
file: test_runner.py
importable: yok

Profil servisinin, çalıştırıcının, geçersizlik gözcüsünün ve Supervisor'ın testleri. Burada bir hata
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
from tests.unit.runtime.world import SOL, T, NullStream, Scripted, World, go, opening


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


def test_reset_cancels_pending_alerts_and_wakes(tmp_path):
    """Eski hesabın alarmı ve verilmemiş uyandırması yeni hesaba geçmez. Kalsalardı eski pozisyonun notlarıyla yeni
    hesabı uyandırırlardı. Yeniden kurulan çalıştırıcı da alarmsız başlar."""
    w = World(tmp_path)
    p, r = w.running()
    w.state.add_alert(p.id, r.alerts.add(SOL, 120.0, 100.0, "eski", "strategy", T))
    w.state.push_wake(p.id, T, "account", None)
    w.service.move(p.id, "stop")
    w.service.reset_account(p.id)
    assert w.state.alerts(p.id) == [] and w.state.wakes(p.id) == []
    assert w.sup.runners[p.id].alerts.alerts == []


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
    first, last = Scripted.seen[1].wakes[0].data, Scripted.seen[2].wakes[0].data
    rejected = first["results"][0]
    assert isinstance(rejected, ActionResult) and "Açık SOLUSDT short pozisyonu yok" in rejected.message
    assert (first["rejected"], first["left"], last["left"]) == (False, 1, 0)


def test_rejected_decision_applies_nothing_and_asks_for_a_correction(tmp_path):
    """Kontrol hatası bütün kararı reddeder: komut gitmez, hafıza yazılmaz, düzeltme kalan hakla istenir."""
    w = World(tmp_path)
    no = Decision([PlaceOrder(SOL, Side.LONG, OrderType.MARKET, qty=1)], ["aç"], memory={"notebook": "yeni"},
                  rejection=["Kontrol: adım 2. Neden: çelişki."], wait_s=900)
    Scripted.script = [no, opening()]
    p, r = w.running()
    w.journal.set_memory(p.id, {"notebook": "eski"})
    go(w.step(r))
    data = Scripted.seen[1].wakes[0].data
    assert data["rejected"] and data["left"] == 1 and "adım 2" in data["results"][0].message
    first = w.journal.entries(p.id)[-1]
    assert first["results"][0]["ok"] is False and first["results"][0]["label"] == "kontrol"
    assert len(r.broker.state().positions) == 1
    assert w.journal.memory(p.id) == {"notebook": "eski"}


def test_rejections_past_the_rights_notify_and_wait(tmp_path):
    """Hak bitince yöneticiye bildirilir ve bir sonraki çağrı stratejinin verdiği beklemeyle olur."""
    w = World(tmp_path)
    queue = w.sup.bus.subscribe(unbounded=True)
    no = lambda: Decision(rejection=["Kontrol: adım 4. Neden: eksik."], wait_s=900)
    Scripted.script = [no(), no(), no()]
    p, r = w.running()
    go(w.step(r))
    assert len(Scripted.seen) == 3 and r.next_wake == T + 900
    seen = [queue.get_nowait() for _ in range(queue.qsize())]
    assert any("decision_rejected" in x for x in seen if isinstance(x, dict))
    assert w.state.system_events(p.id)[0]["kind"] == "decision_rejected"


def test_entry_notes_and_reviews_are_written_with_the_journal_row(tmp_path):
    """Girişin notu olayın emir ya da pozisyon kimliğiyle yazılır, dolan emrin notu pozisyona geçer."""
    w = World(tmp_path)
    note = {"symbol": SOL, "side": "long", "reason": {"karsi_kanit": "x"}, "invalid_price": 90.0,
            "invalid_text": "90 altı"}
    d = Decision([PlaceOrder(SOL, Side.LONG, OrderType.MARKET, qty=1),
                  PlaceOrder(SOL, Side.SHORT, OrderType.LIMIT, qty=1, price=101)], ["aç", "emir"],
                 annotations=[note, {**note, "side": "short", "invalid_price": 103.0}],
                 reviews=[{"position_id": 1, "verdict": "tez_yanlisti", "text": "y"}])
    Scripted.script = [d]
    p, r = w.running()
    go(w.step(r))
    jid = w.journal.entries(p.id)[0]["id"]
    pos = r.broker.state().position(SOL, Side.LONG)
    order = r.broker.state().orders[0]
    assert [n.journal_id for n in r.notes.for_position(pos.id)] == [jid]
    assert r.notes.for_order(order.id).invalid_price == 103.0
    r.on_candle(Candle(SOL, T // 60 * 60, 100, 101.5, 100, 101.5, 1, 1, 1, 1), False)
    short = r.broker.state().position(SOL, Side.SHORT)
    assert r.notes.for_position(short.id)[0].invalid_price == 103.0
    assert pos.id == 1 and r.notes.reviews([1])[1]["journal_id"] == jid


def test_expectation_alarm_replaces_the_old_one_of_the_same_pair(tmp_path):
    """Opus yalnızca son beklediği fiyatta çağrılır, eski beklenti alarmı yenisinden önce kalkar."""
    w = World(tmp_path)
    expect = lambda x: Decision(alarms=[{"op": "expect", "symbol": SOL, "price": x, "note": "bekle", "label": "b"}])
    Scripted.script = [expect(90.0), expect(110.0)]
    p, r = w.running()
    go(w.step(r))
    r.wake("timer")
    go(w.step(r))
    assert [(a.price, a.purpose, a.actor) for a in r.alerts.alerts] == [(110.0, "expectation", "system")]
    assert [a.price for a in w.state.alerts(p.id)] == [110.0]


def test_dropped_expectation_cancels_only_the_expectation_alarm_of_that_pair(tmp_path):
    """Opus fiyat beklemeyi bırakınca paritenin beklenti alarmı iptal edilir. Opus'un kendi alarmı kalır,
    iptal edilecek alarm yoksa sonuç satırı yazılmaz."""
    w = World(tmp_path)
    expect = Decision(alarms=[{"op": "expect", "symbol": SOL, "price": 90.0, "note": "bekle", "label": "b"},
                              {"op": "add", "symbol": SOL, "price": 95.0, "note": "benim", "label": "a"}])
    drop = Decision(alarms=[{"op": "unexpect", "symbol": SOL, "label": "b"}])
    Scripted.script = [expect, drop, drop]
    p, r = w.running()
    go(w.step(r))
    for _ in range(2):
        r.wake("timer")
        go(w.step(r))
    assert [(a.price, a.purpose) for a in r.alerts.alerts] == [(95.0, "opus")]
    assert [a.price for a in w.state.alerts(p.id)] == [95.0]
    second, first = w.journal.entries(p.id)[:2]
    assert [x["message"] for x in first["results"]] == ["artık beklenmeyen fiyatın alarmı #1 iptal edildi."]
    assert second["results"] == []


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


# --- Geçersizlik gözcüsü ---

INV_NOTE = {"symbol": SOL, "reason": {"karsi_kanit": "x"}, "invalid_text": "tez bozulur"}


def _inv_candle(high, low, close=None):
    return Candle(SOL, T // 60 * 60, 100, high, low, close if close is not None else (high + low) / 2, 1, 1, 1, 1)


def _inv_setup(w, *decisions):
    Scripted.script = list(decisions)
    p, r = w.running()
    go(w.step(r))
    return p, r


def test_pending_entry_is_cancelled_by_the_system_without_waking(tmp_path):
    """Bekleyen short girişin geçersizliğine son fiyat değince emir sistemce iptal edilir, Opus çağrılmaz."""
    w = World(tmp_path)
    d = Decision([PlaceOrder(SOL, Side.SHORT, OrderType.LIMIT, qty=1, price=102)], ["emir"],
                 annotations=[{**INV_NOTE, "side": "short", "invalid_price": 101.5}])
    p, r = _inv_setup(w, d)
    order = r.broker.state().orders[0]
    r.on_candle(_inv_candle(101.4, 100), False)
    go(w.step(r))
    assert [o.id for o in r.broker.state().orders] == [order.id]
    r.on_candle(_inv_candle(101.6, 100.5), False)
    r.on_candle(_inv_candle(101.8, 100.5), False)
    assert len(r._cancels) == 1, "Aynı iptal iki kez sıraya girerse canlı broker'a iki istek gider."
    go(w.step(r))
    assert r.broker.state().orders == ()
    events = w.ledger.events(r.broker.account_id, kinds=("order_cancelled",))
    assert [e["actor"] for e in events] == ["system"]
    assert w.state.wakes(p.id) == []
    assert [n.id for n, _ in r.notes.cancelled_since(p.id, 0)] == [r.notes.for_order(order.id).id]


def test_open_position_hit_wakes_once(tmp_path):
    """Açık pozisyonun geçersizliğine değince Opus bir kez uyandırılır, fiyat seviyede gezinse de ikinci kez değil."""
    w = World(tmp_path)
    d = Decision([PlaceOrder(SOL, Side.LONG, OrderType.MARKET, qty=1)], ["aç"],
                 annotations=[{**INV_NOTE, "side": "long", "invalid_price": 99.0}])
    p, r = _inv_setup(w, d)
    r.on_candle(_inv_candle(100, 98.9), False)
    r.on_candle(_inv_candle(100, 98.5), False)
    wakes = w.state.wakes(p.id)
    assert [k for _, _, k, _ in wakes] == ["invalidation"]
    assert "geçersizlik fiyatına, 99,00, son fiyat değdi" in wakes[0][3]


def test_filled_order_is_watched_as_a_position(tmp_path):
    """Emir dolunca not pozisyona geçer, gözcü artık iptal değil uyandırma yapar."""
    w = World(tmp_path)
    d = Decision([PlaceOrder(SOL, Side.LONG, OrderType.LIMIT, qty=1, price=99)], ["emir"],
                 annotations=[{**INV_NOTE, "side": "long", "invalid_price": 97.0}])
    p, r = _inv_setup(w, d)
    r.on_candle(_inv_candle(100, 98.9), False)
    go(w.step(r))
    assert len(r.broker.state().positions) == 1
    r.on_candle(_inv_candle(99, 96.9), False)
    go(w.step(r))
    assert r.broker.state().orders == () or all(o.reduce for o in r.broker.state().orders)
    assert [c.wakes[0].kind for c in Scripted.seen[1:]] == ["account", "invalidation"]
