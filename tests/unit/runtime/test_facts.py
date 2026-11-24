"""
file: test_facts.py
importable: yok

Hesap gerçeklerinin testleri. Opus bunları saymaz, doğru sanar. Yanlış bir
sayı ya Opus'u bir stop serisinin içindeyken rahatlatır ya da yöneticinin
açtığı pozisyonu Opus'un tezi gibi sınıflandırmaya zorlar. Hesap gerçek kâğıt
broker'la kurulur, olaylar onun yazdığı gibidir.

description: runtime.facts için birim testleri.
"""

import asyncio
from datetime import datetime

import pytest

from opus_trade.broker.paper import PaperBroker
from opus_trade.domain.clock import HOME_TZ
from opus_trade.domain.fees import BINANCE_STANDARD as FEES
from opus_trade.domain.models import Side
from opus_trade.domain.trading import Actor, CancelOrder, ClosePosition, OrderType, PlaceOrder, Protection, Ref
from opus_trade.runtime import facts
from opus_trade.storage.database import connect
from opus_trade.storage.journal import JournalRepo
from opus_trade.storage.ledger import MIGRATIONS, Ledger
from opus_trade.storage.migrate import migrate
from opus_trade.storage.notes import NotesRepo
from tests.fakes import FakeClock, catalog

# 20.11.2026 Cuma 23:00 Türkiye saati. Gece yarısı bir saat sonra.
T = int(datetime(2026, 11, 20, 23, 0, tzinfo=HOME_TZ).timestamp())
SOL = "SOLUSDT"
LONG, SHORT = Side.LONG, Side.SHORT


class Desk:
    """Bir profilin hesabı, günlüğü ve not deposu, gerçek kâğıt broker'la."""

    def __init__(self, path):
        con = connect(path / "core.db")
        migrate(con, MIGRATIONS, path / "bak", catalog=catalog())
        self.ledger = Ledger(con)
        self.clock = FakeClock(T)
        con.execute("INSERT INTO profiles (id, name, short, area, created_at) VALUES (1, 'A', 'a', 'paper', ?)", (T,))
        con.execute("INSERT INTO profile_versions (id, profile_id, number, config, created_at) VALUES (1, 1, 1, '{}', ?)",
                    (T,))
        con.commit()
        self.broker = PaperBroker(self.ledger, self.ledger.create_account("paper", 1000.0, T), catalog(), FEES,
                                  self.clock)
        self.journal = JournalRepo(con)
        self.notes = NotesRepo(con)
        self.at(T)
        self.price(100)

    def at(self, t: int) -> None:
        self.clock.t = t

    def price(self, last: float) -> None:
        self.broker.on_mark(SOL, last, last, last, self.clock.time(), last)
        self.broker.on_price(SOL, last, last, last, last, self.clock.time())

    def run(self, cmd, actor=Actor.STRATEGY):
        return asyncio.run(self.broker.execute(cmd, actor))

    def open(self, side=LONG, qty=1.0, actor=Actor.STRATEGY, sl: float | None = None):
        protection = (Protection(OrderType.STOP, sl),) if sl else ()
        return self.run(PlaceOrder(SOL, side, OrderType.MARKET, qty=qty, protection=protection), actor)

    def close(self, side=LONG, qty=None, actor=Actor.STRATEGY):
        return self.run(ClosePosition(SOL, side, qty), actor)

    def facts(self, version_start: int = 0):
        return facts.build(self.ledger, self.journal, self.notes, 1, self.broker.state(), version_start,
                           self.clock.time())


@pytest.fixture
def desk(tmp_path):
    return Desk(tmp_path)


def test_partial_closes_count_as_one_position_with_their_total_net(desk):
    """Kısmi kapanışlı pozisyon tek işlemdir. Parçalar ayrı sayılsa günün işlem sayısı ve art arda kayıp şişerdi."""
    desk.open(qty=2)
    desk.price(99)
    desk.close(qty=1)
    assert desk.facts().recent == ()
    desk.price(98)
    desk.close()
    f = desk.facts()
    assert len(f.recent) == 1 and f.today_count == 1 and f.loss_streak == 1
    assert f.recent[0].net == pytest.approx(sum(t.net for t in desk.ledger.trades(desk.broker.account_id)))
    assert f.recent[0].reason == "strategy" and not f.recent[0].stopped


def test_admin_opened_and_admin_closed_positions_are_not_waiting_for_review(desk):
    """Sınıflandırma Opus'un tezini ölçer. Yöneticinin açtığı ya da kapattığı pozisyon Opus'a sınıflandırtılmaz."""
    desk.open(actor=Actor.ADMIN)
    desk.close()
    desk.open()
    desk.close(actor=Actor.ADMIN)
    desk.open()
    desk.close()
    f = desk.facts()
    assert [c.origin for c in f.recent] == ["admin", "strategy", "strategy"]
    assert [c.reason for c in f.recent] == ["strategy", "admin", "strategy"]
    assert [c.position_id for c in f.unreviewed] == [f.recent[2].position_id]


def test_reviewed_and_old_version_positions_are_not_asked_again(desk):
    """Sınıflandırılan pozisyon bir daha istenmez. Yeni sürümden önce kapananlar hiç istenmez."""
    desk.open()
    desk.close()
    desk.at(T + 100)
    desk.open()
    desk.close()
    f = desk.facts(version_start=T + 50)
    assert len(f.unreviewed) == 1
    desk.notes.add_review(f.unreviewed[0].position_id, 1, None, "tez_dogruydu", "x", T + 200)
    assert desk.facts(version_start=T + 50).unreviewed == ()


