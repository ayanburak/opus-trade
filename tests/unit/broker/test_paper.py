"""
file: test_paper.py
importable: yok

Kâğıt broker'ın testleri. Bir mumun içindeki sıra, bir kapanışın fiyatı ya da
nakit hesabı yanlış olursa hesap sessizce yanlış kâr ya da zarar yazar ve
hiçbir hata görünmez. Testler eşleştiricinin temkinli sırasını, maker ve
taker ayrımını, görünen numaranın kurallarını ve yarım kalan komutun deftere
geçmemesini korur. Rakamlar SOL'un kuralıyla elle hesaplandı: adım 0.01,
kayma yüzde 0,02, bakım marjı yüzde 1.

description: broker.paper için birim testleri.
"""

import asyncio

import pytest

from opus_trade.broker.paper import PaperBroker
from opus_trade.domain.catalog import Catalog
from opus_trade.domain.errors import RuleError
from opus_trade.domain.fees import BINANCE_STANDARD as FEES
from opus_trade.domain.models import Side, TpLevel
from opus_trade.domain.trading import (Actor, CloseAll, ClosePosition, EventKind, OpenPosition,
                                       PlaceOrder, Ref)
from opus_trade.storage.database import connect
from opus_trade.storage.ledger import MIGRATIONS, Ledger
from opus_trade.storage.migrate import migrate
from tests.fakes import FakeClock

T = 1_795_000_000
SOL = "SOLUSDT"
LONG, SHORT = Side.LONG, Side.SHORT


@pytest.fixture
def broker(tmp_path) -> PaperBroker:
    con = connect(tmp_path / "core.db")
    migrate(con, MIGRATIONS, tmp_path / "bak")
    ledger = Ledger(con)
    b = PaperBroker(ledger, ledger.create_account("paper", 100.0, T), Catalog(), FEES, FakeClock(T))
    b.on_price(SOL, 100, 100, 100, T)
    return b


def run(b: PaperBroker, cmd, actor=Actor.STRATEGY):
    return asyncio.run(b.execute(cmd, actor))


def open_long(b, **kw):
    return run(b, OpenPosition(SOL, LONG, leverage=kw.pop("leverage", 10), margin=kw.pop("margin", 10), **kw))[0]


# --- Numaralar ---

def test_numbers_are_reused_and_an_order_keeps_its_number_when_filled(broker):
    """Görünen numara açık kayıtların kullanmadığı en küçük sayıdır, dolan emir pozisyona numarasıyla geçer."""
    assert open_long(broker).number == 1
    assert run(broker, PlaceOrder(SOL, LONG, trigger=95, leverage=10, margin=10))[0].number == 2
    run(broker, ClosePosition(Ref(1, SOL, LONG)))
    assert open_long(broker).number == 1
    filled = broker.on_price(SOL, 100, 94.9, 95, T + 60)
    assert [(e.kind, e.number) for e in filled] == [(EventKind.ORDER_FILLED, 2)]
    assert sorted(p.number for p in broker.state().positions) == [1, 2]


def test_stale_number_is_rejected(broker):
    """Numara arada başka bir kayda geçtiyse komut yanlış pozisyona dokunmak yerine reddedilir."""
    open_long(broker)
    with pytest.raises(RuleError, match="artık SOLUSDT short değil"):
        run(broker, ClosePosition(Ref(1, SOL, SHORT)))
    assert len(broker.state().positions) == 1


def test_command_before_first_price_is_refused(broker):
    """Açılışın ilk saniyelerinde fiyatı gelmemiş bir paritede piyasa komutu hata değil anlaşılır bir ret almalı."""
    with pytest.raises(RuleError, match="fiyatı henüz gelmedi"):
        run(broker, OpenPosition("ETHUSDT", LONG, leverage=10, margin=30))


# --- Eşleştiricinin sırası ---

def test_sl_wins_when_sl_and_tp_are_both_inside_one_candle(broker):
    """Mumun içinde fiyatın önce hangi uca gittiği bilinmez. Temkinli sıra zarar tarafını önce sayar."""
    open_long(broker, tp_levels=(TpLevel(105, 100),), sl=98)
    events = broker.on_price(SOL, 106, 97, 100, T + 60)
    assert [e.reason for e in events] == ["sl"]
    assert events[0].trade.exit_price == pytest.approx(98 * (1 - 0.0002))


def test_liquidation_comes_before_an_sl_placed_beyond_it(broker):
    """Likidasyonun ötesine konmuş SL hiç çalışmaz. Pozisyon likide olur ve marjinin tamamı gider."""
    open_long(broker, leverage=50, sl=95)
    pos = broker.state().positions[0]
    cash_before = broker.state().cash
    events = broker.on_price(SOL, 100, 94, 94, T + 60)
    assert events[0].reason == "liquidation"
    assert events[0].trade.exit_price == pytest.approx(pos.liq_price)
    assert events[0].trade.pnl == pytest.approx(-pos.margin)
    assert broker.state().cash == pytest.approx(cash_before)


