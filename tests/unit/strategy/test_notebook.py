"""
file: test_notebook.py
importable: yok

Not defterinin testleri. Defter Opus'un çağrılar arasındaki tek hafızasıdır.
Geçerli denen plan sessizce değişirse plan sınanmadan sürer, süresi dolan plan
fark edilmezse Opus bayat bir planla işlem açar. Eski biçim okunamazsa yayında
iki profilin hafızası kaybolur.

description: strategy.llm.notebook için birim testleri.
"""

from datetime import datetime

import pytest

from opus_trade.domain.clock import HOME_TZ
from opus_trade.strategy.llm import notebook as nbk
from opus_trade.strategy.llm.notebook import EXPIRED, Notebook

NOW = int(datetime(2026, 11, 22, 12, 0, tzinfo=HOME_TZ).timestamp())
PLAN = {"metin": "Düşüşte 1 saatlik ilk alçalan tepeden short.", "bozulma": "4 saatlikte 62615 üstü kapanış",
        "gecerlilik": "22.11.2026 18:00"}
NEW_PLAN = {"metin": "Yeni plan.", "bozulma": "60000 altı", "gecerlilik": "23.11.2026 06:00"}
SWINGS = {"BTCUSDT": {"4h": {"tepeler": [62615, 62320], "dipler": [60100, 59800]}}}


def _new(plan=NEW_PLAN) -> dict:
    return {"plan": plan, "seviyeler_notu": "62320 direnç.", "gundem": "Salı FOMC tutanakları."}


@pytest.mark.parametrize("review, kept", [("gecerli", True), ("zayifladi", False), ("gerceklesti", False),
                                          ("bozuldu", False)])
def test_plan_review_decides_which_plan_survives(review, kept):
    """Geçerli denen planın metni ve bozulma koşulu Opus farklı yazsa da kalır, yalnızca geçerlilik saati yenisinden
    alınır. Diğer durumlarda yeni plan alınır."""
    old = Notebook(plan=PLAN, note="eski", agenda="eski")
    merged, carried = nbk.merge(old, _new(), review, NOW, SWINGS)
    assert merged.plan == ({**PLAN, "gecerlilik": NEW_PLAN["gecerlilik"]} if kept else NEW_PLAN)
    assert carried is kept
    assert (merged.note, merged.agenda, merged.swings) == ("62320 direnç.", "Salı FOMC tutanakları.", SWINGS)


def test_expired_plan_is_replaced_even_if_called_valid():
    """Süresi dolan plan sınanmadan süremez. Opus geçerli dese de yeni plan alınır."""
    old = Notebook(plan={**PLAN, "gecerlilik": "22.11.2026 11:00"})
    assert nbk.plan_state(old, NOW) == EXPIRED
    merged, carried = nbk.merge(old, _new(), "gecerli", NOW)
    assert merged.plan == NEW_PLAN and carried is False


def test_valid_plan_with_same_text_is_not_reported_as_carried():
    """Opus geçerli planı aynen yazdıysa sonuca not düşmeye gerek yok."""
    merged, carried = nbk.merge(Notebook(plan=PLAN), _new(PLAN), "gecerli", NOW)
    assert merged.plan == PLAN and carried is False


def test_valid_plan_can_be_extended_without_rewriting_it():
    """Trend 23.11 03:57'de süren planını uzatmak istedi, metin kilitli olduğu için uzatamadı. Geçerli denen plan
    aynı metinle yeni geçerlilik saatini alır, bu bir taşıma notu sayılmaz."""
    longer = {**PLAN, "gecerlilik": "23.11.2026 15:00"}
    merged, carried = nbk.merge(Notebook(plan=PLAN), _new(longer), "gecerli", NOW)
    assert merged.plan == longer and carried is False


def test_old_plain_text_notebook_is_read_as_legacy_with_an_expired_plan():
    """Yayında iki profilin defteri düz metin. Okunamazsa hafıza kaybolur, plan sayılırsa sınanmadan sürer."""
    nb = nbk.read({"notebook": "K5: iki zaman dilimi uyumu..."})
    assert nb.legacy.startswith("K5") and nbk.plan_state(nb, NOW) == EXPIRED
    assert "eski biçimde" in nbk.text(nb, NOW) and "K5" in nbk.text(nb, NOW)
    assert nbk.read({}) == Notebook()


def test_round_trip_through_memory_keeps_every_part():
    """Defter hafızaya yazılıp okununca aynı kalır, rakamlar dahil."""
    nb = Notebook(plan=PLAN, note="not", swings=SWINGS, agenda="gündem")
    assert nbk.read({"notebook": nb.to_memory()}) == nb


def test_limits_and_time_format_are_checked():
    """Sınırı aşan defter reddedilir, sistem kesmez. Okunamayan geçerlilik saati planı her çağrıda süresi dolmuş yapardı."""
    ok = _new()
    assert nbk.problems(ok) == []
    long_note = {**ok, "seviyeler_notu": "x" * (nbk.NOTE_MAX + 1)}
    assert nbk.problems(long_note) == [f"seviyeler notu {nbk.NOTE_MAX + 1} karakter, en fazla {nbk.NOTE_MAX} olabilir."]
    bad_time = {**ok, "plan": {**NEW_PLAN, "gecerlilik": "yarın akşam"}}
    assert "22.11.2026 18:00 biçiminde" in nbk.problems(bad_time)[0]
    assert nbk.problems({**ok, "plan": {**NEW_PLAN, "metin": " "}}) == ["plan metni boş olamaz."]


def test_schema_limits_match_the_constants():
    """Şemadaki sınır ile panelin denetlediği sınır aynı kaynaktan gelir."""
    s = nbk.schema()["properties"]
    assert s["seviyeler_notu"]["maxLength"] == nbk.NOTE_MAX
    assert s["plan"]["properties"]["metin"]["maxLength"] == nbk.PLAN_TEXT_MAX
    import re
    assert re.match(s["plan"]["properties"]["gecerlilik"]["pattern"], "22.11.2026 18:00")
    assert not re.match(s["plan"]["properties"]["gecerlilik"]["pattern"], "2026-11-22 18:00")


def test_text_marks_an_expired_plan_and_shows_the_carried_swings():
    """Süresi dolan plan istemde öyle görünür, tepe ve dipler hangi karardan geldiğiyle yazar."""
    nb = Notebook(plan={**PLAN, "gecerlilik": "22.11.2026 11:00"}, note="n", swings=SWINGS, agenda="")
    t = nbk.text(nb, NOW, written_at=NOW - 3600)
    assert "## Plan, süresi doldu" in t
    assert "BTCUSDT 4 saatlik: tepeler 62615" in t
    assert "Gündem\n\nBoş." in t
    assert "## Plan\n\n" in nbk.text(Notebook(plan=PLAN), NOW)
