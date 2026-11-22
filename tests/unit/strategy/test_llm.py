"""
file: test_llm.py
importable: yok

LLM stratejisinin testleri. Opus'un Türkçe eylemi yanlış komuta çevrilirse
yanlış pozisyon kapanır. Talimata yasak bir kelime ya da yanlış bir oran
girerse Opus'un davranışı fark edilmeden değişir. claude çıktısı yanlış
okunursa karar ya da araç kaydı kaybolur. Gerçek Opus çağrısı yapılmaz.

description: strategy.llm paketi için birim testleri.
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

from opus_trade.domain.fees import FeeSchedule
from opus_trade.domain.models import Side
from opus_trade.domain.trading import (AccountEvent, AccountState, Actor, AmendOrder, CancelOrder, ClosePosition,
                                       EventKind, Order, OrderType, PlaceOrder, Position, Protection, Ref, SetLeverage,
                                       Trade)
from opus_trade.strategy.base import ActionResult, Context, Wake
from opus_trade.strategy.llm import actions, prompt
from opus_trade.strategy.llm import strategy as llm_strategy
from opus_trade.strategy.llm.client import ClaudeCliClient, LLMError, LLMRequest, LLMResult
from opus_trade.triggers.price_alert import Alert
from tests.fakes import catalog

NOW = 1_795_000_000
SOL = "SOLUSDT"


# --- Eylemler ---

# Açık 2 SOL long, bir TP ve bir SL emriyle. Kaldıraç ayarlı değil, 20x.
# Nakit 100 dolar, bekleyen giriş yok.
POS = Position(1, 1, SOL, Side.LONG, 2, 10, 100, NOW, 100, NOW, 100, NOW, 95.5, 0.1, 0)
TP_ORDER = Order(5, 1, 1, SOL, Side.LONG, OrderType.TAKE_PROFIT, True, None, 110, None, None, 0, (), None, NOW)
SL_ORDER = Order(6, 1, 2, SOL, Side.LONG, OrderType.STOP, True, 1.0, 95, None, None, 0, (), None, NOW)
STATE = AccountState(1, 100, 100, (POS,), (TP_ORDER, SL_ORDER))
FEES = FeeSchedule(taker=0.0005, maker=0.0002)


def translate(a: dict, state: AccountState = STATE) -> actions.Translation:
    return actions.translate(a, state, {SOL: 100.0}, {SOL: 100.0}, catalog(), FEES)


def cmds(a: dict, state: AccountState = STATE) -> list:
    return translate(a, state).item


def test_close_finds_the_position_by_symbol_and_side():
    """Pozisyonun numarası yoktur, parite ve yönle bulunur. Yüzdeli kapanış miktara çevrilir."""
    assert cmds({"tip": "pozisyon_kapat", "sembol": SOL, "yon": "long", "yuzde": 50}) == [
        ClosePosition(SOL, Side.LONG, 1)]
    assert cmds({"tip": "pozisyon_kapat", "sembol": SOL, "yon": "long"}) == [ClosePosition(SOL, Side.LONG)]
    with pytest.raises(ValueError, match="Açık SOLUSDT short pozisyonu yok"):
        translate({"tip": "pozisyon_kapat", "sembol": SOL, "yon": "short"})


def test_entry_turns_margin_into_qty_and_sets_leverage_first():
    """Komutlar miktar taşır. Marjin kaymalı fiyat ve kaldıraçla miktara çevrilir, kaldıraç farklıysa önce
    parite ayarlanır. TP kademeleri miktarlı olur, son kademe artanı alır, böylece yuvarlamadan korumasız
    kırıntı kalmaz. SL miktarsızdır, girişin tamamını korur."""
    out = cmds({"tip": "piyasa_giris", "sembol": SOL, "yon": "long", "kaldirac": 25, "marjin_usd": 10, "sl": 90,
                "tp_kademeleri": [{"fiyat": 110, "yuzde": 33}, {"fiyat": 120, "yuzde": 67}]})
    assert out == [SetLeverage(SOL, 25), PlaceOrder(SOL, Side.LONG, OrderType.MARKET, qty=2.49, protection=(
        Protection(OrderType.TAKE_PROFIT, 110, qty=0.82), Protection(OrderType.TAKE_PROFIT, 120, qty=1.67),
        Protection(OrderType.STOP, 90)))]


def test_entry_without_leverage_uses_the_pair_setting():
    """Kaldıraç verilmezse paritenin ayarı kullanılır ve kaldıraç komutu gitmez."""
    out = cmds({"tip": "piyasa_giris", "sembol": SOL, "yon": "short", "marjin_usd": 10})
    assert out == [PlaceOrder(SOL, Side.SHORT, OrderType.MARKET, qty=2.0)]


def test_lowering_leverage_with_an_open_position_is_refused_before_anything_is_sent():
    """Düşürme Binance'te reddedilir. Çeviri bunu baştan söylemezse giriş eski kaldıraçla, Opus'un istemediği
    marjinle açılırdı."""
    with pytest.raises(ValueError, match="kaldıraç düşürülemez"):
        translate({"tip": "piyasa_giris", "sembol": SOL, "yon": "long", "kaldirac": 5, "marjin_usd": 10})
    with pytest.raises(ValueError, match="zaten 20x"):
        translate({"tip": "kaldirac_ayarla", "sembol": SOL, "kaldirac": 20})
    assert cmds({"tip": "kaldirac_ayarla", "sembol": SOL, "kaldirac": 30}) == [SetLeverage(SOL, 30)]


def test_order_type_is_what_opus_names():
    """Emir türü artık tahmin edilmez, Opus'un adı Binance türüne birebir döner."""
    base = {"tip": "emir_ver", "sembol": SOL, "yon": "long", "fiyat": 95, "marjin_usd": 10, "gecerlilik_dk": 30}
    for tur, type_ in (("limit", OrderType.LIMIT), ("kirilim", OrderType.STOP),
                       ("geri_cekilme", OrderType.TAKE_PROFIT)):
        order = cmds({**base, "tur": tur})[0]
        assert order.type is type_ and order.price == 95 and order.valid_min == 30


