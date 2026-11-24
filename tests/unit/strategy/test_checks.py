"""
file: test_checks.py
importable: yok

Kontrol adımlarının testleri. Bir kontrol geçmesi gereken kararı reddederse
Opus düzeltme çağrılarında limiti harcar, takılması gereken kararı geçirirse
kırmızı çizgi yoktur. Cumartesi 21.11'in üç hatalı kararı sabit örnek olarak
burada: 10:54'teki yapı okuması 2. adımda, 09:41'deki ikinci giriş eklemede
ve risk beyanında, 15:32'deki giriş 3. adımda takılır. Rakamlar o günün
grafiğindeki gibi BTCUSDT seviyeleridir.

description: strategy.llm.checks için birim testleri.
"""

import copy
from datetime import datetime

import pytest

from opus_trade.domain.clock import HOME_TZ
from opus_trade.domain.fees import BINANCE_STANDARD as FEES
from opus_trade.domain.models import Side
from opus_trade.domain.trading import AccountState, Order, OrderType, PlaceOrder, Position
from opus_trade.strategy.base import ClosedPosition, Context, Facts
from opus_trade.strategy.llm.steps.base import Steps
from opus_trade.strategy.llm.steps.book import BookStep
from opus_trade.strategy.llm.steps.closed import ClosedStep
from opus_trade.strategy.llm.steps.entry import RISK_SLACK, EntryStep
from opus_trade.strategy.llm.steps.structure import StructureStep
from opus_trade.strategy.llm.notebook import Notebook

NOW = int(datetime(2026, 11, 21, 10, 54, tzinfo=HOME_TZ).timestamp())
BTC = "BTCUSDT"
STRUCT = ("4h", "1h")
FIT = ("duzenli_trend",)
PLAN = {"metin": "Düşüşte ilk alçalan tepeden short.", "bozulma": "62615 üstü", "gecerlilik": "21.11.2026 18:00"}


def _level(yon, tops, lows):
    return {"yon": yon, "tepeler": list(tops), "dipler": list(lows)}


def decision(**over) -> dict:
    k = {
        "defter_sinamasi": {"plan": {"durum": "gecerli", "gerekce": "Yapı sürüyor."},
                            "yapi": {"durum": "ayni", "gerekce": "Aynı."},
                            "gundem": {"durum": "etkisi_yok", "gerekce": "Yok."}, "emirler": [], "pozisyonlar": []},
        "pariteler": {BTC: {"yapi": {"ust": _level("alcalan", (62615, 62320), (60100, 59800)),
                                     "alt": _level("alcalan", (61200, 60900), (60300, 60050))},
                            "rejim": {"rejim": "duzenli_trend", "kanit": "4 saatlikte alçalan tepeler, RSI 42.",
                                      "uygun": "evet", "bekledigi": {"tur": "yok", "fiyat": 0, "saat": ""}}}},
        "kapanan_islemler": [],
        "analiz": "",
        "eylemler": [],
        "not_defteri": {"plan": PLAN, "seviyeler_notu": "62320 direnç.", "gundem": ""},
        "ozet": "", "sonraki_cagri_dk": 30, "yoneticiye_cevap": "", "yoneticiye_oneri": "",
    }
    for path, value in over.items():
        node = k
        keys = path.split("__")
        for x in keys[:-1]:
            node = node[x]
        node[keys[-1]] = value
    return k


def _position(side=Side.SHORT, entry=60500.0, qty=0.002, margin=6.05, pid=7) -> Position:
    return Position(pid, 1, BTC, side, qty, margin, entry, NOW - 3600, entry, NOW, entry, NOW, 70000, 0, 0)


def _order(number, side=Side.SHORT, price=60800.0, reduce=False, type_=OrderType.LIMIT, qty=0.002, oid=None):
    return Order(oid or number + 100, 1, number, BTC, side, type_, reduce, qty, price, None, None,
                 0 if reduce else 6.08, (), None, NOW - 600)