def test_tp_levels_fill_at_their_price_as_maker(broker):
    """TP kademesi limit emirdir, tam seviyesinden kaymasız ve maker komisyonuyla dolar."""
    open_long(broker, tp_levels=(TpLevel(103, 50), TpLevel(106, 50)))
    events = broker.on_price(SOL, 107, 100, 106, T + 60)
    trades = [e.trade for e in events]
    assert [(t.qty, t.exit_price) for t in trades] == [(0.49, 103), (0.5, 106)]
    assert trades[1].fee == pytest.approx(0.5 * 100.02 * FEES.taker + 0.5 * 106 * FEES.maker)
    assert broker.state().positions == ()


def test_closed_trade_best_price_includes_its_exit(broker):
    """TP ile kapanan işlemin en iyi fiyatı TP'den kötü görünürse çıkış karnesi yanlış ders çıkarır."""
    open_long(broker, tp_levels=(TpLevel(106, 100),))
    trade = broker.on_price(SOL, 107, 100, 106, T + 60)[0].trade
    assert trade.best_price == 106


def test_trailing_stop_does_not_trigger_in_the_candle_that_raises_it(broker):
    """Aynı mumdaki yükseliş kendi stopunu tetikleyemez, iz bir sonraki fiyatla çalışır."""
    open_long(broker, trail_pct=1)
    assert broker.on_price(SOL, 110, 100.5, 109, T + 60) == []
    assert broker.state().positions[0].sl == pytest.approx(108.9)
    events = broker.on_price(SOL, 109.5, 108.5, 108.6, T + 120)
    assert events[0].reason == "sl"
    assert events[0].trade.exit_price == pytest.approx(108.9 * (1 - 0.0002))


# --- Emirler ---

def test_order_coming_to_price_is_maker_and_breakout_is_taker(broker):
    """Lehe gelen tetik kaymasız dolar, kırılım tetiği stop emri gibi kayma yer."""
    run(broker, PlaceOrder(SOL, LONG, trigger=95, leverage=10, margin=10))
    run(broker, PlaceOrder(SOL, LONG, trigger=105, leverage=10, margin=10))
    broker.on_price(SOL, 100, 94.9, 95, T + 60)
    broker.on_price(SOL, 105.1, 100, 105, T + 120)
    entries = sorted(p.entry_price for p in broker.state().positions)
    assert entries == [95, pytest.approx(105 * 1.0002)]


def test_order_that_cannot_open_at_trigger_fails_and_leaves_the_book(broker):
    """Tetik anında nakit yetmiyorsa emir başarısız olur ve bekleyenlerden çıkar, sonsuza kadar tekrar denenmez."""
    run(broker, PlaceOrder(SOL, LONG, trigger=95, leverage=10, margin=60))
    open_long(broker, margin=60)
    events = broker.on_price(SOL, 100, 94.9, 95, T + 60)
    assert events[0].kind is EventKind.ORDER_FAILED and "Yetersiz nakit" in events[0].reason
    assert broker.state().orders == ()


def test_expired_order_is_removed_before_it_can_fill(broker):
    """Süresi dolmuş bir emir, süresi dolduktan sonra gelen fiyatla dolmamalı."""
    run(broker, PlaceOrder(SOL, LONG, trigger=95, leverage=10, margin=10, valid_min=1))
    events = broker.on_price(SOL, 100, 94, 95, T + 61)
    assert [e.kind for e in events] == [EventKind.ORDER_EXPIRED]
    assert broker.state().positions == ()


# --- Fonlama ---

def test_funding_moves_margin_and_can_liquidate(broker):
    """Fonlama marjinden düşer. Marjini tüketen fonlama pozisyonu likide eder."""
    open_long(broker)
    pos = broker.state().positions[0]
    event = broker.on_funding(SOL, 0.001, 100, T + 60)[0]
    assert event.amount == pytest.approx(pos.qty * 100 * 0.001)
    assert broker.state().positions[0].margin == pytest.approx(pos.margin - event.amount)
    event = broker.on_funding(SOL, 1.0, 100, T + 120)[0]
    assert event.reason == "funding_liquidation"
    assert broker.state().positions == ()


# --- Defter ---

def test_cash_after_round_trip_equals_start_plus_net(broker):
    """Nakit, başlangıç bakiyesi ile kapanan işlemlerin net sonucunun toplamıdır. Fark varsa para yoktan var olur ya da kaybolur."""
    open_long(broker)
    trade = run(broker, ClosePosition(Ref(1, SOL, LONG)))[0].trade
    assert broker.state().cash == pytest.approx(100 + trade.net)


def test_failed_command_leaves_no_partial_change(broker):
    """Hepsini kapat ikinci pozisyonda takılırsa birinci de kapanmamış olmalı, defter yarım bir halde kalmaz."""
    broker.on_price("ETHUSDT", 2000, 2000, 2000, T)
    open_long(broker)
    run(broker, OpenPosition("ETHUSDT", LONG, leverage=10, margin=30))
    cash = broker.state().cash
    del broker.prices["ETHUSDT"]
    with pytest.raises(RuleError):
        run(broker, CloseAll())
    assert len(broker.state().positions) == 2
    assert broker.state().cash == cash
