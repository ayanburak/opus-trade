"""
file: test_steps.py
importable: yok

Karar adımı listesinin testleri. Numaralar yanlış üretilirse talimattaki
adım numarası ret metnindekiyle ve şemanın açıklamasıyla çelişir, Opus hangi
adımda takıldığını yanlış okur. Bir adım listeden çıkınca onun şeması,
metni ya da denetimi geride kalırsa yapısı olmayan tür yapı yasaklarına
takılır. Adımların tek tek denetimleri test_checks.py'dedir.

description: strategy.llm.steps.base için birim testleri.
"""

import pytest

from opus_trade.domain.fees import BINANCE_STANDARD as FEES
from opus_trade.strategy.llm.checks import Review
from opus_trade.strategy.llm.steps.base import Steps
from opus_trade.strategy.llm.steps.book import BookStep
from opus_trade.strategy.llm.steps.closed import ClosedStep
from opus_trade.strategy.llm.steps.entry import EntryStep
from opus_trade.strategy.llm.steps.structure import StructureStep


def full() -> Steps:
    return Steps([BookStep(), StructureStep(("4h", "1h"), ("duzenli_trend",)), ClosedStep(), EntryStep(FEES)])


def bare() -> Steps:
    return Steps([BookStep(), ClosedStep(), EntryStep(FEES)])


def test_numbers_follow_the_list_and_structure_takes_two():
    """Yapı adımı yapı ve rejim diye iki numara alır. Yapı yoksa adımlar 1, 2, 3 olur."""
    s = full()
    assert [x.first for x in s.items] == [1, 2, 4, 5] and s.count == 5 and s.count_word() == "beş"
    assert [t[:2] for t in s.numbered()] == ["1.", "2.", "3.", "4.", "5."]
    b = bare()
    assert [x.first for x in b.items] == [1, 2, 3] and b.count_word() == "üç" and b.entry_no == 3
    assert [t.split(". ", 1)[1][:12] for t in b.numbered()] == ["Defteri sına", "Son işlemler", "Giriş alanla"]


def test_schema_descriptions_carry_the_same_numbers():
    """Şemanın açıklamasındaki adım numarası talimattakiyle aynıdır."""
    f = bare().fields(["XAUUSDT"])
    assert list(f) == ["defter_sinamasi", "kapanan_islemler"]
    assert f["kapanan_islemler"]["description"].startswith("Adım 2.")
    assert full().fields(["XAUUSDT"])["kapanan_islemler"]["description"].startswith("Adım 4.")


def test_without_structure_nothing_of_structure_remains():
    """Yapısı olmayan listede pariteler alanı, rejimler bölümü, giriş seviyesi cümleleri, rozet, beklenti alarmı ve
    deftere taşınan rakam yoktur."""
    b = bare()
    assert "pariteler" not in b.fields(["XAUUSDT"]) and b.sections() == []
    assert b.entry_rules().startswith("Girişin gerekçesi") and "Sistem girişleri" not in b.entry_rules()
    assert b.notebook_note() == ("", "")
    k = {"eylemler": [], "kapanan_islemler": []}
    review = Review()
    assert b.record(k) == {} and b.swings(k) == {} and b.alarms(k, review) == []
    assert b.checks(review) == {} and b.wait_until(review) is None


def test_same_step_twice_is_refused():
    """Aynı adım iki kez verilirse numaraları ve şema alanları çakışır."""
    with pytest.raises(ValueError, match="iki kez"):
        Steps([BookStep(), BookStep()])
