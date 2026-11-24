"""
file: test_paper.py
importable: yok

Kâğıt broker'ın testleri. Kâğıt hesap canlıya geçildiğinde aynı sonucu
vermeli. Bir dolum fiyatı, bir marjin ayırma ya da bir kapanış yanlış olursa
hesap sessizce yanlış kâr ya da zarar yazar ve hiçbir hata görünmez. Testler
Binance'in hedge modu modelini korur: birleşme, emir türleri, miktarlı
koruma, eşleştirme, likidasyon ve kaldıraç. Rakamlar SOL'un kuralıyla elle
hesaplandı: adım 0.01, tick 0.01, kayma yüzde 0,02, taker binde 0,5, maker
binde 0,2, ilk kademede bakım marjı binde 5, likidasyon ücreti binde 15,
varsayılan kaldıraç 20.

description: broker.paper için birim testleri.
"""

import asyncio

import pytest

from opus_trade.broker.paper import PaperBroker
from opus_trade.domain import rules
from opus_trade.domain.errors import RuleError
from opus_trade.domain.fees import BINANCE_STANDARD as FEES
from opus_trade.domain.models import Side
from opus_trade.domain.trading import (Actor, AmendOrder, CancelOrder, CloseAll, ClosePosition, EventKind, Order,
                                       OrderType, PlaceOrder, Protection, Ref, SetLeverage)
from opus_trade.storage.database import connect
from opus_trade.storage.ledger import MIGRATIONS, Ledger
from opus_trade.storage.migrate import migrate
from tests.fakes import FakeClock, catalog

T = 1_795_000_000
SOL = "SOLUSDT"
SPEC = catalog().get(SOL)
LONG, SHORT = Side.LONG, Side.SHORT
MARKET, LIMIT, STOP, TP, TRAIL = (OrderType.MARKET, OrderType.LIMIT, OrderType.STOP, OrderType.TAKE_PROFIT,
                                  OrderType.TRAILING_STOP)
SLIP = SPEC.slippage


@pytest.fixture
def broker(tmp_path) -> PaperBroker:
    con = connect(tmp_path / "core.db")
    migrate(con, MIGRATIONS, tmp_path / "bak", catalog=catalog())
    ledger = Ledger(con)
    b = PaperBroker(ledger, ledger.create_account("paper", 1000.0, T), catalog(), FEES, FakeClock(T))
    price(b, 100)
    mark(b, 100)
    return b


def run(b: PaperBroker, cmd, actor: Actor = Actor.ADMIN):
    return asyncio.run(b.execute(cmd, actor))


def price(b: PaperBroker, last: float, high: float | None = None, low: float | None = None,
          open_: float | None = None):
    return b.on_price(SOL, open_ if open_ is not None else last, max(high or last, last), min(low or last, last),
                      last, T)


def mark(b: PaperBroker, m: float, high: float | None = None, low: float | None = None, fill: float | None = None):
    return b.on_mark(SOL, max(high or m, m), min(low or m, m), m, T, fill)


def buy(qty: float, type_: OrderType = MARKET, price_: float | None = None, side: Side = LONG, **kw) -> PlaceOrder:
    return PlaceOrder(SOL, side, type_, qty=qty, price=price_, **kw)


def pos(b: PaperBroker, side: Side = LONG):
    return b.state().position(SOL, side)


def exits(b: PaperBroker):
    return [o for o in b.state().orders if o.reduce]


def kinds(events) -> list[EventKind]:
    return [e.kind for e in events]


# --- Pozisyon ve birleşme ---

def test_same_side_entries_merge_into_one_position(broker):
    """Hedge modunda parite ve yön başına tek pozisyon olur. İkinci giriş birleşir, giriş miktarla ağırlıklı
    ortalanır, marjin ve komisyon toplanır."""
    assert kinds(run(broker, buy(1))) == [EventKind.OPENED]
    price(broker, 110)
    assert kinds(run(broker, buy(1))) == [EventKind.ADDED]
    p = pos(broker)
    first, second = 100 * (1 + SLIP), 110 * (1 + SLIP)
    assert (p.qty, p.entry_price) == (2, pytest.approx((first + second) / 2))
    assert p.margin == pytest.approx((first + second) / 20)
    assert p.fee == pytest.approx((first + second) * FEES.taker)
    assert p.liq_price == pytest.approx(rules.liq_price(SPEC, LONG, p.entry_price, 2, p.margin))
    assert len(broker.state().positions) == 1


