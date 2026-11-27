"""
file: test_multi.py
importable: yok

Opus Multi türünün, llm_multi, testleri. Bu tür yapı okumalı türün makinesidir,
farkı her rejimin kendi oyun planıdır. Üst aralığın yönüne ters giriş yasağı
yalnızca düzenli trendde kalır. Yasak başka bir rejime sızarsa aşırı uzama ve
dönüş planları hiç giriş açamaz. Düzenli trendden kalkarsa trend planı trende
ters girer. Girişin notundaki plan rejim başına sonucun tek kaynağıdır, düşerse
hangi planın kazandırdığı bilinemez.

description: Opus Multi LLM türü için birim testleri.
"""

import asyncio
from pathlib import Path

import pytest

from opus_trade.domain.models import Side
from opus_trade.domain.trading import AccountState, Position
from opus_trade.llm.base import LLMResult
from opus_trade.strategy.base import Context, Facts, Thesis, Wake
from opus_trade.strategy.llm import kinds, prompt
from opus_trade.strategy.llm import strategy as llm_strategy
from opus_trade.strategy.llm.steps.playbook import DEFAULT_PLANS, PlaybookStep
from tests.fakes import catalog

NOW = 1_795_000_000
SOL = "SOLUSDT"
EMPTY = AccountState(1, 100, 100, (), ())
PLAN = {"metin": "Kenarda ters giriş.", "bozulma": "95 altı", "gecerlilik": "01.01.2030 00:00"}


class FakeLLM:
    def __init__(self, decision):
        self.decision = decision

    async def call(self, req, on_tool=None):
        return LLMResult(self.decision, "{}", [], {}, 1.0, 0.1)


def multi(llm=None, **kw):
    return kinds.multi(llm, catalog(), Path("/w"), {}, Path("/m"), "TARZ", "opus", "high", 15, 180, 25, 600, **kw)


def ctx(state=EMPTY, **kw) -> Context:
    return Context(now=NOW, wakes=[Wake("timer", NOW)], state=state, prices={SOL: 100.0}, symbols=(SOL,),
                   market=None, start_balance=100.0, **kw)


# 4 saatlik alçalıyor, 1 saatlikte ilk alçalan tepe oluşmuş, uyum short yönünde.
# Fiyat 100, long girişin SL'si son 1 saatlik dibin, 98'in, altında.
def decision(regime: str, eylemler=(), fit: str = "evet") -> dict:
    wait = {"tur": "yok", "fiyat": 0, "saat": ""} if fit == "evet" else {"tur": "fiyat", "fiyat": 90, "saat": ""}
    return {
        "defter_sinamasi": {"plan": {"durum": "bozuldu", "gerekce": "Yeni."}, "yapi": {"durum": "ayni", "gerekce": "x"},
                            "gundem": {"durum": "etkisi_yok", "gerekce": "x"}, "emirler": [], "pozisyonlar": []},
        "pariteler": {SOL: {"yapi": {"ust": {"yon": "alcalan", "tepeler": [110, 106], "dipler": [96, 92]},
                                     "alt": {"yon": "yatay", "tepeler": [103, 102], "dipler": [97, 98]}},
                            "rejim": {"rejim": regime, "kanit": "x", "uygun": fit, "bekledigi": wait}}},
        "kapanan_islemler": [], "analiz": "", "eylemler": list(eylemler),
        "not_defteri": {"plan": PLAN, "seviyeler_notu": "98 destek.", "gundem": ""},
        "ozet": "özet", "sonraki_cagri_dk": 60, "yoneticiye_cevap": "", "yoneticiye_oneri": "",
    }


LONG = {"tip": "piyasa_giris", "sembol": SOL, "yon": "long", "marjin_usd": 10, "sl": 97.5,
        "karsi_kanit": "Fonlama pozitif ve long kalabalık, ama açık pozisyon artmıyor.",
        "risk_beyani": {"yuzde": 6, "gerekce": "Yapı net."}, "gecersizlik": {"metin": "97 altı kapanış", "fiyat": 97}}


def decide(monkeypatch, k):
    async def cards(*a):
        return "KARTLAR"
    monkeypatch.setattr(llm_strategy, "cards_text", cards)
    return asyncio.run(multi(FakeLLM(k)).decide(ctx()))


# --- Kayıt ve talimat ---

def test_registry_has_four_plans_and_no_fit_regimes():
    """Panelin formu türü kayıt defterinden çizer. Dört plan ayrı metin alanıdır, varsayılanları doludur. Uygun
    rejimler seçilemez, belirsiz dışındaki her rejimin planı vardır."""
    from opus_trade import plugins
    from opus_trade.registry import validate
    from opus_trade.strategy.base import STRATEGIES
    plugins.load()
    e = STRATEGIES.get("llm_multi")
    names = [p.name for p in e.params]
    assert e.name == "Opus Multi" and "fit_regimes" not in names
    assert {"plan_trend", "plan_range", "plan_stretch", "plan_reversal", "structure_high"} <= set(names)
    p = validate(e.params, {})
    assert p["plan_range"] == DEFAULT_PLANS["yatay"] and p["team_haiku"] == 10
    assert e.cls.accepts_messages and e.cls.uses_model