def ctx(positions=(), orders=(), facts=None, equity=10.0) -> Context:
    state = AccountState(1, equity, equity, tuple(positions), tuple(orders))
    return Context(now=NOW, wakes=[], state=state, prices={BTC: 60600.0}, symbols=(BTC,), market=None,
                   facts=facts or Facts())


STEPS = Steps([BookStep(), StructureStep(STRUCT, FIT), ClosedStep(), EntryStep(FEES)])


def validate(k, c, old=Notebook(plan=PLAN)):
    return STEPS.validate(k, c, old)


def entry(side=Side.SHORT, price=None, qty=0.002, sl=61000.0, risk=10.0, inv=61100.0, **over) -> tuple[dict, PlaceOrder]:
    a = {"tip": "emir_ver" if price else "piyasa_giris", "sembol": BTC, "yon": str(side), "marjin_usd": 6,
         "karsi_kanit": "Fonlama negatif, short kalabalık, bu yüzden sıkışma riski var ama hacim zayıf.",
         "risk_beyani": {"yuzde": risk, "gerekce": "Yapı net."}, "gecersizlik": {"metin": "Tepe aşılırsa.", "fiyat": inv},
         "sl": sl, **over}
    return a, PlaceOrder(BTC, side, OrderType.LIMIT if price else OrderType.MARKET, qty=qty, price=price)


def problem(a, cmd, k, c, busy=frozenset()):
    review = STEPS.validate(k, c, Notebook(plan=PLAN))
    assert review.errors == []
    return STEPS.entry_problem(a, cmd, k, review, c, 20, set(busy))


# --- Adım 1 ---

def test_clean_decision_passes_every_control():
    """Kontrolleri tutan bir karar reddedilmez, yoksa Opus her çağrıda boşuna düzeltme yapar."""
    r = validate(decision(), ctx())
    assert (r.errors, r.parts["structure"].fit, r.parts["structure"].aligned, r.parts["structure"].trend) == ([], {BTC: True}, {BTC: "short"}, {BTC: "short"})


def test_missing_and_extra_orders_and_positions_reject_the_decision():
    """Sınanmayan bir emir sessizce bayatlar. Hesapta olmayan bir emri sınamak da hesabı yanlış okumaktır."""
    c = ctx(positions=[_position()], orders=[_order(3), _order(4, reduce=True, type_=OrderType.STOP)])
    r = validate(decision(defter_sinamasi__emirler=[{"numara": 5, "sembol": BTC, "yon": "short", "durum": "gecerli",
                                                      "gerekce": "x"}]), c)
    assert any("#3 BTCUSDT short bekleyen giriş emri listede yok" in e for e in r.errors)
    assert any("#5 BTCUSDT short diye bekleyen bir giriş emrin yok" in e for e in r.errors)
    assert any("BTCUSDT short pozisyonu listede yok" in e for e in r.errors)
    assert not any("#4" in e for e in r.errors)


def test_broken_order_is_cancelled_and_broken_position_needs_a_close():
    """Bozuldu denen emri sistem iptal eder. Bozuldu denen pozisyon aynı kararda kapanmazsa beyan eylemle çelişir."""
    c = ctx(positions=[_position()], orders=[_order(3)])
    review = {"emirler": [{"numara": 3, "sembol": BTC, "yon": "short", "durum": "bozuldu", "gerekce": "x"}],
              "pozisyonlar": [{"sembol": BTC, "yon": "short", "durum": "bozuldu", "gerekce": "x"}]}
    k = decision(defter_sinamasi__emirler=review["emirler"], defter_sinamasi__pozisyonlar=review["pozisyonlar"])
    r = validate(k, c)
    assert [(x.number, x.symbol) for x in r.cancels] == [(3, BTC)]
    assert any("tezi bozuldu dedin ama" in e for e in r.errors)
    k["eylemler"] = [{"tip": "pozisyon_kapat", "sembol": BTC, "yon": "short"}]
    assert validate(k, c).errors == []


