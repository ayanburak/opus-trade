"""
file: test_open.py
importable: yok

Serbest Opus türünün testleri. Bu türde yön, yöntem ve zamanlama Opus'undur:
yapı, rejim, trende ters giriş yasağı, girişin tepe ve dibe göre yeri ve aynı
yönde ekleme kuralı yoktur. Karar şeması, defter sınaması, kapanan işlemlerin
sınıflandırılması ve giriş alanları ise yapı okumalı türdeki gibi zorunludur,
tasarim.md § 4.6. Yapının bir kalıntısı talimata ya da denetime sızarsa Opus
olmayan bir kurala takılır. Zorunlu bir alan düşerse kayıplar incelenemez.

description: Serbest LLM türü, llm_open, için birim testleri.
"""

import asyncio
import json
from pathlib import Path

import jsonschema
import pytest

from opus_trade.domain.models import Side
from opus_trade.domain.trading import AccountState, Order, OrderType, Position
from opus_trade.llm.base import LLMResult
from opus_trade.strategy.base import Context, Facts, Wake
from opus_trade.strategy.llm import kinds
from opus_trade.strategy.llm import strategy as llm_strategy
from tests.fakes import catalog

NOW = 1_795_500_000
XAU = "XAUUSDT"
PLAN = {"metin": "4000 üstünde long, hedef 4100.", "bozulma": "3950 altı", "gecerlilik": "01.01.2030 00:00"}
# Bütün kotalar aynı olunca iki türün Araçların bölümü de aynıdır.
TEAM = {"team_opus": 3, "team_sonnet": 10, "team_haiku": 50}


class FakeLLM:
    def __init__(self, decision):
        self.decision = decision
        self.requests = []

    async def call(self, req, on_tool=None):
        self.requests.append(req)
        return LLMResult(self.decision, "{}", [], {}, 1.0, 0.1)


def free(llm=None, **kw):
    return kinds.free(llm, catalog(), Path("/w"), {}, Path("/m"), kw.pop("style", "TARZ"), "opus", "high", 15, 240, 40,
                      1800, **kw)


def ctx(state=None, **kw) -> Context:
    return Context(now=NOW, wakes=[Wake("timer", NOW)], state=state or AccountState(1, 10.0, 10.0, (), ()),
                   prices={XAU: 4000.0}, marks={XAU: 4000.0}, symbols=(XAU,), market=None, start_balance=10.0, **kw)


def decision(eylemler=(), **over) -> dict:
    k = {"defter_sinamasi": {"plan": {"durum": "bozuldu", "gerekce": "Yeni."}, "yapi": {"durum": "ayni", "gerekce": "x"},
                             "gundem": {"durum": "etkisi_yok", "gerekce": "x"}, "emirler": [], "pozisyonlar": []},
         "kapanan_islemler": [], "analiz": "", "eylemler": list(eylemler),
         "not_defteri": {"plan": PLAN, "seviyeler_notu": "3950 destek.", "gundem": ""},
         "ozet": "özet", "sonraki_cagri_dk": 60, "yoneticiye_cevap": "", "yoneticiye_oneri": ""}
    k.update(over)
    return k


# Short giriş. Yapı okumalı türde üst yön yükselen yazılınca reddedilirdi.
SHORT = {"tip": "piyasa_giris", "sembol": XAU, "yon": "short", "marjin_usd": 1, "kaldirac": 10, "sl": 4040,
         "karsi_kanit": "Dolar zayıf, ama altın direnci üçüncü kez test ediyor.",
         "risk_beyani": {"yuzde": 2, "gerekce": "Direnç net."}, "gecersizlik": {"metin": "4030 üstü", "fiyat": 4030}}
LONG = {**SHORT, "yon": "long", "sl": 3960, "gecersizlik": {"metin": "3970 altı", "fiyat": 3970}}


def decide(k, state=None, monkeypatch=None):
    async def cards(*a):
        return "KARTLAR"
    monkeypatch.setattr(llm_strategy, "cards_text", cards)
    llm = FakeLLM(k)
    return asyncio.run(free(llm).decide(ctx(state))), llm


# --- Talimat ve şema ---