def test_long_and_short_live_side_by_side(broker):
    """Hedge modunda aynı paritede long ve short ayrı pozisyondur. Biri diğerini kapatmaz."""
    run(broker, buy(1))
    run(broker, buy(2, side=SHORT))
    assert (pos(broker).qty, pos(broker, SHORT).qty) == (1, 2)


def test_command_before_first_price_is_refused(broker):
    """Fiyatı henüz gelmeyen paritede emir eski bir fiyattan açılmamalı."""
    with pytest.raises(RuleError, match="ETHUSDT fiyatı henüz gelmedi"):
        run(broker, PlaceOrder("ETHUSDT", LONG, MARKET, qty=0.1))


def test_cash_after_round_trip_equals_start_plus_net(broker):
    """Açılıp kapanan işlemden sonra nakit, başlangıç artı işlemin net sonucudur. Kuruş kaçarsa hesap kayar."""
    run(broker, buy(2))
    price(broker, 105)
    run(broker, ClosePosition(SOL, LONG))
    t = broker.ledger.trades(broker.account_id)[0]
    assert broker.state().cash == pytest.approx(1000 + t.net)


def test_failed_command_leaves_no_partial_change(broker):
    """Reddedilen komut deftere hiçbir şey yazmaz, yarım bir giriş kalmaz."""
    bad = buy(1, protection=(Protection(TP, price=99),))
    with pytest.raises(RuleError):
        run(broker, bad)
    s = broker.state()
    assert (s.cash, s.positions, s.orders) == (1000, (), ())


# --- Giriş emirleri ---

def test_market_entry_needs_available_balance(broker):
    """Marjin ve komisyon kullanılabilir bakiyeyi aşarsa giriş reddedilir. Miktar kendiliğinden küçülmez."""
    with pytest.raises(RuleError, match="Yetersiz kullanılabilir bakiye"):
        run(broker, buy(200))


def test_breakout_and_pullback_entries_in_both_directions(broker):
    """Fiyatı bekleyen giriş iki yönde de olur. Alış stopu fiyat yükselince, alış take-profit'i düşünce
    tetiklenir. Satışta tersi. Yön karışırsa emir yanlış anda açılır."""
    run(broker, buy(1, STOP, 105))
    run(broker, buy(1, STOP, 95, side=SHORT))
    run(broker, buy(1, TP, 103, side=SHORT))
    price(broker, 102, high=102.9, low=96)
    assert broker.state().positions == ()
    price(broker, 105, high=105, open_=102)
    assert pos(broker).entry_price == pytest.approx(105 * (1 + SLIP))
    assert pos(broker, SHORT).entry_price == pytest.approx(103 * (1 - SLIP))
    run(broker, buy(1, TP, 99))
    price(broker, 95, low=95)
    assert (pos(broker).qty, pos(broker, SHORT).qty) == (2, 2)


def test_trigger_already_passed_is_refused(broker):
    """Tetiği geçilmiş stop ya da take-profit Binance'te hemen tetiklenirdi diye reddedilir."""
    with pytest.raises(RuleError, match="hemen tetiklenirdi"):
        run(broker, buy(1, STOP, 95))
    with pytest.raises(RuleError, match="hemen tetiklenirdi"):
        run(broker, buy(1, TP, 105))


def test_sl_trigger_is_checked_against_mark_and_tp_against_last(broker):
    """SL mark fiyatıyla, TP son fiyatla tetiklenir, kurulurken de kendi fiyatına göre denetlenir. Mark ile son
    fiyat ayrıştığında yanlış fiyata bakan denetim hemen tetiklenecek bir SL'yi kabul eder ya da geçerli bir TP'yi
    reddeder."""
    run(broker, buy(1))
    mark(broker, 98)
    with pytest.raises(RuleError, match="SL tetiği 99,00, mark fiyatı 98,00"):
        run(broker, buy(None, STOP, 99, reduce=True))
    with pytest.raises(RuleError, match="TP tetiği 99,50, son fiyat 100,00"):
        run(broker, buy(None, TP, 99.5, reduce=True))
    run(broker, buy(None, STOP, 97.5, reduce=True))


def test_limit_on_the_wrong_side_fills_at_once_as_taker(broker):
    """Fiyatın üstündeki alış limiti karşı tarafla hemen eşleşir, taker öder ve kayma yer. Maker sayılsa ucuz
    dolardı."""
    events = run(broker, buy(1, LIMIT, 101))
    assert kinds(events) == [EventKind.OPENED]
    p = pos(broker)
    assert p.entry_price == pytest.approx(100 * (1 + SLIP))
    assert p.fee == pytest.approx(p.entry_price * FEES.taker)


