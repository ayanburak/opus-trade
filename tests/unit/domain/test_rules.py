"""
file: test_rules.py
importable: yok

Hesap kurallarının testleri. Her test bir kuralın yanlış hesaplanması halinde
paranın ya da riskin yanlış görüneceği bir noktayı korur. Beklenen rakamlar
kuraldan elle hesaplanmıştır.

description: domain.rules için birim testleri.
"""

from dataclasses import replace

import pytest

from opus_trade.domain import rules
from opus_trade.domain.errors import RuleError
from opus_trade.domain.fees import BINANCE_STANDARD
from opus_trade.domain.models import Bracket, Side, TpLevel
from tests.fakes import catalog

CATALOG = catalog()
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
    assert rules.liq_price(SOL, Side.LONG, entry=100, qty=1, margin=10) == pytest.approx(90 / 0.995)
    assert rules.liq_price(SOL, Side.SHORT, entry=100, qty=1, margin=10) == pytest.approx(110 / 1.005)


def test_liq_price_uses_the_bracket_of_the_position_size():
    """Büyük pozisyonun bakım marjı üst kademeden gelir, sabitiyle birlikte. İlk kademenin oranı kullanılsaydı
    likidasyon gerçekte olduğundan uzakta görünürdü."""
    spec = replace(SOL, brackets=(Bracket(1000, 100, 0.005, 0), Bracket(10000, 50, 0.01, 5)))
    assert rules.liq_price(spec, Side.LONG, entry=100, qty=20, margin=200) == pytest.approx((2000 - 200 - 5) / (20 * 0.99))
    assert rules.liq_price(spec, Side.SHORT, entry=100, qty=20, margin=200) == pytest.approx((2000 + 200 + 5) / (20 * 1.01))


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


def test_tp_shares_follow_the_position_after_it_shrinks():
    """Kısmi kapanıştan sonra kademe miktarı aynı kalır, yüzdesi kalan pozisyondaki payı olur.
    Eski yüzde görünürse panel ve Opus kademenin yarısını kapatacağını sanır, oysa çoğunu kapatır."""
    out = rules.tp_shares((TpLevel(103, 50, 0.069), TpLevel(106, 50, None)), 0.098)
    assert out == (TpLevel(103, 70.41, 0.069), TpLevel(106, 29.59, None))


def test_tp_shares_give_back_the_same_qty_when_resubmitted():
    """Güncel yüzdeyle kademe yeniden kurulunca aynı miktar çıkmalı. 0,33/0,97 yüzde 34,0206'dır, aşağı
    yuvarlanıp 34,02 yazılsaydı split_tp 0,32 kurar ve kademe bir adım küçülürdü."""
    out = rules.tp_shares((TpLevel(103, 50, 0.33), TpLevel(106, 50, None)), 0.97)
    again = rules.split_tp(SOL, [TpLevel(k.price, k.pct) for k in out], 0.97)
    assert again[0].qty == 0.33


def test_tp_shares_drop_levels_the_position_no_longer_reaches():
    """Pozisyon ilk kademeden küçülünce ilk kademe hepsini kapatır, sonrakiler hiç dolamaz ve düşer.
    Bütün kademeler miktarlıyken de yüzdelerin toplamı yüzü geçmez, yoksa yeniden gönderim reddedilir."""
    out = rules.tp_shares((TpLevel(103, 50, 0.5), TpLevel(106, 30, 0.3), TpLevel(109, 20, None)), 0.4)
    assert out == (TpLevel(103, 100.0, 0.5),)
    out = rules.tp_shares((TpLevel(103, 20, 0.2), TpLevel(106, 10, 0.1)), 0.3)
    assert sum(k.pct for k in out) <= 100


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


# --- Mesajlar ---

def test_rule_messages_write_numbers_the_turkish_way():
    """Kural mesajı panelde ve Opus'ta olduğu gibi görünür, sayılar virgüllü olmalı."""
    with pytest.raises(RuleError, match="Long için SL 101, referans fiyat 100,5 altında olmalı"):
        rules.check_sl(Side.LONG, 100.5, 101)


def test_size_says_qty_rounds_to_zero_instead_of_zero_dollars():
    """Miktar sıfıra yuvarlanınca büyüklük 0,00 dolar demek kafa karıştırır, en küçük miktar söylenir."""
    with pytest.raises(RuleError, match="miktar sıfıra yuvarlanıyor, en küçük miktar 0,01 SOL") as e:
        rules.size(SOL, 1, 0.5, 100, TAKER, 100)
    assert "0,00 dolar" not in str(e.value)


# --- Binance sınırları ---

def test_leverage_is_a_whole_number_within_the_first_bracket():
    """Binance kaldıracı tam sayı kabul eder, en yükseği ilk kademenin kaldıracıdır."""
    rules.check_leverage(SOL, 100, 1000)
    for bad in (0, 2.5, 101):
        with pytest.raises(RuleError, match="1 ile 100 arasında bir tam sayı"):
            rules.check_leverage(SOL, bad, 1000)


def test_high_leverage_caps_the_position_size():
    """Kaldıraç büyüdükçe izin verilen pozisyon küçülür. Sınır aşılırsa Binance emri reddeder."""
    spec = replace(SOL, brackets=(Bracket(1000, 100, 0.005, 0), Bracket(10000, 50, 0.01, 5)))
    rules.check_leverage(spec, 50, 10000)
    with pytest.raises(RuleError, match="100x kaldıraçla pozisyon en fazla 1000 dolar"):
        rules.check_leverage(spec, 100, 1500)


def test_prices_must_sit_on_the_tick():
    """Adıma uymayan fiyatı Binance reddeder, kâğıt hesap da aynısını yapmalı. Sıfır SL korumayı kaldırır, geçer."""
    rules.check_ticks(SOL, 100.01, None, 0)
    with pytest.raises(RuleError, match="fiyat adımı 0,01 değerinin katı"):
        rules.check_ticks(SOL, 100.005)
    assert rules.to_tick(SOL, 100.006) == 100.01 and rules.to_tick(BTC, 62420.04) == 62420.0


def test_trail_stays_in_binance_range():
    """Binance izi yüzde 0,1 ile 10 arasında kabul eder."""
    rules.check_trail(0.1)
    rules.check_trail(10)
    for bad in (0.05, 10.5):
        with pytest.raises(RuleError, match="yüzde 0,1 ile 10 arasında"):
            rules.check_trail(bad)


def test_size_respects_the_market_order_limit():
    """Tek piyasa emri Binance'in miktar sınırını aşamaz."""
    spec = replace(SOL, max_market_qty=5)
    with pytest.raises(RuleError, match="en fazla 5 SOL"):
        rules.size(spec, 10, 100, 100, TAKER, 1000)