def test_schema_has_every_universal_field_and_nothing_of_structure():
    """Defter sınaması, kapanan işlemler, giriş alanları, defter, analiz, özet ve yönetici alanları zorunludur.
    Yapı ve rejim alanı yoktur."""
    s = free().schema([XAU])
    assert list(s["properties"]) == ["defter_sinamasi", "kapanan_islemler", "analiz", "eylemler", "not_defteri", "ozet",
                                     "sonraki_cagri_dk", "yoneticiye_cevap", "yoneticiye_oneri"]
    assert s["required"] == list(s["properties"])
    assert s["properties"]["kapanan_islemler"]["description"].startswith("Adım 2.")
    entry = next(o for o in s["properties"]["eylemler"]["items"]["anyOf"] if o["properties"]["tip"].get("const") ==
                 "piyasa_giris")
    assert {"karsi_kanit", "risk_beyani", "gecersizlik"} <= set(entry["required"])
    assert "Tepe ve dip" not in json.dumps(s, ensure_ascii=False)
    jsonschema.validate(decision(), s)


def test_prompt_has_no_trace_of_structure():
    """Talimatta yapı, rejim, trend yönü, beklenen fiyat ya da tepe ve dip kuralı geçmez. Adımlar 1, 2, 3'tür."""
    text = free().system(ctx())
    for gone in ("Rejimler", "Yapıyı okumak", "Sistem girişleri ayrıca", "rejim", "Rejimi uygun bulmadığın",
                 "Tepe ve dip rakamlarını", "düzenli trend", "İlk beş adım", "4. Son işlemler"):
        assert gone not in text, gone
    assert "İlk üç adım" in text and "2. Son işlemler" in text and "3. Giriş alanları" in text
    assert "## Hedefin\n\nHedefin çalışma tarzın bölümünde yazar." in text
    assert "kalıcı olarak büyütmek" not in text and "## Çalışma tarzın\n\nTARZ" in text


def _sections(text: str) -> dict[str, str]:
    parts = text.split("\n## ")
    return {p.split("\n", 1)[0]: p for p in parts[1:]}


def test_common_sections_are_byte_for_byte_the_structured_ones():
    """Ortak bölümler iki türde bayt bayt aynıdır. Yalnızca hedef, karar adımları ve yapının taşıdığı iki cümle
    farklıdır."""
    a = _sections(free(**TEAM).system(ctx()))
    b = _sections(kinds.structured(None, catalog(), Path("/w"), {}, Path("/m"), "TARZ", "opus", "high", 15, 240, 40,
                                   1800, **TEAM).system(ctx()))
    assert list(a) == list(b)
    for head in ("Öncelik sırası", "Çalışma tarzın", "Yönetici", "Araçların", "Cevap"):
        assert a[head] == b[head], head
    note = " Tepe ve dip rakamlarını sistem 2. adımda yazdıklarından deftere kendisi taşır."
    assert a["Not defteri"] == b["Not defteri"].replace(note, "")
    alarm = (" Rejimi uygun bulmadığın bir paritede beklediğin fiyata sistemin kurduğu alarm da listede görünür, "
             "yeni bir beklenen fiyat yazınca eskisinin yerini alır.")
    assert a["Hesabın kuralları"] == b["Hesabın kuralları"].replace(alarm, "")


def test_registry_has_both_kinds_with_their_own_quota():
    """Panelin oluşturma formu iki türü de kayıt defterinden çizer. Serbest türün yapı parametresi yoktur."""
    from opus_trade import plugins
    from opus_trade.registry import validate
    from opus_trade.strategy.base import STRATEGIES
    plugins.load()
    e = STRATEGIES.get("llm_open")
    names = [p.name for p in e.params]
    assert e.name == "Opus, serbest" and "structure_high" not in names and "fit_regimes" not in names
    p = validate(e.params, {})
    assert (p["team_opus"], p["team_sonnet"], p["team_haiku"]) == (3, 10, 50)
    assert (p["daily_opus"], p["daily_sonnet"], p["daily_haiku"]) == (15, 60, 300)
    assert e.cls.accepts_messages and e.cls.uses_model


# --- Karar ---

