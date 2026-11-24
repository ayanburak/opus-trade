"""
file: test_notes.py
importable: yok

Giriş notları deposunun ve 0009 taşımasının testleri. Bir not yanlış emre ya
da pozisyona bağlanırsa Opus başka bir girişin tezini kendi tezi sanır. Bir
geçersizlik iki kez işlenirse Opus aynı olay için boşuna iki kez çağrılır.
Taşıma eski alarmların amacını yanlış doldurursa yöneticinin alarmı Opus'un
alarmı gibi görünür.

description: storage.notes ve 0009_decision_notes taşıması için birim testleri.
"""

import shutil

import pytest

from opus_trade.storage.database import connect
from opus_trade.storage.ledger import MIGRATIONS
from opus_trade.storage.migrate import migrate
from opus_trade.storage.notes import NotesRepo
from opus_trade.storage.runtime_state import StateRepo
from opus_trade.triggers.price_alert import PriceAlerts
from tests.fakes import catalog

T = 1_795_000_000


def _base(con) -> None:
    con.execute("INSERT INTO accounts (id, kind, start_balance, cash, created_at) VALUES (1, 'paper', 10, 10, ?)", (T,))
    con.execute("INSERT INTO profiles (id, name, short, area, account_id, created_at) VALUES (1, 'A', 'a', 'paper', 1, ?)",
                (T,))
    for i, side in ((1, "short"), (2, "long")):
        con.execute("INSERT INTO positions (id, account_id, symbol, side, qty, margin, entry_price, entry_time, "
                    "best_price, best_time, worst_price, worst_time, liq_price, fee, funding) "
                    "VALUES (?, 1, 'BTCUSDT', ?, 0.001, 5, 60000, ?, 60000, ?, 60000, ?, 70000, 0, 0)",
                    (i, side, T, T, T))
    for i in (1, 2):
        con.execute("INSERT INTO orders (id, account_id, number, symbol, side, type, reduce, qty, price, protection, "
                    "created_at) VALUES (?, 1, ?, 'BTCUSDT', 'short', 'limit', 0, 0.001, 61000, '[]', ?)", (i, i, T))
    con.commit()


@pytest.fixture
def repo(tmp_path):
    con = connect(tmp_path / "core.db")
    migrate(con, MIGRATIONS, tmp_path / "bak", catalog=catalog())
    _base(con)
    return NotesRepo(con)


def _entry(repo, **kw) -> int:
    args = dict(profile_id=1, account_id=1, symbol="BTCUSDT", side="short",
                reason={"karsi_kanit": "RSI düşük", "risk_beyani": {"yuzde": 2, "gerekce": "net yapı"}},
                invalid_price=62000.0, invalid_text="62000 üstünde kapanış", now=T)
    return repo.add_entry(**{**args, **kw})


def test_order_note_follows_the_order_to_its_position(repo):
    """Bekleyen girişin notu emir dolunca pozisyona geçer, Opus pozisyonun yanında aynı tezi görür."""
    nid = _entry(repo, order_id=1)
    assert repo.for_order(1).id == nid
    assert repo.for_position(2) == []
    repo.attach_position(1, 2)
    assert [n.id for n in repo.for_position(2)] == [nid]
    assert repo.for_position(2)[0].reason["risk_beyani"]["yuzde"] == 2


def test_two_entries_into_one_position_keep_both_notes_in_order(repo):
    """Aynı yönde ikinci giriş pozisyonla birleşir, iki tez de kaybolmadan sırayla görünür."""
    a = _entry(repo, position_id=1)
    b = _entry(repo, position_id=1, invalid_price=63000.0)
    assert [n.id for n in repo.for_position(1)] == [a, b]
    assert set(repo.for_positions([1, 2])) == {1}