def test_prompt_carries_the_plans_and_the_narrowed_direction_rule():
    """Talimatta dört plan, belirsizin cümlesi ve planın kuralı durur. Yön kuralı yalnızca düzenli trendi söyler.
    Adımlar yapı okumalı türdeki gibi beştir, oyun planı numara almaz."""
    text = multi(plan_range="BANT PLANI").system(ctx())
    assert "### Oyun planları" in text and "- Yatay: BANT PLANI" in text and "- Belirsiz: Giriş açmazsın." in text
    assert f"- Dönüş: {DEFAULT_PLANS['donus']}" in text
    assert "Yatay, aşırı uzama ve dönüş rejimlerinde girişin yönü o rejimin oyun planındadır." in text
    assert "girişinin değil. 4 saatlik yönü" not in text
    assert "girişinin değil. Rejimi düzenli trend okuduysan ve 4 saatlik yönü" in text
    assert "Stratejin şu rejimlerde çalışır: düzenli trend, yatay, aşırı uzama, dönüş." in text
    assert "Açık pozisyonu, rejim değişse de, onu açan planın kurallarıyla yönetirsin." in text
    assert "İlk beş adım" in text and "4. Son işlemler" in text and "5. Giriş alanları" in text


def test_empty_plan_is_refused_when_saved_and_when_built():
    """Boş plan Opus'a başlıksız bir rejim bırakırdı. Panelden kaydedilirken reddedilir, çünkü kurulumda reddedilen
    sürüm çalışan profili sessizce durdururdu. Kurucu da ayrıca reddeder."""
    from opus_trade import plugins
    from opus_trade.registry import validate
    from opus_trade.strategy.base import STRATEGIES
    plugins.load()
    with pytest.raises(ValueError, match="Oyun planı: yatay boş olamaz"):
        validate(STRATEGIES.get("llm_multi").params, {"plan_range": " \n "})
    assert validate(STRATEGIES.get("llm").params, {"style": ""})["style"] == ""
    with pytest.raises(ValueError, match="Aşırı uzama rejiminin oyun planı boş olamaz"):
        multi(plan_stretch="  ")


# --- Karar ---

@pytest.mark.parametrize("regime, plan", [("asiri_uzama", "aşırı uzama"), ("donus", "dönüş"), ("yatay", "yatay")])
def test_counter_trend_entry_passes_outside_the_trend_and_carries_its_plan(monkeypatch, regime, plan):
    """Üst yön alçalırken long, trend dışındaki rejimlerde kabul edilir. Notu paritenin rejimini plan olarak taşır."""
    d = decide(monkeypatch, decision(regime, [LONG]))
    assert d.rejection is None and d.errors == []
    assert d.commands[-1].side is Side.LONG
    assert d.annotations[-1]["reason"]["oyun_plani"] == plan
    assert d.record["template_version"] == kinds.MULTI.version


def test_counter_trend_entry_is_refused_in_a_regular_trend(monkeypatch):
    """Düzenli trendde yön kuralı aynen geçerlidir. Plan onu kaldırmaz."""
    d = decide(monkeypatch, decision("duzenli_trend", [LONG]))
    assert d.commands == [] and "SOLUSDT yön" in d.errors[0].message


def test_unclear_regime_cannot_be_called_fit(monkeypatch):
    """Belirsiz rejimin planı yoktur. Uygun denirse bütün karar reddedilir."""
    d = decide(monkeypatch, decision("belirsiz", [LONG]))
    assert d.rejection and "stratejinin çalıştığı rejimlerden değil" in d.rejection[0]


def test_open_position_shows_the_plan_that_opened_it():
    """Rejim değişse de pozisyon açan planla yönetilir. Opus planı pozisyonun satırında görür."""
    pos = Position(5, 1, SOL, Side.LONG, 1, 5, 100, NOW - 600, 100, NOW, 100, NOW, 90, 0, 0)
    note = Thesis(SOL, "long", {"oyun_plani": "yatay", "risk_beyani": {"yuzde": 2, "gerekce": "x"}}, 97, "97 altı",
                  NOW - 600)
    c = ctx(AccountState(1, 100, 100, (pos,), ()), facts=Facts(position_notes={5: (note,)}))
    text = prompt.build_prompt(c, "KARTLAR", None)
    assert ": oyun planı yatay, risk beyanı yüzde 2" in text


def test_playbook_step_takes_no_number():
    """Oyun planı adımı numara almaz, sonraki adımların numarası kaymaz."""
    from opus_trade.strategy.llm.steps.base import Steps
    from opus_trade.strategy.llm.steps.book import BookStep
    from opus_trade.strategy.llm.steps.closed import ClosedStep
    steps = Steps([BookStep(), PlaybookStep(DEFAULT_PLANS), ClosedStep()])
    assert steps.count == 2 and steps.items[2].first == 2