def test_counter_trend_entry_and_adding_to_the_same_side_pass(monkeypatch):
    """Short ve aynı kararda ikinci short kabul edilir. Yapı okumalı türde ikincisi aynı yönde eklemeye takılırdı.
    Rozet, beklenti alarmı ve defterde rakam yoktur."""
    d, llm = decide(decision([SHORT, SHORT]), monkeypatch=monkeypatch)
    assert d.rejection is None and d.errors == []
    assert len([c for c in d.commands if getattr(c, "side", None) is Side.SHORT]) == 2
    assert "badge" not in d.record and d.alarms == [] and d.memory["notebook"]["yapi"]["rakamlar"] == {}
    assert d.record["template_version"] == kinds.OPEN.version and d.record["checks"]["errors"] == []
    assert llm.requests[0].timeout_s == 1800 and "ekip" in llm.requests[0].toolset.servers


def test_adding_to_a_position_without_moving_the_stop_passes(monkeypatch):
    """Açık long'un stopu girişte değilken ve bekleyen long giriş varken yeni long kabul edilir. Ekleme yine gerçek
    riskle bağlıdır: stopsuz açık pozisyonun marjini beyana girer, yüzde 2 yazılsa reddedilirdi."""
    pos = Position(5, 1, XAU, Side.LONG, 0.002, 0.8, 3990, NOW - 600, 4000, NOW, 3990, NOW, 3600, 0, 0)
    pending = Order(9, 1, 1, XAU, Side.LONG, OrderType.LIMIT, False, 0.002, 3980, None, None, 0.8, (), None, NOW)
    state = AccountState(1, 10.0, 10.0, (pos,), (pending,), leverages={XAU: 10})
    k = decision([{**LONG, "risk_beyani": {"yuzde": 20, "gerekce": "Açık pozisyon dahil."}}])
    k["defter_sinamasi"]["emirler"] = [{"numara": 1, "sembol": XAU, "yon": "long", "durum": "gecerli", "gerekce": "x"}]
    k["defter_sinamasi"]["pozisyonlar"] = [{"sembol": XAU, "yon": "long", "durum": "gecerli", "gerekce": "x"}]
    d, _ = decide(k, state, monkeypatch)
    assert d.rejection is None and d.errors == [] and d.commands[0].side is Side.LONG


@pytest.mark.parametrize("change, text", [
    ({"karsi_kanit": "Yok."}, "adım 3, karşı kanıt"),
    ({"gecersizlik": {"metin": "x", "fiyat": 3990}}, "adım 3, geçersizlik"),
    ({"risk_beyani": {"yuzde": 0.1, "gerekce": "x"}}, "adım 3, risk beyanı"),
])
def test_universal_entry_checks_still_refuse(monkeypatch, change, text):
    """Karşı kanıt, geçersizliğin yeri ve risk beyanı serbest türde de bağlar. Numara giriş adımınındır."""
    d, _ = decide(decision([{**SHORT, **change}]), monkeypatch=monkeypatch)
    assert d.commands == [] and text in d.errors[0].message


def test_book_and_closed_checks_still_reject_the_whole_decision(monkeypatch):
    """Defter sınamasında eksik pozisyon ve sınıflandırılmayan kapanış bütün kararı reddeder, adım 1 ve adım 2."""
    from opus_trade.strategy.base import ClosedPosition
    pos = Position(5, 1, XAU, Side.LONG, 0.002, 0.8, 3990, NOW - 600, 4000, NOW, 3990, NOW, 3600, 0, 0)
    closed = ClosedPosition(3, XAU, "short", -0.2, "sl", NOW - 60, "strategy", True)

    async def cards(*a):
        return "KARTLAR"
    monkeypatch.setattr(llm_strategy, "cards_text", cards)
    s = free(FakeLLM(decision()))
    c = ctx(AccountState(1, 10.0, 10.0, (pos,), ()), facts=Facts(unreviewed=(closed,)))
    d = asyncio.run(s.decide(c))
    assert d.rejection and any("adım 1, pozisyonlar" in e for e in d.rejection)
    assert any("adım 2, kapanan işlemler" in e for e in d.rejection)
