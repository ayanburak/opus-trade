"""
file: test_rules.py
importable: yok

Hesap kurallarının testleri. Her test bir kuralın yanlış hesaplanması halinde
paranın ya da riskin yanlış görüneceği ya da kâğıt hesabın canlının
reddedeceği bir emri kabul edeceği bir noktayı korur. Beklenen rakamlar
kuraldan elle hesaplanmıştır.

description: domain.rules için birim testleri.
"""

from dataclasses import replace

import pytest

from opus_trade.domain import rules
from opus_trade.domain.errors import RuleError
from opus_trade.domain.models import Bracket, Side
from opus_trade.domain.trading import (AccountState, AmendOrder, CancelOrder, ClosePosition, CloseAll, OrderType,
                                       PlaceOrder, Ref, SetLeverage, risky)
from tests.fakes import catalog

CATALOG = catalog()
BTC, SOL, XRP = CATALOG.get("BTCUSDT"), CATALOG.get("SOLUSDT"), CATALOG.get("XRPUSDT")

# İki kademeli küçük tablo, kademe sınırını küçük rakamlarla denemek için.
TWO = replace(SOL, brackets=(Bracket(1000, 100, 0.005, 0), Bracket(10000, 50, 0.01, 5)))


# --- Miktar ---

def test_round_qty_keeps_exact_multiples():
    """0.29 SOL kayan noktada 28.999 adım çıkar. Düz floor bir adım eksik miktar açardı."""
    assert rules.round_qty(SOL, 0.29) == 0.29
    assert rules.round_qty(XRP, 4.3) == 4.3


def test_min_notional_is_the_larger_of_dollar_limit_and_one_step():
    """BTC'de bir adım 100 dolar eder ve 50 dolarlık sınırdan büyüktür. Yanlış sınır açılamayacak işlemi kabul ederdi."""
    assert rules.min_notional(BTC, 100_000) == pytest.approx(100)
    assert rules.min_notional(SOL, 100) == pytest.approx(5)


def test_qty_for_margin_never_exceeds_the_given_margin():
    """Marjinle düşünen taraf miktarı bununla bulur. Yukarı yuvarlansa istenenden fazla marjin bağlanırdı."""
    qty = rules.qty_for_margin(SOL, leverage=10, margin=10.309, price=100)
    assert qty == 1.03
    assert rules.order_margin(qty, 100, 10) <= 10.309


def test_qty_must_sit_on_the_step_and_above_the_minimum():
    """Adıma uymayan ya da en küçük büyüklüğün altındaki miktarı Binance reddeder."""
    rules.check_qty(SOL, 0.05, price=100, market=True)
    with pytest.raises(RuleError, match="miktar adımı 0,01 SOL değerinin katı"):
        rules.check_qty(SOL, 0.055, price=100, market=True)
    with pytest.raises(RuleError, match="en küçük işlem büyüklüğü yaklaşık 5,00 dolar, bu emirle 4,00 dolar"):
        rules.check_qty(SOL, 0.04, price=100, market=True)
    with pytest.raises(RuleError, match="sıfırdan büyük"):
        rules.check_qty(SOL, 0, price=100, market=True)


def test_market_qty_limit_is_tighter_than_the_order_limit():
    """Binance'te piyasa emrinin miktar sınırı limit emrinkinden dardır. Tek sınır olsa ya büyük limit emri
    reddedilir ya da büyük piyasa emri kabul edilirdi."""
    spec = replace(SOL, max_market_qty=5, max_qty=50)
    rules.check_qty(spec, 20, price=100, market=False)
    with pytest.raises(RuleError, match="tek piyasa emrinin miktarı en fazla 5 SOL"):
        rules.check_qty(spec, 20, price=100, market=True)
    with pytest.raises(RuleError, match="tek emrin miktarı en fazla 50 SOL"):
        rules.check_qty(spec, 60, price=100, market=False)


# --- Yön, limit ve birleşme ---