def test_limit_fills_at_its_level_only_after_price_passes_it_by_a_tick(broker):
    """Defterdeki limit seviyeye yalnızca dokunan fiyatla dolmaz, bir tick geçince seviyesinden maker dolar."""
    run(broker, buy(1, LIMIT, 99))
    price(broker, 99.5, low=99)
    assert broker.state().positions == ()
    price(broker, 99.5, low=98.99)
    p = pos(broker)
    assert (p.entry_price, p.fee) == (99, pytest.approx(99 * FEES.maker))


def test_limit_reserves_its_margin_when_placed(broker):
    """Limit emir verilirken marjini kullanılabilir bakiyeden ayrılır. Ayrılmasa aynı para iki emre birden
    yeterdi. Nakit ancak dolumda değişir."""
    run(broker, buy(100, LIMIT, 99))
    s = broker.state()
    assert s.cash == 1000 and s.available == pytest.approx(1000 - 100 * 99 / 20)
    with pytest.raises(RuleError, match="Yetersiz kullanılabilir bakiye"):
        run(broker, buy(110, LIMIT, 98))


def test_limit_above_mark_also_reserves_the_open_loss(broker):
    """Mark'ın üstündeki alış daha dolarken zararda başlar, Binance farkı da ayırır."""
    price(broker, 102)
    run(broker, buy(10, LIMIT, 101))
    assert broker.state().available == pytest.approx(1000 - 10 * 101 / 20 - 10 * 1)


def test_stop_entry_reserves_nothing_until_it_triggers(broker):
    """Tetikli emir Binance'te defterde beklemez ve marjin ayırmaz."""
    run(broker, buy(100, STOP, 105))
    assert broker.state().available == 1000


def test_limit_price_stays_within_the_band_of_mark(broker):
    """Limit fiyatı mark fiyatından uzak olamaz. Bant dışı alış limiti hemen dolacak olsa da reddedilir."""
    with pytest.raises(RuleError, match="alış limiti en fazla"):
        run(broker, buy(1, LIMIT, 100 * SPEC.price_up + 1))


def test_order_fill_fails_when_cash_is_gone(broker):
    """Tetikli emir dolarken bakiye yetmiyorsa emir başarısız olur ve defterden düşer."""
    run(broker, buy(150, STOP, 105))
    run(broker, buy(50))
    events = price(broker, 105, high=105)
    assert kinds(events) == [EventKind.ORDER_FAILED]
    assert "Yetersiz" in events[0].reason and not [o for o in broker.state().orders if not o.reduce]


def test_expired_order_is_removed_before_it_can_fill(broker):
    """Süresi dolan emir aynı fiyat kontrolünde dolmadan önce düşer."""
    run(broker, buy(1, STOP, 105, valid_min=1))
    broker.clock.t = T + 60
    events = broker.on_price(SOL, 100, 106, 100, 106, T + 60)
    assert kinds(events) == [EventKind.ORDER_EXPIRED] and broker.state().positions == ()


def test_order_count_limit_is_enforced(broker):
    """Paritenin açık emir sınırını Binance zorlar, kâğıt hesap da aşamaz."""
    for i in range(SPEC.max_orders):
        broker.ledger.insert_order(Order(0, broker.account_id, i + 1, SOL, LONG, STOP, False, 1, 105 + i, None,
                                         None, 0, (), None, T))
    with pytest.raises(RuleError, match="en fazla 200 bekleyen emir"):
        run(broker, buy(1, STOP, 105))


def test_stale_number_is_rejected(broker):
    """Numara arada başka bir emre geçtiyse komut eski numarayla yanlış emre dokunmamalı."""
    run(broker, buy(1, STOP, 105))
    with pytest.raises(RuleError, match="artık SOLUSDT short değil"):
        run(broker, CancelOrder(Ref(1, SOL, SHORT)))
    run(broker, CancelOrder(Ref(1, SOL, LONG)))
    assert broker.state().orders == ()


# --- Fiyat değiştirme ---