def test_margin_slightly_over_available_is_fitted_and_told():
    """Kıl payı aşım düzeltme çağrısına mal olmaz, miktar küçülür ve Opus bunu sonuçta görür. Büyük aşım ise
    kullanılabilir bakiye yazılarak reddedilir, sessizce küçük bir işleme dönüşmez."""
    t = translate({"tip": "piyasa_giris", "sembol": SOL, "yon": "short", "marjin_usd": 99.5})
    assert t.item[0].qty == 19.8 and "19,9 SOL yerine 19,8 SOL" in t.note
    with pytest.raises(ValueError, match="Kullanılabilir bakiye 100,00 dolar"):
        translate({"tip": "piyasa_giris", "sembol": SOL, "yon": "short", "marjin_usd": 120})
    with pytest.raises(ValueError, match="Kullanılabilir bakiye 100,00 dolar"):
        translate({"tip": "piyasa_giris", "sembol": SOL, "yon": "short", "marjin_usd": 103})


def test_limit_above_mark_counts_its_open_loss():
    """Mark fiyatının aleyhindeki limit giriş aradaki açık zararı da ayırır. Sığdırma bunu saymazsa bakiyeye
    sığdı sanılan emri broker reddeder ve boşa bir düzeltme çağrısı gider."""
    with pytest.raises(ValueError, match="Kullanılabilir bakiye 100,00 dolar"):
        translate({"tip": "emir_ver", "sembol": SOL, "yon": "long", "tur": "limit", "fiyat": 102, "marjin_usd": 90})


def test_reserved_margin_of_pending_entries_is_not_available():
    """Bekleyen limit girişin ayırdığı marjin kullanılabilir değildir, sığdırma onu düşerek hesaplar."""
    limit = Order(7, 1, 3, SOL, Side.LONG, OrderType.LIMIT, False, 1.0, 95, None, None, 60, (), None, NOW)
    state = AccountState(1, 100, 100, (POS,), (limit,))
    with pytest.raises(ValueError, match="Kullanılabilir bakiye 40,00 dolar"):
        translate({"tip": "piyasa_giris", "sembol": SOL, "yon": "short", "marjin_usd": 50}, state)


def test_single_tp_and_levels_together_are_refused():
    """İkisi birlikte verilirse hangisinin istendiği bilinmez. Biri sessizce seçilse Opus'un niyeti bozulur."""
    with pytest.raises(ValueError, match="birlikte verilemez"):
        translate({"tip": "piyasa_giris", "sembol": SOL, "yon": "long", "marjin_usd": 10, "tp": 110,
                   "tp_kademeleri": [{"fiyat": 120, "yuzde": 100}]})


def test_guards_on_a_share_of_the_position_carry_their_qty():
    """Pozisyonun bir payına konan korumalar o payın miktarını taşır. Tamamına konan son kademe miktarsızdır ve
    tetik anında kalanın tamamını kapatır."""
    half = cmds({"tip": "koruma_ekle", "sembol": SOL, "yon": "long", "yuzde": 50, "sl": 97,
                 "tp_kademeleri": [{"fiyat": 110, "yuzde": 50}, {"fiyat": 115, "yuzde": 50}]})
    assert [(c.type, c.price, c.qty, c.reduce) for c in half] == [
        (OrderType.TAKE_PROFIT, 110, 0.5, True), (OrderType.TAKE_PROFIT, 115, 0.5, True),
        (OrderType.STOP, 97, 1.0, True)]
    whole = cmds({"tip": "koruma_ekle", "sembol": SOL, "yon": "long",
                  "tp_kademeleri": [{"fiyat": 110, "yuzde": 50}, {"fiyat": 115, "yuzde": 50}]})
    assert [(c.price, c.qty) for c in whole] == [(110, 1.0), (115, None)]