def test_admin_opened_position_may_stay_open_even_if_broken():
    """Yöneticinin açtığı pozisyonu Opus kapatmak zorunda değildir."""
    c = ctx(positions=[_position()], facts=Facts(position_origins={7: "admin"}))
    k = decision(defter_sinamasi__pozisyonlar=[{"sembol": BTC, "yon": "short", "durum": "bozuldu", "gerekce": "x"}])
    assert validate(k, c).errors == []


def test_broken_or_expired_plan_needs_a_new_plan():
    """Bozulan ya da süresi dolan plan aynen yeniden yazılamaz, geçerliliği geçmişte olamaz."""
    k = decision(defter_sinamasi__plan={"durum": "bozuldu", "gerekce": "62615 aşıldı."})
    assert any("aynı planı yeniden yazamazsın" in e for e in validate(k, ctx()).errors)
    old = Notebook(plan={**PLAN, "gecerlilik": "21.11.2026 09:00"})
    k = decision(not_defteri__plan={**PLAN, "gecerlilik": "21.11.2026 10:00"})
    errors = validate(k, ctx(), old).errors
    assert any("geçmişte kalıyor" in e for e in errors)


def test_valid_plan_rewritten_is_carried_with_a_note_not_rejected():
    """Geçerli denen planın metni değiştirilirse ret yok, eski metin taşınır ve bu Opus'a söylenir. Yalnızca
    geçerlilik saati değiştiyse not düşülmez, ama geçmişte kalan saat reddedilir."""
    r = validate(decision(not_defteri__plan={**PLAN, "metin": "Başka."}), ctx())
    assert r.errors == [] and "metni ve bozulma koşulu aynen taşındı" in r.notes[0]
    r = validate(decision(not_defteri__plan={**PLAN, "gecerlilik": "22.11.2026 06:00"}), ctx())
    assert r.errors == [] and r.notes == []
    r = validate(decision(not_defteri__plan={**PLAN, "gecerlilik": "21.11.2026 10:00"}), ctx())
    assert any("geçmişte kalıyor" in e for e in r.errors)


def test_realized_plan_needs_a_new_plan_but_not_a_new_date_only():
    """Altın 23.11'de beklediği kurulum oluşunca planı zayıfladı demek zorunda kaldı. Gerçekleşti denen plan
    bozuldu gibi yeni plan ister, aynı metni yalnızca saatini değiştirerek yazmak da kabul edilmez."""
    k = decision(defter_sinamasi__plan={"durum": "gerceklesti", "gerekce": "Geri çekilme 3607'de bitti."},
                 not_defteri__plan={**PLAN, "gecerlilik": "22.11.2026 06:00"})
    assert any("gerçekleşti dedin, aynı planı yeniden yazamazsın" in e for e in validate(k, ctx()).errors)
    k = decision(defter_sinamasi__plan={"durum": "gerceklesti", "gerekce": "Geri çekilme 3607'de bitti."},
                 not_defteri__plan={**PLAN, "metin": "Long taşınır, stop yeni dibin altına çekilir."})
    assert validate(k, ctx()).errors == []


# --- Adım 2 ve 3 ---

def test_saturday_1054_structure_reading_is_rejected_at_step_2():
    """21.11 10:54: 1 saatlik yön alçalan seçildi, yazılan dipler yükseliyordu. Beyan rakamla çelişir."""
    k = decision(pariteler__BTCUSDT__yapi__alt=_level("alcalan", (58400, 58210), (57352, 58103)))
    r = validate(k, ctx())
    assert r.errors[0] == ("Kontrol: adım 2, BTCUSDT 1 saatlik yapı. Neden: yönü alçalan seçtin ama yazdığın dipler "
                           "57352.00 ve 58103.00, yani alçalmıyor.") or "dipler 57352" in r.errors[0]