def test_amend_moves_a_limit_and_its_reservation(broker):
    """Limit emrin fiyatı tek adımda taşınır, numarası kalır, ayırdığı tutar yeni fiyata göre değişir."""
    run(broker, buy(10, LIMIT, 95))
    events = run(broker, AmendOrder(Ref(1, SOL, LONG), 97))
    o = broker.state().order(1)
    assert kinds(events) == [EventKind.ORDER_AMENDED] and o.price == 97
    assert broker.state().available == pytest.approx(1000 - 10 * 97 / 20)


def test_amend_across_the_price_fills_at_once(broker):
    """Fiyatın üstüne taşınan alış limiti hemen taker dolar."""
    run(broker, buy(1, LIMIT, 95))
    run(broker, AmendOrder(Ref(1, SOL, LONG), 101))
    assert pos(broker).entry_price == pytest.approx(100 * (1 + SLIP))
    assert [o for o in broker.state().orders if not o.reduce] == []


def test_only_limit_orders_can_be_amended(broker):
    """Binance'te yalnızca limit emir değiştirilir. Stop iptal edilip yeniden verilir."""
    run(broker, buy(1, STOP, 105))
    with pytest.raises(RuleError, match="Yalnızca limit"):
        run(broker, AmendOrder(Ref(1, SOL, LONG), 106))


# --- Koruma ---

def test_entry_protection_is_armed_with_the_entry_qty(broker):
    """Girişin koruma istekleri dolumda o girişin miktarıyla kapatan emir olur. Miktarlı TP kendi miktarını,
    miktarsız SL girişin tamamını taşır."""
    run(broker, buy(1))
    events = run(broker, buy(2, protection=(Protection(TP, price=110, qty=1), Protection(STOP, price=90))))
    assert kinds(events) == [EventKind.ADDED, EventKind.ORDER_PLACED, EventKind.ORDER_PLACED]
    assert {(o.type, o.price, o.qty) for o in exits(broker)} == {(TP, 110, 1), (STOP, 90, 2)}


def test_pending_entry_protection_waits_for_the_fill(broker):
    """Bekleyen girişin koruması emirde istek olarak durur, emir dolmadan kapatan emir kurulmaz."""
    run(broker, buy(1, LIMIT, 99, protection=(Protection(STOP, price=95),)))
    assert exits(broker) == []
    price(broker, 99, low=98.9)
    assert [(o.type, o.price, o.qty) for o in exits(broker)] == [(STOP, 95, 1)]


def test_protection_on_the_wrong_side_is_refused_with_the_entry(broker):
    """Dolumda kurulamayacak bir koruma istek anında reddedilir, giriş yarım korumayla açılmaz."""
    with pytest.raises(RuleError, match="hemen tetiklenirdi"):
        run(broker, buy(1, LIMIT, 99, protection=(Protection(STOP, price=99.5),)))


def test_protection_passed_by_a_gap_fails_but_the_entry_stays(broker):
    """Fiyat dolumla koruma arasında seviyeyi geçtiyse Binance koruma emrini reddeder ve pozisyon korumasız
    kalır. Kâğıt hesap da aynısını yapar ve olayla söyler."""
    run(broker, buy(1, STOP, 105, protection=(Protection(TP, price=106),)))
    events = price(broker, 107, high=107, open_=107)
    assert EventKind.ORDER_FAILED in kinds(events)
    assert pos(broker).qty == 1 and exits(broker) == []


def test_tp_closes_its_qty_and_sl_closes_the_rest(broker):
    """Miktarlı TP yalnızca kendi miktarını kapatır, miktarsız SL tetiklenince kalanın tamamını."""
    run(broker, buy(2, protection=(Protection(TP, price=110, qty=1), Protection(STOP, price=90))))
    price(broker, 110, high=110)
    assert pos(broker).qty == 1
    mark(broker, 89, fill=89)
    assert pos(broker) is None


def test_exit_larger_than_the_rest_closes_the_rest(broker):
    """Kısmi kapanıştan sonra kalandan büyük kalan koruma tetiklenince kalanı kapatır, fazlası yoktur."""
    run(broker, buy(2, protection=(Protection(STOP, price=90, qty=2),)))
    run(broker, ClosePosition(SOL, LONG, qty=1.5))
    mark(broker, 89, fill=89)
    assert pos(broker) is None
    assert broker.ledger.trades(broker.account_id)[-1].qty == 0.5