def test_changing_a_guard_places_the_new_one_before_cancelling_the_old():
    """Önce iptal edilseydi yeni emir reddedildiğinde pozisyon korumasız kalırdı. Miktar korunur."""
    assert cmds({"tip": "koruma_degistir", "numara": 2, "sembol": SOL, "yon": "long", "fiyat": 96}) == [
        PlaceOrder(SOL, Side.LONG, OrderType.STOP, qty=1.0, price=96, reduce=True), CancelOrder(Ref(2, SOL, Side.LONG))]
    with pytest.raises(ValueError, match="#9 numaralı SOLUSDT long emri yok"):
        translate({"tip": "koruma_degistir", "numara": 9, "sembol": SOL, "yon": "long", "fiyat": 96})


def test_limit_price_change_is_an_amend():
    """Limit emrin fiyatı tek adımda değişir, emir sırasını ve ayırdığı marjini kaybetmez."""
    assert cmds({"tip": "emir_degistir", "numara": 4, "sembol": SOL, "yon": "long", "fiyat": 96}) == [
        AmendOrder(Ref(4, SOL, Side.LONG), 96)]


def test_amending_a_guard_points_to_the_guard_action():
    """Broker'ın cümlesi panel içindir ve Opus'u doğru eyleme götürmez. Çeviri koruma emrini tanıyıp yolu söyler."""
    with pytest.raises(ValueError, match="#2 bir koruma emri, seviyesi koruma_degistir ile değişir"):
        translate({"tip": "emir_degistir", "numara": 2, "sembol": SOL, "yon": "long", "fiyat": 96})


def test_schema_requires_symbol_and_side_on_every_numbered_action():
    """Şema bu alanları zorunlu tutmazsa Opus onları atlayabilir ve kapı kontrolü boşa çıkar."""
    for option in actions.schema([SOL])["anyOf"]:
        if "numara" in option["properties"]:
            assert {"numara", "sembol", "yon"} <= set(option["required"])


# --- Talimat ve istem ---

def _system(**kw) -> str:
    args = dict(balance=10, specs=[catalog().get(SOL)], fees=FeeSchedule(taker=0.0004, maker=0.0001), style="",
                min_wait=15, max_wait=180, step_limit=25, structure=("4h", "1h"), fit_regimes=("duzenli_trend",),
                now=NOW)
    return prompt.system_prompt(**{**args, **kw})


def test_system_prompt_has_no_forbidden_words_and_uses_real_fees():
    """Talimatta deney, sanal para ya da dönem geçmez. Komisyon oranı hesabın kendi oranından gelir."""
    text = _system()
    lower = text.lower()
    for word in ("deney", "sanal", "dönem", "gerçek para", "düşünce süreci", "iç akıl yürütme"):
        assert word not in lower
    # Eski talimatın ifadeleri. Biri geri gelirse Opus kaldırılan bir kuralı okur.
    for old in ("istediğin kadar pozisyon", "pozisyonların ve bekleyen emirlerin aynı numaralandırmayı",
                "pozisyonun yüzdesi", "açıldığından beri", "nakit, emir gerçekleştiği anda",
                "tek bir dolar bakiyen var", "lazım olacak her şeyi yaz", "küçük bir kârla yetinmek", "guven"):
        assert old not in lower
    assert "Hesabının tek bir bakiyesi vardır, dolar cinsindendir." in text
    assert "yüzde 0.04 öder" in text and "yüzde 0.01 öder" in text
    assert "SOLUSDT | SOL | 100x | 5 dolar | 0.01 | 0.01 | yüzde 0.02 | yüzde 3 | yüzde 1.5" in text


def test_system_prompt_sections_follow_the_core_order():
    """Opus önce nasıl karar vereceğini okur, Binance mekaniği sona iner."""
    text = _system(style="TARZ")
    heads = ["## Hedefin", "## Öncelik sırası", "## Nasıl karar verirsin", "## Çalışma tarzın", "## Not defteri",
             "## Yönetici", "## Araçların", "## Hesabın kuralları", "## Cevap"]
    places = [text.index(h) for h in heads]
    assert places == sorted(places)


def test_system_prompt_takes_intervals_and_regimes_from_the_profile():
    """Zaman dilimleri ve uygun rejimler profilin ayarıdır, talimattaki tanım onlardan kurulur."""
    text = _system(structure=("1d", "4h"), fit_regimes=("duzenli_trend", "yatay"))
    assert "Düzenli trend: günlük grafikte" in text and "4 saatlik grafik bu yönde" in text
    assert "Stratejin şu rejimlerde çalışır: düzenli trend, yatay." in text