def test_flat_and_unclear_are_always_allowed():
    """Yatay ve belirsiz Opus'un okumasıdır, rakamlar yükselse de kod reddetmez."""
    k = decision(pariteler__BTCUSDT__yapi__ust=_level("yatay", (62000, 62500), (60000, 60500)),
                 pariteler__BTCUSDT__rejim={"rejim": "belirsiz", "kanit": "x", "uygun": "hayir",
                                            "bekledigi": {"tur": "fiyat", "fiyat": 60000, "saat": ""}})
    r = validate(k, ctx())
    assert r.errors == [] and r.parts["structure"].trend == {BTC: None} and r.parts["structure"].expectations == [(BTC, 60000.0)]


def test_regular_trend_needs_alignment_on_the_lower_interval():
    """Düzenli trend için alt aralıkta trend yönünde ilk yapı oluşmalı. Düşüşte alt aralığın tepesi alçalmalı."""
    k = decision(pariteler__BTCUSDT__yapi__alt=_level("yukselen", (60500, 60900), (60050, 60300)))
    r = validate(k, ctx())
    assert r.parts["structure"].aligned == {BTC: None} and any("uyum yok" in e for e in r.errors)


def test_fit_must_match_the_profiles_regimes_and_unfit_needs_what_it_waits_for():
    """Uygun denen rejim profilin listesinde olmalı. Uygun değilse beklenen fiyat ya da saat yazılmalı."""
    rejim = {"rejim": "asiri_uzama", "kanit": "x", "uygun": "evet", "bekledigi": {"tur": "yok", "fiyat": 0, "saat": ""}}
    assert any("stratejinin çalıştığı rejimlerden değil" in e
               for e in validate(decision(pariteler__BTCUSDT__rejim=rejim), ctx()).errors)
    rejim = {**rejim, "uygun": "hayir"}
    assert any("neyi beklediğini yazmadın" in e for e in validate(decision(pariteler__BTCUSDT__rejim=rejim), ctx()).errors)
    rejim["bekledigi"] = {"tur": "saat", "fiyat": 0, "saat": "21.11.2026 14:00"}
    r = validate(decision(pariteler__BTCUSDT__rejim=rejim), ctx())
    assert r.errors == [] and r.parts["structure"].wait_until == int(datetime(2026, 11, 21, 14, 0, tzinfo=HOME_TZ).timestamp())


def test_open_position_may_wait_for_nothing_but_still_cannot_enter():
    """Açık pozisyonu yöneten Opus rejim uygun değilken beklemediği bir hedef uydurmak zorunda kalmaz. Yine de o
    paritede giriş yapamaz, alarm ve saat de kurulmaz."""
    rejim = {"rejim": "asiri_uzama", "kanit": "x", "uygun": "hayir", "bekledigi": {"tur": "yok", "fiyat": 0, "saat": ""}}
    k = decision(pariteler__BTCUSDT__rejim=rejim, defter_sinamasi__pozisyonlar=[
        {"sembol": BTC, "yon": "short", "durum": "gecerli", "gerekce": "Tez sürüyor."}])
    c = ctx(positions=[_position()])
    r = validate(k, c)
    assert r.errors == [] and r.parts["structure"].expectations == [] and r.parts["structure"].wait_until is None
    a, cmd = entry()
    assert "giriş yapamazsın" in STEPS.entry_problem(a, cmd, k, r, c, 20, set())


def test_saturday_1532_entry_is_rejected_at_step_3():
    """21.11 15:32: rejim dönüş ve uygun değil okundu, yine de giriş verildi. Giriş reddedilir."""
    rejim = {"rejim": "donus", "kanit": "Son alçalan tepe aşıldı.", "uygun": "hayir",
             "bekledigi": {"tur": "fiyat", "fiyat": 59500, "saat": ""}}
    k = decision(pariteler__BTCUSDT__rejim=rejim)
    c = ctx()
    review = validate(k, c)
    a, cmd = entry()
    assert "adım 3" in STEPS.entry_problem(a, cmd, k, review, c, 20, set())


# --- Adım 4 ---