def test_invalidation_is_handled_once(repo):
    """Aynı seviyeye ikinci değiş, emri ikinci kez iptal etmeye ya da Opus'u ikinci kez çağırmaya kalkmaz."""
    on_order = _entry(repo, order_id=1)
    on_pos = _entry(repo, position_id=1)
    found = repo.pending_invalidations(1, [1], [1])
    assert [n.id for n in found] == [on_order, on_pos]
    assert repo.mark_hit(on_order, T + 60, "cancelled") is True
    assert repo.mark_hit(on_order, T + 120, "cancelled") is False
    assert [n.id for n in repo.pending_invalidations(1, [1], [1])] == [on_pos]


def test_only_open_orders_and_positions_are_watched(repo):
    """Dolmuş emrin notu emir olarak değil pozisyon olarak izlenir, kapanmış pozisyonun notu hiç izlenmez."""
    nid = _entry(repo, order_id=1)
    repo.attach_position(1, 2)
    assert repo.pending_invalidations(1, [1], []) == []
    assert [n.id for n in repo.pending_invalidations(1, [], [2])] == [nid]
    assert repo.pending_invalidations(1, [], []) == []


def test_cancelled_since_lists_only_system_cancels_after_the_moment(repo):
    """Hesap bölümünde yalnızca son çağrıdan sonra geçersizlikten iptal edilen emirler yazar."""
    a = _entry(repo, order_id=1)
    b = _entry(repo, order_id=2)
    c = _entry(repo, position_id=1)
    repo.mark_hit(a, T + 10, "cancelled")
    repo.mark_hit(b, T + 100, "cancelled")
    repo.mark_hit(c, T + 100, "woke")
    assert [(n.id, t) for n, t in repo.cancelled_since(1, T + 50)] == [(b, T + 100)]


def test_review_is_written_once(repo):
    """Bir pozisyon bir kez sınıflandırılır, düzeltme çağrısındaki ikinci sınıflandırma ilkini ezmez."""
    repo.add_review(1, 1, None, "tez_yanlisti", "Yapı bozuldu.", T)
    repo.add_review(1, 1, None, "tez_dogruydu", "Yeniden.", T + 1)
    assert repo.reviewed_ids(1) == {1}
    assert repo.reviews([1, 2])[1]["verdict"] == "tez_yanlisti"
    assert repo.reviews([]) == {}


def test_migration_runs_on_an_empty_and_a_filled_database_and_fills_purpose(tmp_path):
    """Taşıma dolu veritabanında eski alarmları kuranına göre işaretler, yöneticinin alarmı Opus'unki sayılmaz."""
    old = tmp_path / "old"
    old.mkdir()
    for f in sorted(MIGRATIONS.glob("000[1-8]_*")):
        shutil.copy(f, old)
    con = connect(tmp_path / "core.db")
    migrate(con, old, tmp_path / "bak", catalog=catalog())
    _base(con)
    for n, actor in ((1, "strategy"), (2, "admin")):
        con.execute("INSERT INTO alerts (profile_id, number, symbol, price, below, actor, created_at) "
                    "VALUES (1, ?, 'BTCUSDT', 60000, 1, ?, ?)", (n, actor, T))
    con.commit()
    assert migrate(con, MIGRATIONS, tmp_path / "bak", catalog=catalog())[0] == "0009_decision_notes.sql"
    assert [(a.number, a.purpose) for a in StateRepo(con).alerts(1)] == [(1, "opus"), (2, "admin")]

    empty = connect(tmp_path / "empty.db")
    assert "0009_decision_notes.sql" in migrate(empty, MIGRATIONS, tmp_path / "bak2", catalog=catalog())


def test_alert_purpose_survives_a_round_trip(repo):
    """Sistemin kurduğu beklenti alarmı yeniden başlatmada Opus'un alarmına dönüşmez."""
    alerts = PriceAlerts()
    a = alerts.add("BTCUSDT", 60000.0, 61000.0, "beklenen fiyat", "system", T, purpose="expectation")
    b = alerts.add("BTCUSDT", 62000.0, 61000.0, "yönetici", "admin", T)
    state = StateRepo(repo.con)
    state.add_alert(1, a)
    state.add_alert(1, b)
    assert [x.purpose for x in state.alerts(1)] == ["expectation", "admin"]