def test_order_filled_position_takes_the_origin_of_its_order(desk):
    """Bekleyen emirle açılan pozisyonun olayı sistemindir. Kaynak emri verendir, yoksa yöneticinin emri Opus'un sayılırdı."""
    desk.run(PlaceOrder(SOL, LONG, OrderType.LIMIT, qty=1, price=95), Actor.ADMIN)
    desk.run(PlaceOrder(SOL, SHORT, OrderType.LIMIT, qty=1, price=105))
    orders = {o.side: o.id for o in desk.broker.state().orders}
    assert desk.facts().order_origins == {orders[LONG]: "admin", orders[SHORT]: "strategy"}
    desk.price(94)
    p = desk.broker.state().position(SOL, LONG)
    assert desk.facts().position_origins == {p.id: "admin"}


def test_day_starts_at_midnight_turkey_time(desk):
    """Gün Türkiye saatiyle başlar. UTC ile sayılsa 03:00'e kadarki işlemler dünün sayılırdı."""
    desk.open(sl=95)
    desk.price(94)
    assert desk.facts().today_stops == 1
    desk.at(T + 3600 + 60)
    f = desk.facts()
    assert (f.today_count, f.today_stops, f.today_net) == (0, 0, 0)
    assert f.last_stop == {"SOLUSDT long": True}


def test_profitable_trailing_exit_is_not_a_stop(desk):
    """İz süren stop SL nedeniyle kapanır ama kârdaysa stop değildir, Opus'tan son stoptan farkı istenmez."""
    desk.open()
    desk.price(103)
    desk.run(PlaceOrder(SOL, LONG, OrderType.STOP, qty=None, price=102, reduce=True))
    desk.price(101)
    f = desk.facts()
    assert f.recent[-1].reason == "sl" and not f.recent[-1].stopped
    assert f.last_stop == {"SOLUSDT long": False}


def test_fill_rate_counts_only_the_strategys_last_ten_entries(desk):
    """Dolma oranı Opus'un girişlerinin isabetidir. Yöneticinin emri ya da on emirden eskisi oranı bozar."""
    for i in range(12):
        desk.run(PlaceOrder(SOL, LONG, OrderType.LIMIT, qty=0.1, price=90))
        o = desk.broker.state().orders[0]
        desk.at(T + i + 1)
        desk.run(CancelOrder(Ref(o.number, SOL, LONG)))
    desk.run(PlaceOrder(SOL, LONG, OrderType.LIMIT, qty=0.1, price=99))
    desk.run(PlaceOrder(SOL, SHORT, OrderType.LIMIT, qty=0.1, price=101), Actor.ADMIN)
    desk.at(T + 100)
    desk.price(98)
    desk.price(102)
    assert desk.facts().fill_rate == (1, 10)


def test_lateness_is_reported_only_past_the_threshold(desk):
    """Birkaç dakikalık gecikme söylenmez. Eşik aşılınca Opus kör kaldığı süreyi dakikasıyla görür."""
    desk.journal.add(1, 1, T, [], [], {}, None, T + 600)
    desk.at(T + 600 + facts.LATE_MIN * 60 - 1)
    assert desk.facts().late_min is None
    desk.at(T + 600 + 3 * 3600)
    f = desk.facts()
    assert (f.late_min, f.last_call) == (180, T)
    desk.journal.add(1, 1, T + 20000, [], [], {}, "hata", None)
    assert desk.facts().last_call == T


def test_admin_events_of_the_last_day_are_listed(desk):
    """Yöneticinin işlemleri yirmi dört saat istemde kalır, sonra düşer."""
    desk.open(actor=Actor.ADMIN)
    desk.open(side=SHORT)
    texts = desk.facts().admin_24h
    assert len(texts) == 1 and texts[0].endswith("opened")
    desk.at(T + 86400 + 1)
    assert desk.facts().admin_24h == ()


def test_notes_of_open_orders_and_positions_come_with_the_facts(desk):
    """Emrin ve pozisyonun yanında tezi görünür. Dolan emrin notu pozisyona geçer."""
    desk.run(PlaceOrder(SOL, LONG, OrderType.LIMIT, qty=1, price=95))
    o = desk.broker.state().orders[0]
    desk.notes.add_entry(1, desk.broker.account_id, SOL, "long", {"karsi_kanit": "x"}, 90.0, "90 altı", T,
                         order_id=o.id)
    assert desk.facts().order_notes[o.id].invalid_price == 90.0
    desk.price(94)
    p = desk.broker.state().position(SOL, LONG)
    desk.notes.attach_position(o.id, p.id)
    f = desk.facts()
    assert f.order_notes == {} and f.position_notes[p.id][0].invalid_text == "90 altı"


def test_team_tasks_of_today_are_counted_by_role(desk):
    """Günlük kota bugün çalışan alt görevlerden hesaplanır. Dünkü ve atlanan görev sayılmaz,
    hatalı çağrının görevi sayılır."""
    haiku, opus = {"role": "haiku"}, {"role": "opus"}
    desk.journal.add(1, 1, T - 86400, [], [], {"team": [haiku, haiku]}, None, None)
    desk.journal.add(1, 1, T - 600, [], [], {"team": [haiku, opus, {**haiku, "skipped": True}]}, None, None)
    desk.journal.add(1, 1, T - 300, [], [], {"team": [haiku]}, "zaman aşımı", None)
    desk.journal.add(1, 1, T - 100, [], [], {}, None, None)
    assert desk.facts().team_today == {"haiku": 2, "opus": 1}