def test_us_release_times_are_in_the_core_not_the_prompt():
    """Hiç değişmeyen bilgi talimattadır, istem kısalır."""
    assert "ABD'nin ekonomik verileri ve Fed kararları açıklandığı günlerde" in _system()
    assert "ABD'nin" not in prompt.build_prompt(_ctx(), "KARTLAR")


def _ctx(**kw) -> Context:
    state = kw.pop("state", AccountState(1, 10.0, 10.0, (), ()))
    return Context(now=NOW, wakes=kw.pop("wakes", []), state=state, prices={SOL: 100.0}, symbols=(SOL,), market=None,
                   start_balance=10.0, **kw)


def test_correction_section_separates_applied_and_rejected():
    """Düzeltme çağrısında Opus kabul edilenleri tekrar verirse aynı işlem iki kez açılır."""
    results = [ActionResult("long_ac SOLUSDT", True, "açıldı"),
               ActionResult("pozisyon_kapat #4 SOLUSDT", False, "#4 numaralı açık pozisyon yok.")]
    data = {"rejected": False, "results": results, "left": 1}
    text = prompt.build_prompt(_ctx(wakes=[Wake("correction", NOW, data)]), "KARTLAR")
    assert "tekrar verme" in text and "1 düzeltme hakkın kaldı." in text
    assert "long_ac SOLUSDT: uygulandı" in text and "pozisyon_kapat #4 SOLUSDT: reddedildi" in text


def test_rejected_decision_correction_says_nothing_was_applied_and_rights_left():
    """Kontrol hatasında kararın hiç uygulanmadığını bilmeyen Opus yarım bir kararı tamamlamaya çalışır."""
    data = {"rejected": True, "results": [ActionResult("kontrol", False, "Kontrol: adım 2. Neden: çelişki.")],
            "left": 0}
    text = prompt.build_prompt(_ctx(wakes=[Wake("correction", NOW, data)]), "KARTLAR")
    assert "hiçbir eylemin uygulanmadı" in text and "Bu son düzeltme hakkın." in text
    assert "- Kontrol: adım 2. Neden: çelişki." in text


def test_alert_set_by_admin_is_not_told_as_opus_own():
    """Yöneticinin kurduğu alarm Opus'unmuş gibi anlatılırsa Opus kendi notu sanıp yanlış karar verir."""
    alert = Alert(2, SOL, 95.0, True, "destek", "admin", NOW, "admin")
    text = prompt.build_prompt(_ctx(alerts=[alert]), "KARTLAR")
    assert "alarm #2 SOLUSDT 95.00, not: destek, yönetici kurdu" in text


def test_account_shows_the_binance_model():
    """Opus kullanılabilir bakiyeyi, parite kaldıracını, numaralı ve miktarlı korumaları, izin bugünkü seviyesini
    ve bekleyen girişin türünü görmezse talimattaki kurallara göre karar veremez. SL'siz kalan miktar ayrıca
    söylenir, giriş başına SL'de bir girişin korumasız kaldığı buradan görünür."""
    trail = Order(7, 1, 3, SOL, Side.LONG, OrderType.TRAILING_STOP, True, 0.5, None, 1.5, 104, 0, (), None, NOW)
    limit = Order(8, 1, 4, SOL, Side.SHORT, OrderType.STOP, False, 1.0, 90, None, None, 0,
                  (Protection(OrderType.STOP, 95),), None, NOW)
    state = AccountState(1, 100, 100, (POS,), (TP_ORDER, SL_ORDER, trail, limit), {SOL: 25})
    text = prompt.build_prompt(_ctx(state=state), "KARTLAR")
    assert "Kullanılabilir bakiye: 100.00 dolar" in text and "Paritelerin kaldıracı: SOLUSDT 25x" in text
    assert "- SOLUSDT long, miktar 2," in text
    assert "  - #1 TP 110.00 miktar kalanın tamamı" in text and "  - #2 SL 95.00 miktar 1" in text
    assert "#3 iz yüzde 1.5 miktar 0.5, gördüğü en iyi mark fiyatı 104.00, seviyesi 102.44" in text
    assert "SL ya da izle korunmayan miktar 0.5." in text
    assert ("- #4 SOLUSDT short kırılım 90.00 miktar 1, gerçekleşince kurulacak koruma: SL 95.00 miktar girişin "
            "tamamı") in text


def test_closed_event_is_told_with_its_reason_and_net():
    """SL ile kapanış Opus'a nedeni ve net sonucuyla anlatılır, çağrılma nedenini anlaması buna bağlı."""
    t = Trade(1, 1, 1, SOL, Side.LONG, 1, 10, 100, NOW, 97.98, NOW, "sl", -2.0, 0.1, 0.0, 101, 97)
    e = AccountEvent(EventKind.CLOSED, 1, NOW, Actor.SYSTEM, SOL, Side.LONG, price=97.98, trade=t, reason="sl")
    assert prompt.wake_text(Wake("account", NOW, e), NOW) == \
        "SOLUSDT long pozisyonundan 1 SOL 97,98 seviyesinde SL ile kapandı, net -2,10 dolar."