def test_exits_are_cancelled_when_the_position_closes(broker):
    """Pozisyon tamamen kapanınca ona bağlı kapatan emirler iptal edilir. Kalsalar sonraki pozisyonu
    yanlış miktarla kapatabilirlerdi."""
    run(broker, buy(1, protection=(Protection(TP, price=110), Protection(STOP, price=90))))
    events = run(broker, ClosePosition(SOL, LONG))
    assert kinds(events) == [EventKind.CLOSED, EventKind.ORDER_CANCELLED, EventKind.ORDER_CANCELLED]
    assert broker.state().orders == ()


def test_exit_needs_an_open_position(broker):
    """Hedge modunda kapatan emir yalnızca açık pozisyona konur."""
    with pytest.raises(RuleError, match="Açık SOLUSDT long pozisyonu yok"):
        run(broker, buy(1, STOP, 90, reduce=True))


def test_closed_trade_remembers_its_guards(broker):
    """İşlem kaydı kapanış anındaki TP fiyatlarını ve en yakın stopu taşır, çıkış karnesi bunları ölçer."""
    run(broker, buy(2, protection=(Protection(TP, price=120, qty=1), Protection(TP, price=110, qty=1),
                                   Protection(STOP, price=90), Protection(STOP, price=92, qty=1))))
    run(broker, ClosePosition(SOL, LONG))
    t = broker.ledger.trades(broker.account_id)[0]
    assert (t.tp_prices, t.sl) == ((110, 120), 92)


# --- Eşleştirme ---

def test_tp_is_a_market_order_with_slippage(broker):
    """TP take-profit-market emridir, taker öder ve kayma yer."""
    run(broker, buy(1, protection=(Protection(TP, price=110),)))
    price(broker, 110, high=110, open_=109)
    t = broker.ledger.trades(broker.account_id)[0]
    assert t.exit_price == pytest.approx(110 * (1 - SLIP)) and t.reason == "tp"


def test_pin_fills_the_tp_at_its_level(broker):
    """Fiyat TP'ye iğne gibi değip aynı aralıkta geri dönse de TP seviyesinden dolar. Binance'te piyasa emri
    tetiklendiği an gider, sonradan geri dönen fiyat dolumu değiştirmez. İğnenin ucundan dolduğu varsayılsa kâr
    şişerdi, aralığın sonundan dolduğu varsayılsa eksilirdi."""
    run(broker, buy(1, protection=(Protection(TP, price=110),)))
    price(broker, 109, high=111, low=108, open_=108)
    t = broker.ledger.trades(broker.account_id)[0]
    assert t.exit_price == pytest.approx(110 * (1 - SLIP))


def test_pin_fills_a_triggered_entry_at_its_level(broker):
    """Tetikli giriş de iğneyle tetiklenince seviyesinden dolar, fiyat aynı aralıkta geri dönse de."""
    run(broker, buy(1, STOP, 105))
    price(broker, 103, high=106, low=102, open_=102)
    assert pos(broker).entry_price == pytest.approx(105 * (1 + SLIP))


def test_gap_beyond_the_trigger_fills_from_the_open(broker):
    """Aralık tetiğin ötesinde açıldıysa dolum tetikten değil açılıştan başlar."""
    run(broker, buy(1, STOP, 105))
    price(broker, 106, high=107, low=106, open_=107)
    assert pos(broker).entry_price == pytest.approx(107 * (1 + SLIP))


def test_sl_fills_at_the_last_price_of_its_moment(broker):
    """SL mark fiyatıyla tetiklenir, o anın son fiyatından kaymayla dolar. Son fiyat bilinmiyorsa seviyesinden."""
    run(broker, SetLeverage(SOL, 5))
    run(broker, buy(1, protection=(Protection(STOP, price=90),)))
    mark(broker, 89.5, fill=88)
    assert broker.ledger.trades(broker.account_id)[0].exit_price == pytest.approx(88 * (1 - SLIP))


def test_last_price_spike_does_not_trigger_sl(broker):
    """SL son fiyata değil mark fiyatına bakar, son fiyattaki iğne SL'yi çalıştırmaz."""
    run(broker, buy(1, protection=(Protection(STOP, price=90),)))
    price(broker, 100, low=85)
    assert pos(broker).qty == 1


def test_trailing_stop_starts_from_its_placement(broker):
    """İz Binance'teki gibi konduğu andan beri görülen en iyi mark fiyatından hesaplanır, pozisyonun daha önce
    gördüğü tepeden değil."""
    run(broker, buy(1))
    mark(broker, 120)
    mark(broker, 110)
    run(broker, buy(None, TRAIL, reduce=True, trail_pct=5))
    mark(broker, 105)
    assert pos(broker) is not None
    mark(broker, 104, fill=104)
    assert pos(broker) is None