def test_closed_positions_are_classified_once_and_only_those_waiting():
    """Bekleyen her pozisyon bir kez sınıflandırılır. Fazlası ya da eksiği kayda yanlış öğrenme yazar."""
    waiting = Facts(unreviewed=(ClosedPosition(11, BTC, "short", -0.3, "sl", NOW - 60, "strategy", True),))
    assert any("#11" in e for e in validate(decision(), ctx(facts=waiting)).errors)
    k = decision(kapanan_islemler=[{"pozisyon_no": 11, "sinif": "tez_yanlisti", "aciklama": "x"},
                                   {"pozisyon_no": 12, "sinif": "tez_yanlisti", "aciklama": "x"}])
    assert any("#12" in e for e in validate(k, ctx(facts=waiting)).errors)
    k["kapanan_islemler"].pop()
    assert validate(k, ctx(facts=waiting)).errors == []


# --- Adım 5 ---

def test_good_entry_passes():
    """Kontrolleri tutan giriş reddedilmez."""
    a, cmd = entry()
    assert problem(a, cmd, decision(), ctx()) is None


def test_counter_evidence_cannot_be_empty():
    """Karşı kanıt yok demek arama yapılmadığı demektir."""
    a, cmd = entry(karsi_kanit="Yok.")
    assert "karşı kanıt" in problem(a, cmd, decision(), ctx())


def test_entry_against_the_upper_direction_is_rejected():
    """Opus 4 saatliği alçalan yazdıysa long girişi kendi beyanıyla çelişir."""
    a, cmd = entry(side=Side.LONG, sl=60000.0, inv=60010.0)
    assert "yön" in problem(a, cmd, decision(), ctx())


def test_invalidation_must_be_on_the_far_side():
    """Short'ta geçersizlik girişin üstünde olur, altında olursa giriş anında bozulmuş olur."""
    a, cmd = entry(inv=60000.0)
    assert "geçersizlik fiyatı girişin üstünde" in problem(a, cmd, decision(), ctx())


def test_after_a_stop_the_difference_must_be_written():
    """Aynı yönde son işlem stopla bittiyse yeni girişin farkı yazılır."""
    c = ctx(facts=Facts(last_stop={"BTCUSDT short": True}))
    a, cmd = entry()
    assert "son stoptan farkı" in problem(a, cmd, decision(), c)
    a["son_stoptan_farki"] = "Bu kez 1 saatlikte alçalan tepe oluştu."
    assert problem(a, cmd, decision(), c) is None


def test_entry_level_must_be_below_the_last_lower_top_and_the_stop_beyond_it():
    """Short giriş alt aralığın son tepesinin altında, SL onun üstünde. Önceki tepe stopun yeridir."""
    a, cmd = entry(price=61000.0, inv=61500.0, sl=61400.0)
    msg = problem(a, cmd, decision(), ctx())
    assert "giriş seviyesi" in msg and "stopunun yeridir" in msg
    a, cmd = entry(sl=60800.0)
    assert "Stop o tepenin ötesinde" in problem(a, cmd, decision(), ctx())


def test_saturday_0941_second_entry_is_rejected_for_adding():
    """21.11 09:41: aynı yönde bekleyen bir short varken ikinci short verildi. Ekleme kuralına takılır."""
    c = ctx(orders=[_order(1, price=60800.0)])
    k = decision(defter_sinamasi__emirler=[{"numara": 1, "sembol": BTC, "yon": "short", "durum": "gecerli",
                                            "gerekce": "x"}])
    a, cmd = entry(price=60700.0)
    assert "aynı yönde ekleme" in problem(a, cmd, k, c)
    k["eylemler"] = [{"tip": "emir_iptal", "numara": 1, "sembol": BTC, "yon": "short"}]
    assert problem(a, cmd, k, c) is None