def test_wake_texts_name_the_order_by_its_type():
    """Opus uyandığında hangi emrin ne olduğunu numara ve türüyle görür. Stop hem kırılım girişi hem SL olabildiği
    için kapatan emir olup olmadığı ayrıca okunur. Kurulamayan koruma pozisyonun adıyla anlatılır, numarası yoktur."""
    ev = lambda kind, **kw: AccountEvent(kind, 1, NOW, Actor.SYSTEM, SOL, Side.LONG, **kw)
    filled = ev(EventKind.ORDER_FILLED, number=4, price=95.0, qty=1.0, order_type=OrderType.STOP)
    assert prompt.wake_text(Wake("account", NOW, filled), NOW).startswith(
        "#4 SOLUSDT long kırılım giriş emri 1 SOL, 95,00 fiyatından gerçekleşti.")
    guard = ev(EventKind.ORDER_FAILED, price=94.0, reason="Fiyat seviyeyi geçti.", order_type=OrderType.STOP,
               reduce=True)
    assert prompt.wake_text(Wake("account", NOW, guard), NOW) == \
        "SOLUSDT long pozisyonunun 94,00 SL emri kurulamadı: Fiyat seviyeyi geçti."
    entry = ev(EventKind.ORDER_FAILED, number=4, reason="Yetersiz bakiye.", order_type=OrderType.TAKE_PROFIT)
    assert prompt.wake_text(Wake("account", NOW, entry), NOW) == \
        "#4 SOLUSDT long geri çekilme giriş emri tetiklendi ama açılamadı: Yetersiz bakiye."
    t = Trade(1, 1, 1, SOL, Side.LONG, 1, 10, 100, NOW, 110, NOW, "tp", 10, 0.1, 0.0, 110, 99)
    closed = ev(EventKind.CLOSED, number=1, price=110.0, trade=t, reason="tp", order_type=OrderType.TAKE_PROFIT,
                reduce=True)
    assert prompt.wake_text(Wake("account", NOW, closed), NOW) == \
        "SOLUSDT long pozisyonundan 1 SOL 110,00 seviyesinde TP ile kapandı, kapatan emir #1 TP, net +9,90 dolar."


# --- Strateji ---

class FakeLLM:
    def __init__(self, decision):
        self.decision = decision
        self.requests = []

    async def call(self, req, on_tool=None):
        self.requests.append(req)
        return LLMResult(self.decision, "{}", [], {}, 1.0, 0.1)


PLAN = {"metin": "Yükselişte 1 saatlik ilk yükselen dipten long.", "bozulma": "95 altı", "gecerlilik": "01.01.2030 00:00"}


def _k(eylemler=(), pozisyonlar=(), **over) -> dict:
    """Kontrolleri tutan bir karar. 4 saatlik ve 1 saatlik yükseliyor, uyum long yönünde."""
    k = {
        "defter_sinamasi": {"plan": {"durum": "bozuldu", "gerekce": "Yeni."}, "yapi": {"durum": "ayni", "gerekce": "x"},
                            "gundem": {"durum": "etkisi_yok", "gerekce": "x"}, "emirler": [],
                            "pozisyonlar": list(pozisyonlar)},
        "pariteler": {SOL: {"yapi": {"ust": {"yon": "yukselen", "tepeler": [100, 104], "dipler": [90, 94]},
                                     "alt": {"yon": "yukselen", "tepeler": [101, 103], "dipler": [97, 98]}},
                            "rejim": {"rejim": "duzenli_trend", "kanit": "x", "uygun": "evet",
                                      "bekledigi": {"tur": "yok", "fiyat": 0, "saat": ""}}}},
        "kapanan_islemler": [], "analiz": "", "eylemler": list(eylemler),
        "not_defteri": {"plan": PLAN, "seviyeler_notu": "98 destek.", "gundem": ""},
        "ozet": "özet", "sonraki_cagri_dk": 1, "yoneticiye_cevap": "", "yoneticiye_oneri": "",
    }
    k.update(over)
    return k


ENTRY = {"tip": "piyasa_giris", "sembol": SOL, "yon": "long", "marjin_usd": 10, "sl": 97.5,
         "karsi_kanit": "Fonlama pozitif ve long kalabalık, ama açık pozisyon artmıyor.",
         "risk_beyani": {"yuzde": 6, "gerekce": "Yapı net."}, "gecersizlik": {"metin": "97 altı kapanış", "fiyat": 97}}
EMPTY = AccountState(1, 100, 100, (), ())