def test_trailing_stop_does_not_trigger_in_the_range_that_raises_it(broker):
    """Aynı aralık hem izi yükseltip hem tetiklemez, sırası bilinmez ve temkinli olan tetiklememektir."""
    run(broker, buy(1))
    run(broker, buy(None, TRAIL, reduce=True, trail_pct=5))
    mark(broker, 110, high=120, low=110)
    assert pos(broker) is not None
    assert exits(broker)[0].best == 120
    mark(broker, 113, low=113)
    assert pos(broker) is None


# --- Likidasyon ve fonlama ---

def test_liquidation_takes_the_binance_fee_from_what_is_left(broker):
    """Likidasyonda pozisyon likidasyon fiyatından kapanır ve kalan marjinden Binance'in likidasyon ücreti alınır.
    Ücret kalan marjini aşamaz, kayıp marjinle sınırlıdır."""
    run(broker, buy(10))
    p = pos(broker)
    mark(broker, p.liq_price - 1, low=p.liq_price - 1)
    t = broker.ledger.trades(broker.account_id)[0]
    left = p.margin + rules.unrealized_pnl(LONG, p.entry_price, 10, p.liq_price)
    fee = min(rules.liq_fee(SPEC, 10, p.liq_price), left)
    assert t.reason == "liquidation" and t.exit_price == pytest.approx(p.liq_price)
    assert t.fee == pytest.approx(p.fee + fee)
    assert broker.state().cash == pytest.approx(1000 - p.margin - p.fee + left - fee)
    assert 1000 - broker.state().cash <= p.margin + p.fee + 1e-9


def test_liquidation_comes_before_an_sl_placed_beyond_it(broker):
    """Likidasyonun ötesindeki SL'ye fiyat varmadan pozisyon likide olur."""
    run(broker, buy(10))
    p = pos(broker)
    run(broker, buy(None, STOP, round(p.liq_price - 1, 2), reduce=True))
    mark(broker, p.liq_price - 2, low=p.liq_price - 2)
    assert broker.ledger.trades(broker.account_id)[0].reason == "liquidation"


def test_funding_moves_margin_and_can_wipe_it(broker):
    """Fonlama marjinden düşer ve likidasyonu yaklaştırır. Marjini tüketen fonlama pozisyonu marjinin tamamıyla
    kapatır."""
    run(broker, buy(10))
    before = pos(broker)
    broker.on_funding(SOL, 0.001, 100, T)
    after = pos(broker)
    assert after.margin == pytest.approx(before.margin - 1) and after.liq_price > before.liq_price
    events = broker.on_funding(SOL, 1, 100, T)
    assert events[0].reason == "funding_liquidation" and pos(broker) is None


def test_equity_uses_the_mark_price(broker):
    """Özkaynak Binance'teki gibi mark fiyatıyla hesaplanır, son fiyattaki iğne onu oynatmaz."""
    run(broker, buy(1))
    price(broker, 150)
    mark(broker, 110)
    p = pos(broker)
    assert broker.state().equity == pytest.approx(broker.state().cash + p.margin + 110 - p.entry_price)


# --- Kaldıraç ---

def test_leverage_is_the_symbols_setting(broker):
    """Kaldıraç paritenin ayarıdır, sonraki girişin marjinini belirler. Üst sınır ilk kademedir."""
    run(broker, SetLeverage(SOL, 10))
    run(broker, buy(1))
    assert pos(broker).margin == pytest.approx(100 * (1 + SLIP) / 10)
    with pytest.raises(RuleError, match="1 ile 100 arasında"):
        run(broker, SetLeverage(SOL, 101))


def test_leverage_cannot_drop_with_an_open_position(broker):
    """Açık pozisyon varken kaldıraç düşmez, yükselir. Yükseltme açık pozisyonun marjinine dokunmaz."""
    run(broker, buy(1))
    margin = pos(broker).margin
    with pytest.raises(RuleError, match="düşürülemez"):
        run(broker, SetLeverage(SOL, 10))
    run(broker, SetLeverage(SOL, 50))
    assert pos(broker).margin == margin


def test_high_leverage_caps_the_position_size(broker):
    """Kaldıraç büyüdükçe izin verilen pozisyon küçülür. Sınırı aşan giriş reddedilir."""
    run(broker, SetLeverage(SOL, 100))
    broker.ledger.set_cash(broker.account_id, 10_000)
    with pytest.raises(RuleError, match="100x kaldıraçla pozisyon en fazla 50000 dolar"):
        run(broker, buy(600))


