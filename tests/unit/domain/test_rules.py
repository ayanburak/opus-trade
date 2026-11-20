"""
file: test_rules.py
importable: yok

Hesap kurallarının testleri. Her test bir kuralın yanlış hesaplanması halinde
paranın ya da riskin yanlış görüneceği bir noktayı korur. Beklenen rakamlar
kuraldan elle hesaplanmıştır.

description: domain.rules için birim testleri.
"""

import pytest

from opus_trade.domain import rules
from opus_trade.domain.catalog import Catalog
from opus_trade.domain.errors import RuleError
from opus_trade.domain.fees import BINANCE_STANDARD
from opus_trade.domain.models import Side, TpLevel

CATALOG = Catalog()
BTC, SOL, XRP = CATALOG.get("BTCUSDT"), CATALOG.get("SOLUSDT"), CATALOG.get("XRPUSDT")
TAKER = BINANCE_STANDARD.taker


# --- Miktar ve büyüklük ---

def test_round_qty_keeps_exact_multiples():
    """0.29 SOL kayan noktada 28.999 adım çıkar. Düz floor bir adım eksik miktar açardı."""
    assert rules.round_qty(SOL, 0.29) == 0.29
    assert rules.round_qty(XRP, 4.3) == 4.3


def test_min_notional_is_the_larger_of_dollar_limit_and_one_step():
    """BTC'de bir adım 100 dolar eder ve 50 dolarlık sınırdan büyüktür. Yanlış sınır açılamayacak işlemi kabul ederdi."""
    assert rules.min_notional(BTC, 100_000) == pytest.approx(100)
    assert rules.min_notional(SOL, 100) == pytest.approx(5)


def test_size_uses_rounded_qty_for_margin_and_fee():
    """Marjin ve komisyon yuvarlanmış miktardan hesaplanır. Verilen marjinden hesaplansa nakit sessizce kayardı."""
    s = rules.size(SOL, leverage=10, margin=10.3, entry=100, fee_rate=TAKER, cash=50)
    assert s.qty == 1.03
    assert s.margin == pytest.approx(10.3)
    assert s.fee == pytest.approx(103 * TAKER)


def test_size_shrinks_qty_when_cash_is_slightly_short():
    """Bakiyenin tamamıyla açılan işlem komisyon yüzünden birkaç sent eksik kalır. Reddedilmek yerine küçülür."""
    s = rules.size(SOL, leverage=10, margin=10, entry=100, fee_rate=TAKER, cash=10.04)
    assert s.qty == 0.99
    assert s.margin + s.fee <= 10.04


def test_size_rejects_when_cash_is_clearly_short():
    """Nakit belirgin biçimde yetmiyorsa işlem küçültülerek açılmaz, istenenden çok farklı bir pozisyon doğardı."""
    with pytest.raises(RuleError, match="Yetersiz nakit"):
        rules.size(SOL, leverage=10, margin=10, entry=100, fee_rate=TAKER, cash=9)


def test_size_rejects_below_min_notional_and_says_needed_margin():
    """Binance'in reddedeceği küçük işlem burada reddedilir ve mesaj gereken marjini söyler."""
    with pytest.raises(RuleError, match="en az 10.00 dolar marjin"):
        rules.size(BTC, leverage=10, margin=5, entry=100_000, fee_rate=TAKER, cash=100)


# --- Likidasyon ve fonlama ---

def test_liq_price_sits_maint_margin_before_margin_is_gone():
    """Likidasyon marjinin tamamen bittiği fiyattan bakım marjı kadar önce gelir, iki yönde de."""
    assert rules.liq_price(SOL, Side.LONG, entry=100, qty=1, margin=10) == pytest.approx(90 / 0.99)
    assert rules.liq_price(SOL, Side.SHORT, entry=100, qty=1, margin=10) == pytest.approx(110 / 1.01)


def test_funding_sign_follows_side():
    """Pozitif oranda long öder, short alır. İşaret ters olursa fonlama hesabı büyütür."""
    assert rules.funding_payment(Side.LONG, qty=2, mark=100, rate=0.0001) == pytest.approx(0.02)
    assert rules.funding_payment(Side.SHORT, qty=2, mark=100, rate=0.0001) == pytest.approx(-0.02)


# --- Emir ---

@pytest.mark.parametrize("side, trigger, maker", [
    (Side.LONG, 99, True), (Side.LONG, 101, False), (Side.SHORT, 101, True), (Side.SHORT, 99, False),
])
def test_order_is_maker_only_when_price_comes_to_it(side, trigger, maker):
    """Lehe tetik limit gibi kaymasız ve maker, kırılım tetiği stop gibi taker ve kaymalı gerçekleşir."""
    assert rules.order_is_maker(side, trigger, price=100) is maker


# --- Koruma ---

def test_sl_on_profit_side_is_rejected():
    """Kâr tarafına konan SL pozisyonu hemen kapatırdı."""
    with pytest.raises(RuleError):
        rules.check_sl(Side.SHORT, ref=100, sl=95)


def test_tp_levels_are_ordered_nearest_first():
    """Eşleştirici listenin başına bakar. Short'ta en yakın kademe en yüksek fiyattır."""
    out = rules.check_tp_levels(Side.SHORT, 100, [TpLevel(90, 50), TpLevel(95, 50)])
    assert [k.price for k in out] == [95, 90]


def test_tp_levels_over_hundred_percent_are_rejected():
    """Toplamı yüzü geçen kademeler pozisyonda olmayan miktarı kapatmaya çalışırdı."""
    with pytest.raises(RuleError, match="en fazla 100"):
        rules.check_tp_levels(Side.LONG, 100, [TpLevel(110, 60), TpLevel(120, 50)])


def test_split_tp_leaves_last_level_as_the_remainder():
    """Yüzdeler yüzü bulunca son kademe kalanın tamamıdır, yuvarlama yüzünden kırıntı açık kalmaz."""
    out = rules.split_tp(SOL, [TpLevel(110, 33), TpLevel(120, 67)], qty=1.0)
    assert out[0].qty == 0.33
    assert out[1].qty is None


def test_split_tp_rejects_level_smaller_than_one_step():
    """Bir adımdan küçük kademe borsada açılamaz. Emir kurulurken reddedilmezse tetik anında giriş kaçardı."""
    with pytest.raises(RuleError, match="en küçük miktar adımının altında"):
        rules.split_tp(SOL, [TpLevel(110, 40), TpLevel(120, 60)], qty=0.02)


def test_trailed_sl_rejects_trail_already_behind_price():
    """En iyi fiyat 110, yüzde 5 iz 104.5'e düşer. Fiyat 104'teyse stop kurulurken tetiklenmiş olurdu."""
    with pytest.raises(RuleError, match="yüzde 5.45"):
        rules.trailed_sl(Side.LONG, best=110, pct=5, sl=None, price=104)


def test_trailed_sl_keeps_the_tighter_of_trail_and_existing_sl():
    """İz var olan SL'yi yalnızca fiyata yaklaştırabilir, hiçbir zaman uzaklaştırmaz."""
    assert rules.trailed_sl(Side.LONG, best=110, pct=5, sl=100, price=106) == pytest.approx(104.5)
    assert rules.trailed_sl(Side.LONG, best=110, pct=5, sl=105, price=106) == 105