def _decide(monkeypatch, tmp_path, k, state=STATE, memory=None):
    async def cards(*a):
        return "KARTLAR"
    monkeypatch.setattr(llm_strategy, "cards_text", cards)
    fake = FakeLLM(k)
    s = llm_strategy.LLMStrategy(fake, catalog(), tmp_path, {}, tmp_path / "m.db", "", "opus", "high", 15, 180, 25, 600)
    return asyncio.run(s.decide(_ctx(state=state, memory=memory or {}))), fake


def test_decide_turns_actions_into_commands_alarms_and_errors(monkeypatch, tmp_path):
    """Kararın her parçası doğru yere gider: komut kapıya, alarm tetikleyiciye, çeviride takılan eylem düzeltmeye.
    Bir eylemin ikinci komutu birinciye zincirlenir, birincisi reddedilirse gönderilmez."""
    k = _k(eylemler=[{"tip": "koruma_degistir", "numara": 2, "sembol": SOL, "yon": "long", "fiyat": 96},
                     {"tip": "alarm_kur", "sembol": SOL, "fiyat": 90, "not": "bak"},
                     {**ENTRY, "tp": 110, "tp_kademeleri": [{"fiyat": 120, "yuzde": 100}]}],
           pozisyonlar=[{"sembol": SOL, "yon": "long", "durum": "gecerli", "gerekce": "x"}])
    d, fake = _decide(monkeypatch, tmp_path, k)
    assert d.rejection is None
    assert d.commands == [PlaceOrder(SOL, Side.LONG, OrderType.STOP, qty=1.0, price=96, reduce=True),
                          CancelOrder(Ref(2, SOL, Side.LONG))]
    assert d.chained == [False, True] and d.annotations == [None, None]
    assert d.alarms[0]["op"] == "add" and d.alarms[0]["price"] == 90
    assert not d.errors[0].ok and "birlikte verilemez" in d.errors[0].message
    assert d.wait_s == 15 * 60 and d.record["template_version"] == prompt.TEMPLATE_VERSION
    assert d.memory["notebook"]["plan"] == PLAN and d.memory["notebook_at"] == NOW
    assert d.memory["notebook"]["yapi"]["rakamlar"][SOL]["1h"]["dipler"] == [97, 98]
    assert fake.requests[0].max_turns == 30 and "defter_sinamasi" in fake.requests[0].schema["properties"]
    assert d.record["badge"][SOL]["structure"] == [{"interval": "4h", "dir": "yukselen"},
                                                   {"interval": "1h", "dir": "yukselen"}]


def test_good_entry_carries_its_note_and_a_second_one_is_refused(monkeypatch, tmp_path):
    """Kontrolden geçen girişin notu komutuna bağlanır. Aynı kararda aynı yönde ikinci giriş eklemeye takılır."""
    d, _ = _decide(monkeypatch, tmp_path, _k(eylemler=[ENTRY, ENTRY]), state=EMPTY)
    assert len(d.commands) == 1 and d.annotations[0]["invalid_price"] == 97
    assert d.annotations[0]["reason"]["risk_beyani"]["yuzde"] == 6
    assert "aynı yönde ekleme" in d.errors[0].message


def test_control_error_rejects_everything_and_keeps_the_notebook(monkeypatch, tmp_path):
    """Kontrol hatasında komut, alarm ve hafıza yoktur. Düzeltme hakkı biterse en az 15 dakika beklenir."""
    k = _k(eylemler=[ENTRY])
    k["pariteler"][SOL]["yapi"]["alt"]["dipler"] = [98, 97]
    d, _ = _decide(monkeypatch, tmp_path, k, state=EMPTY)
    assert d.rejection and "adım 2" in d.rejection[0]
    assert (d.commands, d.alarms, d.memory) == ([], [], {})
    assert d.wait_s == 15 * 60 and d.record["rejection"] == d.rejection


def test_unfit_regime_sets_an_expectation_alarm_or_shortens_the_wait(monkeypatch, tmp_path):
    """Beklenen fiyata sistem alarm kurar, beklenen saat bir sonraki çağrının en geç zamanı olur."""
    k = _k(sonraki_cagri_dk=180)
    k["pariteler"][SOL]["rejim"] = {"rejim": "asiri_uzama", "kanit": "x", "uygun": "hayir",
                                   "bekledigi": {"tur": "fiyat", "fiyat": 92.5, "saat": ""}}
    d, _ = _decide(monkeypatch, tmp_path, k, state=EMPTY)
    assert d.alarms == [{"op": "expect", "symbol": SOL, "price": 92.5, "label": f"beklenti {SOL}",
                         "note": "Rejimi uygun bulmadığın çağrıda beklediğin fiyat."}]
    assert d.record["badge"][SOL] == {"structure": d.record["badge"][SOL]["structure"], "regime": "aşırı uzama",
                                      "fit": False, "waiting": {"price": 92.5}, "hint": d.record["badge"][SOL]["hint"]}
    k["pariteler"][SOL]["rejim"]["bekledigi"] = {"tur": "saat", "fiyat": 0, "saat": "01.01.2030 00:00"}
    d, _ = _decide(monkeypatch, tmp_path, k, state=EMPTY)
    assert d.wait_s == 180 * 60
    from opus_trade.strategy.llm.notebook import parse_until
    soon = NOW + 1800
    from opus_trade.strategy.llm.texts import local
    k["pariteler"][SOL]["rejim"]["bekledigi"]["saat"] = local(soon).strftime("%d.%m.%Y %H:%M")
    d, _ = _decide(monkeypatch, tmp_path, k, state=EMPTY)
    assert d.wait_s == parse_until(k["pariteler"][SOL]["rejim"]["bekledigi"]["saat"]) - NOW