# --- Önizleme ---

def test_entry_preview_matches_the_real_entry_and_writes_nothing(broker):
    """Panelin gördüğü tahmin gerçek girişle aynı olmalı ve hesaba hiçbir şey yazmamalı."""
    run(broker, buy(1))
    cmd = buy(2)
    pv = broker.preview(cmd)
    assert pos(broker).qty == 1
    run(broker, cmd)
    p = pos(broker)
    assert (pv.qty, pv.avg_entry, pv.liq_price) == (2, pytest.approx(p.entry_price), pytest.approx(p.liq_price))
    assert pv.cash_after == pytest.approx(broker.state().available)


def test_wrong_side_limit_preview_is_the_taker_fill(broker):
    """Fiyatın yanlış tarafındaki limit hemen taker dolar. Önizleme de dolum fiyatını ve taker komisyonunu vermeli.

    Önizleme bu emri defterde bekleyen maker emir sanarsa Onay kartı daha ucuz bir komisyon gösterirdi.
    """
    cmd = buy(1, LIMIT, 101)
    pv = broker.preview(cmd)
    run(broker, cmd)
    p = pos(broker)
    assert (pv.entry, pv.fee) == (pytest.approx(p.entry_price), pytest.approx(p.fee))
    assert pv.cash_after == pytest.approx(broker.state().available)


def test_close_preview_matches_the_real_close_and_writes_nothing(broker):
    """Kapatma kartının tahmini gerçek kapanışla aynı olmalı."""
    run(broker, buy(2))
    price(broker, 105)
    cmd = ClosePosition(SOL, LONG, qty=1)
    pv = broker.preview_close(cmd)
    assert pos(broker).qty == 2
    run(broker, cmd)
    t = broker.ledger.trades(broker.account_id)[0]
    assert (pv.exit, pv.net, pv.remaining) == (pytest.approx(t.exit_price), pytest.approx(t.net), 1)


def test_close_all_closes_and_cancels_everything(broker):
    """Acil durdurma bütün pozisyonları kapatır ve bütün emirleri iptal eder."""
    run(broker, buy(1, protection=(Protection(STOP, price=90),)))
    run(broker, buy(1, STOP, 105, side=LONG))
    run(broker, buy(1, side=SHORT))
    run(broker, CloseAll())
    s = broker.state()
    assert (s.positions, s.orders) == ((), ())


def _guarded(broker):
    run(broker, buy(3, protection=(Protection(TP, price=110, qty=1), Protection(TP, price=120),
                                   Protection(STOP, price=97, qty=2), Protection(TRAIL, trail_pct=2, qty=1))))
    return {o.price or o.trail_pct: o.id for o in exits(broker)}


def test_outlook_predicts_the_tp_closes(broker):
    """Tahmin TP'lerin gerçek kapanışıyla aynıdır: sıra fiyata yakından, miktarsız TP kalanı kapatır, kayma ve
    komisyon dahil. Paneldeki "dolarsa" tutarı gerçekte yazılan net'ten ayrışamaz."""
    ids = _guarded(broker)
    got = {x.order_id: x for x in broker.outlook(pos(broker))}
    price(broker, 110)
    price(broker, 120)
    t1, t2 = broker.ledger.trades(broker.account_id)
    assert (got[ids[110]].qty, got[ids[120]].qty) == (1, 2)
    assert (got[ids[110]].net, got[ids[120]].net) == (pytest.approx(t1.net), pytest.approx(t2.net))


def test_outlook_predicts_the_stop_closes(broker):
    """Stoplar TP'lerden ayrı, kendi sıralarıyla tahmin edilir. İzin seviyesi emrin en iyi fiyatından bugün hesaplanır,
    fiyata en yakın stop önce kapatır."""
    ids = _guarded(broker)
    got = {x.order_id: x for x in broker.outlook(pos(broker))}
    assert got[ids[2]].level == pytest.approx(98)
    mark(broker, 96)
    t1, t2 = broker.ledger.trades(broker.account_id)
    assert (got[ids[2]].qty, got[ids[97]].qty) == (1, 2)
    assert (got[ids[2]].net, got[ids[97]].net) == (pytest.approx(t1.net), pytest.approx(t2.net))
    assert None not in got