@pytest.mark.parametrize("side, reduce, buy", [
    (Side.LONG, False, True), (Side.LONG, True, False), (Side.SHORT, False, False), (Side.SHORT, True, True)])
def test_is_buy_follows_hedge_mode(side, reduce, buy):
    """Long'u açan ve short'u kapatan emir alıştır. Ters olursa kayma ve tetik yönü yanlış hesaplanır."""
    assert rules.is_buy(side, reduce) is buy


@pytest.mark.parametrize("buy, limit, crosses", [(True, 101, True), (True, 100, True), (True, 99, False),
                                                 (False, 99, True), (False, 101, False)])
def test_limit_on_the_wrong_side_crosses(buy, limit, crosses):
    """Fiyatın üstündeki alış ve altındaki satış limiti hemen taker dolar. Maker sayılsa ucuz ve kaymasız dolardı."""
    assert rules.crosses(buy, limit, 100) is crosses


def test_merge_entry_weights_by_qty():
    """Aynı yöndeki giriş pozisyonla birleşir, giriş miktarla ağırlıklı ortalanır. Düz ortalama kârı bozardı."""
    assert rules.merge_entry(1, 100, 3, 120) == pytest.approx(115)


# --- Maliyet ---

def test_order_cost_adds_open_loss_only_on_the_bad_side():
    """Mark'ın üstündeki alış ya da altındaki satış daha dolarken zararda başlar, Binance bu farkı da ayırır.
    Lehe fiyattaki emirde açık zarar yoktur."""
    assert rules.order_margin(2, 100, 10) == pytest.approx(20)
    assert rules.open_loss(True, 2, price=101, mark=100) == pytest.approx(2)
    assert rules.open_loss(False, 2, price=99, mark=100) == pytest.approx(2)
    assert rules.open_loss(True, 2, price=99, mark=100) == 0
    assert rules.open_loss(False, 2, price=101, mark=100) == 0


# --- Likidasyon ve fonlama ---

def test_liq_price_sits_maint_margin_before_margin_is_gone():
    """Likidasyon marjinin tamamen bittiği fiyattan bakım marjı kadar önce gelir, iki yönde de."""
    assert rules.liq_price(SOL, Side.LONG, entry=100, qty=1, margin=10) == pytest.approx(90 / 0.995)
    assert rules.liq_price(SOL, Side.SHORT, entry=100, qty=1, margin=10) == pytest.approx(110 / 1.005)


def test_liq_price_uses_the_bracket_of_the_position_size():
    """Büyük pozisyonun bakım marjı üst kademeden gelir, sabitiyle birlikte. İlk kademenin oranı kullanılsaydı
    likidasyon gerçekte olduğundan uzakta görünürdü."""
    assert rules.liq_price(TWO, Side.LONG, entry=100, qty=20, margin=200) == pytest.approx((2000 - 200 - 5) / (20 * 0.99))
    assert rules.liq_price(TWO, Side.SHORT, entry=100, qty=20, margin=200) == pytest.approx((2000 + 200 + 5) / (20 * 1.01))


def test_liq_fee_comes_from_the_symbol():
    """Likidasyon ücreti Binance'in parite başına oranıdır, BTC'de binde 12,5, SOL'de binde 15."""
    assert rules.liq_fee(BTC, qty=0.01, price=100_000) == pytest.approx(12.5)
    assert rules.liq_fee(SOL, qty=10, price=100) == pytest.approx(15)


def test_funding_sign_follows_side():
    """Pozitif oranda long öder, short alır. İşaret ters olursa fonlama hesabı büyütür."""
    assert rules.funding_payment(Side.LONG, qty=2, mark=100, rate=0.0001) == pytest.approx(0.02)
    assert rules.funding_payment(Side.SHORT, qty=2, mark=100, rate=0.0001) == pytest.approx(-0.02)


# --- Kaldıraç ---