def test_valid_plan_is_carried_and_reviews_and_suggestion_are_passed_on(monkeypatch, tmp_path):
    """Geçerli denen plan taşınır ve bu Opus'a söylenir. Sınıflandırma ve öneri çalıştırıcıya geçer."""
    old = {"notebook": {"plan": PLAN, "yapi": {"seviyeler_notu": "", "rakamlar": {}}, "gundem": ""}}
    k = _k(yoneticiye_oneri="Girişlerim erken kalıyor.")
    k["defter_sinamasi"]["plan"]["durum"] = "gecerli"
    k["not_defteri"]["plan"] = {**PLAN, "metin": "Başka."}
    d, _ = _decide(monkeypatch, tmp_path, k, state=EMPTY, memory=old)
    assert d.memory["notebook"]["plan"] == PLAN and "eski plan aynen taşındı" in d.notes[0].message
    assert d.suggestion == "Girişlerim erken kalıyor." and d.record["suggestion"] == d.suggestion


def test_structure_must_have_a_longer_upper_interval(tmp_path):
    """Üst aralık alt aralıktan uzun değilse uyum tanımsız kalır, profil kurulmaz."""
    with pytest.raises(ValueError, match="üst aralığı"):
        llm_strategy.LLMStrategy(None, catalog(), tmp_path, {}, tmp_path, "", "opus", "high", 15, 180, 25, 600,
                                 structure_high="1h", structure_low="4h")


# --- claude istemcisi ---

FAKE = '''import json, sys
mode = sys.argv[sys.argv.index("--model") + 1]
sys.stdin.read()
out = lambda m: print(json.dumps(m), flush=True)
out({"type": "system", "session_id": "s1"})
out({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "mcp__piyasa__mumlar",
     "input": {"sembol": "BTCUSDT"}}]}})
out({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "mum listesi"}]}})
if mode == "ok":
    out({"type": "result", "structured_output": {"ozet": "tamam"}, "usage": {}, "total_cost_usd": 0.2})
elif mode == "turns":
    out({"type": "result", "subtype": "error_max_turns"})
else:
    print("bir hata oldu", file=sys.stderr)
'''


def _client(tmp_path) -> ClaudeCliClient:
    script = tmp_path / "claude"
    script.write_text(f"#!{sys.executable}\n" + FAKE)
    script.chmod(0o755)
    return ClaudeCliClient(Path(sys.executable), tmp_path, claude=str(script))


def _req(tmp_path, model: str) -> LLMRequest:
    return LLMRequest("talimat", "istem", {"type": "object"}, model, "high", 30, tmp_path / "work", timeout_s=20)


def test_client_reads_decision_and_tool_results(tmp_path):
    """Araç sonuçları kayda girmezse Opus'un neye bakarak karar verdiği sonradan incelenemez."""
    seen = []
    res = asyncio.run(_client(tmp_path).call(_req(tmp_path, "ok"), seen.append))
    assert res.decision == {"ozet": "tamam"} and res.cost_usd == 0.2
    assert res.tools[0]["name"] == "piyasa.mumlar" and res.tools[0]["result"] == "mum listesi"
    assert seen[0]["title"] == "sembol=BTCUSDT"


@pytest.mark.parametrize("mode, text", [("turns", "adım sınırına ulaştı"), ("crash", "bir hata oldu")])
def test_client_failures_keep_the_tools_used_so_far(tmp_path, mode, text):
    """Başarısız çağrının da araç kaydı kalmalı, limitin neye harcandığı görünsün."""
    with pytest.raises(LLMError, match=text) as e:
        asyncio.run(_client(tmp_path).call(_req(tmp_path, mode)))
    assert e.value.tools[0]["name"] == "piyasa.mumlar"


def test_command_gives_no_file_or_shell_tools(tmp_path):
    """Opus'a dosya, terminal ya da kod çalıştırma aracı verilmez. Bir değişiklik bunu açarsa sunucu tehlikeye girer."""
    cmd = _client(tmp_path).command(_req(tmp_path, "ok"))
    assert cmd[cmd.index("--tools") + 1] == "WebSearch,WebFetch,Agent"
    assert cmd[cmd.index("--allowedTools") + 1] == "WebSearch,WebFetch,Agent,mcp__piyasa"
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == {"type": "object"}