def test_outlook_puts_the_liquidation_before_a_stop_beyond_it(broker):
    """Likidasyonun ötesindeki SL hiçbir zaman dolmaz. Tahmin önce likidasyonu koyar, SL'nin tutarı sıfır olur."""
    run(broker, buy(3, protection=(Protection(STOP, price=90),)))
    liq, sl = broker.outlook(pos(broker))
    mark(broker, 89)
    t = broker.ledger.trades(broker.account_id)[0]
    assert (liq.order_id, liq.qty, sl.qty) == (None, 3, 0)
    assert (t.reason, liq.net) == ("liquidation", pytest.approx(t.net))


def test_outlook_of_a_stop_after_a_full_close_is_zero(broker):
    """Önündeki emir pozisyonun tamamını kapatıyorsa sonraki emir hiçbir şey kapatmaz, tutarı iki kez sayılmaz."""
    run(broker, buy(1, protection=(Protection(STOP, price=95), Protection(STOP, price=90))))
    got = sorted(broker.outlook(pos(broker)), key=lambda x: -x.level)
    assert (got[0].qty, got[1].qty, got[1].net) == (1, 0, 0)


# --- Son fiyatla tetiklenen SL ve canlı parite dizini ---

def _trades(b: PaperBroker):
    return b.ledger.trades(b.account_id)


def test_sl_by_last_ignores_mark_and_fires_on_the_last_price(broker):
    """İğnede mark fiyatı son fiyatın gerisinde kalır. Son fiyatla tetiklenmesi istenen SL mark'a bakarsa geç
    çalışır, mark'la tetiklenmesi istenen eski SL'ler ise eskisi gibi kalmalı."""
    run(broker, SetLeverage(SOL, 5))
    run(broker, buy(1, protection=(Protection(STOP, price=95, by_last=True),)))
    assert [o.by_last for o in exits(broker)] == [True]
    mark(broker, 90, fill=90)
    assert pos(broker) is not None
    price(broker, 94.9, open_=96)
    t = _trades(broker)[-1]
    assert pos(broker) is None and t.reason == "sl"
    assert t.exit_price == pytest.approx(95 * (1 - SLIP))


def test_sl_by_last_fills_from_the_open_after_a_jump(broker):
    """Fiyat seviyenin ötesine sıçradıysa stop-market tetik anındaki fiyattan dolar, seviyeden değil."""
    run(broker, buy(1, protection=(Protection(STOP, price=95, by_last=True),)))
    price(broker, 93, open_=93)
    assert _trades(broker)[-1].exit_price == pytest.approx(93 * (1 - SLIP))


def test_sl_by_last_is_checked_against_the_last_price_when_placed(broker):
    """Son fiyatla tetiklenen SL'nin doğru tarafta olup olmadığı son fiyata göre denetlenir. Mark'a göre
    denetlenseydi iğnede geçerli bir SL reddedilirdi. TP ve izde bu seçenek yoktur."""
    run(broker, SetLeverage(SOL, 5))
    run(broker, buy(1))
    mark(broker, 90)
    run(broker, buy(None, STOP, 95, reduce=True, by_last=True))
    with pytest.raises(RuleError, match="yalnızca SL'de"):
        run(broker, buy(None, TP, 110, reduce=True, by_last=True))
    with pytest.raises(RuleError, match="yalnızca SL'de"):
        run(broker, buy(1, protection=(Protection(TRAIL, trail_pct=1, by_last=True),)))


def test_sl_by_last_comes_before_tp_in_the_same_range(broker):
    """Aynı aralıkta TP ve SL birlikte tetiklenirse SL önce sayılır. Tersi kâğıt hesabı olduğundan iyi gösterirdi."""
    run(broker, buy(1, protection=(Protection(TP, price=105), Protection(STOP, price=95, by_last=True))))
    price(broker, 100, high=106, low=94)
    assert _trades(broker)[-1].reason == "sl"


def test_live_index_follows_orders_and_positions(broker):
    """Emri ve pozisyonu olmayan paritenin fiyatı defteri sorgulamaz. Dizin bir değişiklikten sonra yenilenmezse
    yeni emir hiç dolmaz ya da kapanan pozisyonun fiyatı boşa işlenir."""
    assert broker.live_symbols() == set()
    run(broker, buy(1, LIMIT, 99))
    assert broker.live_symbols() == {SOL}
    price(broker, 98.9)
    assert pos(broker) is not None and broker.live_symbols() == {SOL}
    run(broker, ClosePosition(SOL, LONG))
    assert broker.live_symbols() == set()