def test_leverage_is_a_whole_number_within_the_first_bracket():
    """Binance kaldıracı tam sayı kabul eder, en yükseği ilk kademenin kaldıracıdır."""
    rules.check_leverage(SOL, 100)
    for bad in (0, 2.5, 101):
        with pytest.raises(RuleError, match="1 ile 100 arasında bir tam sayı"):
            rules.check_leverage(SOL, bad)


def test_high_leverage_caps_the_position_size():
    """Kaldıraç büyüdükçe izin verilen pozisyon küçülür. Sınır aşılırsa Binance emri reddeder."""
    rules.check_notional(TWO, 50, 10000)
    with pytest.raises(RuleError, match="100x kaldıraçla pozisyon en fazla 1000 dolar"):
        rules.check_notional(TWO, 100, 1500)


def test_leverage_cannot_drop_while_a_position_is_open():
    """İzole marjinde açık pozisyon varken kaldıraç düşürülemez, Binance -4161 ile reddeder. Pozisyon yokken
    düşürme serbesttir."""
    rules.check_leverage_change(TWO, old=20, new=5, notional=0)
    with pytest.raises(RuleError, match="açık pozisyon varken kaldıraç düşürülemez, bugünkü kaldıraç 20x"):
        rules.check_leverage_change(TWO, old=20, new=5, notional=500)


def test_leverage_can_rise_only_if_the_open_size_fits():
    """Açık pozisyonda kaldıraç yükseltilebilir ama açık büyüklük yeni kaldıracın kademe sınırına sığmalı."""
    rules.check_leverage_change(TWO, old=20, new=100, notional=900)
    with pytest.raises(RuleError, match="100x kaldıraçla pozisyon en fazla 1000 dolar"):
        rules.check_leverage_change(TWO, old=20, new=100, notional=1500)


def test_unset_symbol_uses_binance_default_leverage():
    """Kaldıracı hiç ayarlanmamış parite Binance'teki gibi 20x'tir. Başka bir değer marjini yanlış hesaplatırdı."""
    state = AccountState(1, 100, 100, (), (), {"BTCUSDT": 5})
    assert state.leverage("BTCUSDT") == 5
    assert state.leverage("SOLUSDT") == rules.DEFAULT_LEVERAGE == 20


# --- Fiyat ve tetik ---

def test_prices_must_sit_on_the_tick():
    """Adıma uymayan fiyatı Binance reddeder, kâğıt hesap da aynısını yapmalı."""
    rules.check_ticks(SOL, 100.01, None)
    with pytest.raises(RuleError, match="fiyat adımı 0,01 değerinin katı"):
        rules.check_ticks(SOL, 100.005)
    assert rules.to_tick(SOL, 100.006) == 100.01 and rules.to_tick(BTC, 62420.04) == 62420.0


def test_limit_price_stays_within_the_band_of_mark():
    """Binance limit fiyatının mark fiyatından uzaklığını sınırlar. Kâğıt hesap sınır dışı limiti kabul etmemeli."""
    spec = replace(SOL, price_up=1.05, price_down=0.95)
    rules.check_price_band(spec, True, 105, mark=100)
    rules.check_price_band(spec, False, 95, mark=100)
    with pytest.raises(RuleError, match="alış limiti en fazla 105,00 olabilir, mark fiyatı 100,00"):
        rules.check_price_band(spec, True, 105.5, mark=100)
    with pytest.raises(RuleError, match="satış limiti en az 95,00 olabilir"):
        rules.check_price_band(spec, False, 94.5, mark=100)


@pytest.mark.parametrize("stop, buy, ok, bad", [(True, True, 101, 99), (True, False, 99, 101),
                                                (False, True, 99, 101), (False, False, 101, 99)])