# --- İstemin bölümleri ---

def test_prompt_sections_put_yesterdays_thinking_before_todays_market():
    """Opus eski defterini okur, en son ve en taze bilgi olarak piyasayı okur, sınamayı bu veriyle yapar."""
    from opus_trade.strategy.base import Facts
    ctx = _ctx(facts=Facts(admin_24h=("21.11 10:00: SOLUSDT long pozisyonu açıldı.",)),
               history=[{"time": NOW - 600, "summary": "bekledim", "results": [],
                         "regimes": {SOL: ("donus", "hayir")}, "rejected": False}])
    text = prompt.build_prompt(ctx, "# Parite kartları")
    heads = ["# Çağrı", "# Yöneticiden", "# Not defterin", "# Son kararların", "# Hesap", "# Piyasa saatleri",
             "# Parite kartları"]
    places = [text.index(h) for h in heads]
    assert places == sorted(places)
    assert "Yöneticinin son 24 saatte hesabında yaptıkları\n\n- 21.11 10:00: SOLUSDT long" in text
    assert "SOLUSDT dönüş · uygun değil · bekledim" in text


def test_blind_time_is_told_at_the_top():
    """Sistem kapalı kaldıysa Opus bunu ilk satırlarda görür, eski planı kör kaldığı süreyi bilmeden sürdürmez."""
    from opus_trade.strategy.base import Facts
    text = prompt.build_prompt(_ctx(facts=Facts(last_call=NOW - 3 * 3600, late_min=150)), "K")
    head = text.split("# Not defterin")[0]
    assert "Son çağrın 3 saat 0 dakika önceydi" in head and "150 dakika geç" in head
    assert "Son çağrın" not in prompt.build_prompt(_ctx(), "K")


def test_account_shows_ready_facts_notes_and_who_opened():
    """Opus saymaz, yorumlar. Pozisyonun tezi ve yöneticinin açtığı pozisyon hesap bölümünde görünür."""
    from opus_trade.strategy.base import ClosedPosition, Facts, Thesis
    thesis = Thesis(SOL, "long", {"risk_beyani": {"yuzde": 3, "gerekce": "Yapı net."}}, 95.0, "95 altı kapanış", NOW)
    closed = ClosedPosition(11, SOL, "long", -0.4, "sl", NOW - 60, "strategy", True)
    facts = Facts(today_count=2, today_stops=1, today_net=-0.3, loss_streak=1, recent=(closed,), fill_rate=(2, 10),
                  position_origins={1: "admin"}, unreviewed=(closed,), position_notes={1: (thesis,)},
                  invalidated=((thesis, NOW - 30),))
    text = prompt.build_prompt(_ctx(state=STATE, facts=facts), "K")
    assert "- SOLUSDT long, yönetici açtı, miktar 2" in text
    assert "Giriş notu, " in text and "risk beyanı yüzde 3,00, geçersizlik 95.00: 95 altı kapanış Gerekçe: Yapı net." in text
    assert "Bugün: 2 pozisyon kapandı, 1 tanesi zararla stoplandı, günün neti -0.30 dolar." in text
    assert "Son 10 bekleyen giriş emrinden 2 tanesi doldu, 8 tanesi" in text
    assert "Sınıflandırılmayı bekleyen pozisyonlar, kapanan_islemler alanında:\n- #11 SOLUSDT long" in text
    assert "sistemin iptal ettiği girişler:\n- SOLUSDT long, geçersizlik 95.00" in text


def test_market_hours_fit_one_line():
    """Piyasa saatleri tek satırdır: açık borsalar ve sıradaki iki açılış."""
    from datetime import datetime, timezone
    from opus_trade.strategy.llm.hours import hours_text
    text = hours_text(datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc))
    body = text.split("\n\n", 1)[1]
    assert body.startswith("Açık: ") and "Sıradaki açılışlar:" in body and "\n" not in body


def test_multi_param_keeps_choice_order_and_needs_one():
    """Uygun rejimler birden çok seçimdir. Boş seçim stratejiyi hiçbir rejimde çalışmaz hale getirirdi."""
    from opus_trade.registry import Param, validate
    p = Param("fit", "multi", "Uygun rejimler", default=("a",), choices=("a", "b", "c"))
    assert validate((p,), {"fit": ["c", "a"]}) == {"fit": ("a", "c")}
    assert validate((p,), {}) == {"fit": ("a",)}
    with pytest.raises(ValueError, match="en az bir seçim"):
        validate((p,), {"fit": []})
    with pytest.raises(ValueError, match="Geçersiz: d"):
        validate((p,), {"fit": ["d"]})