def test_saturday_0941_risk_statement_is_checked_against_the_real_risk():
    """21.11 09:41: risk küçük beyan edildi. Gerçek risk SL'deki zarar ve komisyondur, beyanın 1,25 katını aşamaz."""
    a, cmd = entry(risk=1.0)
    msg = problem(a, cmd, decision(), ctx())
    assert "risk beyanı" in msg and "gerçek risk yüzde" in msg
    loss = 0.002 * (61000 - 60600) + 0.002 * 60600 * FEES.taker + 0.002 * 61000 * FEES.taker
    a, cmd = entry(risk=loss / 10 * 100 / RISK_SLACK + 0.01)
    assert problem(a, cmd, decision(), ctx()) is None


def test_adding_needs_the_open_position_stop_at_entry_and_counts_its_risk():
    """Açık pozisyonun stopu girişe taşınmadan aynı yönde eklenmez. Taşındıysa toplam risk birlikte hesaplanır."""
    pos = _position(entry=60900.0)
    stop = _order(2, price=61300.0, reduce=True, type_=OrderType.STOP, qty=None)
    k = decision(defter_sinamasi__pozisyonlar=[{"sembol": BTC, "yon": "short", "durum": "gecerli", "gerekce": "x"}])
    a, cmd = entry(risk=50.0)
    assert "stopu girişe" in problem(a, cmd, k, ctx(positions=[pos], orders=[stop]))
    moved = _order(2, price=60900.0, reduce=True, type_=OrderType.STOP, qty=None)
    assert problem(a, cmd, k, ctx(positions=[pos], orders=[moved])) is None


def test_admin_opened_position_counts_for_adding():
    """Yöneticinin açtığı pozisyon da hesaptadır. Stopu girişte değilse aynı yönde ekleme reddedilir."""
    c = ctx(positions=[_position()], facts=Facts(position_origins={7: "admin"}))
    k = decision(defter_sinamasi__pozisyonlar=[{"sembol": BTC, "yon": "short", "durum": "gecerli", "gerekce": "x"}])
    a, cmd = entry()
    assert "aynı yönde ekleme" in problem(a, cmd, k, c)


def test_second_entry_in_the_same_decision_is_rejected():
    """Aynı kararda aynı yönde iki giriş ekleme kuralını delerdi."""
    a, cmd = entry()
    assert "aynı yönde ekleme" in problem(a, cmd, decision(), ctx(), busy={(BTC, "short")})


def test_original_decision_is_not_modified():
    """Kontroller kararı değiştirmez, günlüğe Opus'un yazdığı aynen girer."""
    k = decision()
    before = copy.deepcopy(k)
    validate(k, ctx())
    assert k == before


def test_rising_direction_needs_both_tops_and_bottoms_rising():
    """Tepeler yükselip dipler alçalıyorsa yapı yükselen değildir, genişleyen bir banttır."""
    k = decision(pariteler__BTCUSDT__yapi__alt=_level("yukselen", (60500, 60900), (60300, 60050)),
                 pariteler__BTCUSDT__rejim={"rejim": "belirsiz", "kanit": "x", "uygun": "hayir",
                                            "bekledigi": {"tur": "fiyat", "fiyat": 60000, "saat": ""}})
    assert any("dipler 60300" in e and "yükselmiyor" in e for e in validate(k, ctx()).errors)


def test_risk_just_over_the_slack_is_rejected():
    """Pay 1,25 kattır. Biraz fazlası bile reddedilir, yoksa beyan Opus'u bağlamaz."""
    loss = 0.002 * (61000 - 60600) + 0.002 * 60600 * FEES.taker + 0.002 * 61000 * FEES.taker
    a, cmd = entry(risk=loss / 10 * 100 / RISK_SLACK - 0.05)
    assert "risk beyanı" in problem(a, cmd, decision(), ctx())


def test_counter_evidence_too_short_to_name_a_datum_is_rejected():
    """Bir veriyi ve neden ağır basmadığını söylemeyen kısa bir cümle karşı kanıt değildir."""
    a, cmd = entry(karsi_kanit="RSI yüksek.")
    assert "karşı kanıt" in problem(a, cmd, decision(), ctx())