def test_trigger_must_be_on_the_side_price_has_not_reached(stop, buy, ok, bad):
    """Stop alış fiyatın üstünde, stop satış altında, take-profit tersi kurulur. Geçilmiş tetik Binance'te
    hemen tetiklenirdi diye reddedilir. Kabul edilse kurulduğu an dolardı."""
    rules.check_trigger(stop, buy, False, ok, 100, "son fiyat")
    with pytest.raises(RuleError, match="yoksa emir hemen tetiklenirdi"):
        rules.check_trigger(stop, buy, False, bad, 100, "son fiyat")
    with pytest.raises(RuleError):
        rules.check_trigger(stop, buy, False, 100, 100, "son fiyat")


def test_trigger_message_names_the_order_as_opus_and_the_panel_think():
    """Mesaj Binance'in alış satış dilini değil kırılım, geri çekilme, SL ve TP adlarını ve tetiğin baktığı
    fiyatı kullanır. SL mark fiyatına bakar, son fiyat yazsaydı Opus yanlış fiyata göre düzeltirdi. Sayılar
    virgüllüdür."""
    with pytest.raises(RuleError, match="SL tetiği 101,00, mark fiyatı 100,50 olduğu için bu fiyatın altında olmalı"):
        rules.check_trigger(True, False, True, 101, 100.5, "mark fiyatı")
    with pytest.raises(RuleError, match="^Kırılım girişinin tetiği 100,00"):
        rules.check_trigger(True, True, False, 100, 100.5, "son fiyat")
    with pytest.raises(RuleError, match="^Geri çekilme girişinin tetiği 101,00"):
        rules.check_trigger(False, True, False, 101, 100.5, "son fiyat")
    with pytest.raises(RuleError, match="^TP tetiği 98,00"):
        rules.check_trigger(False, False, True, 98, 99, "son fiyat")


# --- İz süren stop ---

def test_trail_stays_in_binance_range():
    """Binance izi yüzde 0,1 ile 10 arasında kabul eder."""
    rules.check_trail(0.1)
    rules.check_trail(10)
    for bad in (0.05, 10.5):
        with pytest.raises(RuleError, match="yüzde 0,1 ile 10 arasında"):
            rules.check_trail(bad)


def test_trail_level_sits_behind_the_best_price():
    """İz long'da en yüksek fiyatın altında, short'ta en düşüğün üstünde durur. Yön ters olsa iz hemen dolardı."""
    assert rules.trail_level(Side.LONG, 200, 1) == pytest.approx(198)
    assert rules.trail_level(Side.SHORT, 100, 2) == pytest.approx(102)


# --- Kapanış ---

def test_close_fill_slips_against_the_position():
    """Taker kapanış aleyhe kayar: long'un kapanışı satış, short'unki alıştır. Maker kapanış seviyesinden dolar."""
    price, pnl, fee = rules.close_fill(SOL, Side.LONG, entry=100, qty=1, price=110, maker=False, fee_rate=0.0005)
    assert price == pytest.approx(110 * (1 - SOL.slippage))
    assert pnl == pytest.approx(price - 100) and fee == pytest.approx(price * 0.0005)
    price, _, _ = rules.close_fill(SOL, Side.SHORT, entry=100, qty=1, price=90, maker=False, fee_rate=0.0005)
    assert price == pytest.approx(90 * (1 + SOL.slippage))
    assert rules.close_fill(SOL, Side.SHORT, 100, 1, 90, maker=True, fee_rate=0.0002)[0] == 90


# --- Komutlar ---

def test_only_commands_that_can_add_risk_are_risky():
    """Kısıtlar yalnızca riski artırabilen komutlara bakar. Kapatma, koruma ve iptal hiçbir zaman engellenmemeli."""
    ref = Ref(1, "SOLUSDT", Side.LONG)
    assert risky(PlaceOrder("SOLUSDT", Side.LONG, OrderType.MARKET, qty=1))
    assert risky(SetLeverage("SOLUSDT", 10)) and risky(AmendOrder(ref, 99))
    assert not risky(PlaceOrder("SOLUSDT", Side.LONG, OrderType.STOP, price=90, reduce=True))
    assert not any(risky(c) for c in (CancelOrder(ref), ClosePosition("SOLUSDT", Side.LONG), CloseAll()))
