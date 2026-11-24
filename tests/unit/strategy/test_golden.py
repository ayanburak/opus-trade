"""
file: test_golden.py
importable: yok

Canlı Trend 4 ve Altın 1'in talimatı, karar şeması, istemi, kontrolleri ve
claude komutu bu iş boyunca değişmez. Bu dosya o profillerin bugün Opus'a
gördürdüğü her şeyi sabit bir bağlamla üretir ve tests/fixtures/golden
altındaki kopyalarla bayt bayt karşılaştırır. Bir yeniden düzenleme talimatı
tek bir boşluk bile değiştirirse burada düşer.

Kopyalar bilerek değiştirildiğinde yenilenir. Yenilemeden önce eski ve yeni
kopyanın farkı alınır, fark yalnızca istenen bölümde olmalı:

    OPUS_GOLDEN=update .venv/bin/python -m pytest -q tests/unit/strategy/test_golden.py

description: Yapı okumalı LLM türünün altın kopya testleri.
"""

# NOTE: Bağlam elle kurulur, piyasa verisi yoktur. Parite kartları sabit bir
# metindir, çünkü kartların kendi testleri test_cards.py'dedir. Burada korunan
# talimatın ve kontrollerin metnidir.
#
# NOTE: Kontrol vakaları her adımdan bir ret içerir. Bir vaka birden çok
# denetime takılmasın diye her biri geçen karardan tek bir alanı bozar. Bir
# girişin birden çok denetime takıldığı vaka bilerek yoktur, hangi retin önce
# söylendiği karar adımlarının sırasına bağlıdır ve korunan şey değildir.

import asyncio
import json
import os
from dataclasses import asdict, fields, is_dataclass
from datetime import datetime
from pathlib import Path

import pytest

from opus_trade.domain.clock import HOME_TZ
from opus_trade.domain.fees import BINANCE_STANDARD as FEES
from opus_trade.domain.models import Side
from opus_trade.domain.trading import AccountState, Order, OrderType, PlaceOrder, Position, Protection
from opus_trade.strategy.base import ClosedPosition, Context, Facts, Thesis, Wake
from opus_trade.strategy.llm import checks, kinds, prompt
from opus_trade.strategy.llm import strategy as llm_strategy
from opus_trade.llm.base import LLMResult
from opus_trade.llm.claude_cli import ClaudeCliClient
from opus_trade.strategy.llm.notebook import Notebook
from opus_trade.triggers.price_alert import Alert
from tests.fakes import catalog

GOLDEN = Path(__file__).resolve().parents[2] / "fixtures" / "golden"
UPDATE = os.environ.get("OPUS_GOLDEN") == "update"

NOW = int(datetime(2026, 11, 24, 20, 0, tzinfo=HOME_TZ).timestamp())
BTC, XAU = "BTCUSDT", "XAUUSDT"
SYMBOLS = (XAU, BTC)
STRUCT = ("4h", "1h")
FIT = ("duzenli_trend",)
STYLE = "Altında ve bitcoinde trendi izlersin. Geri çekilmenin bittiği yerde girersin."
PLAN = {"metin": "Altında 1 saatlik ilk yükselen dipten long.", "bozulma": "4040 altı",
        "gecerlilik": "25.11.2026 08:00"}


def check(name: str, text: str) -> None:
    """Metni kopyasıyla karşılaştırır. OPUS_GOLDEN=update ise kopyayı yazar."""
    path = GOLDEN / name
    if UPDATE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return
    assert path.exists(), f"{name} kopyası yok, OPUS_GOLDEN=update ile üretilir."
    assert text == path.read_text(), f"{name} kopyasından farklı."


def dump(x) -> str:
    return json.dumps(x, ensure_ascii=False, indent=1, sort_keys=True, default=str) + "\n"


# --- Bağlam ---

# Hesap: BTC'de stopu girişin ötesinde açık bir short, altında bekleyen bir
# limit long, bir alarm. Bugün bir stop, sınıflandırılmayı bekleyen bir pozisyon.
BTC_SHORT = Position(7, 1, BTC, Side.SHORT, 0.002, 6.05, 60500, NOW - 7200, 60100, NOW - 600, 60900, NOW - 3600,
                     70000, 0.05, 0.001)
BTC_SL = Order(21, 1, 1, BTC, Side.SHORT, OrderType.STOP, True, None, 61300, None, None, 0, (), None, NOW - 7200)
XAU_LIMIT = Order(22, 1, 2, XAU, Side.LONG, OrderType.LIMIT, False, 0.01, 4060, None, None, 2.03,
                  (Protection(OrderType.STOP, 4030),), NOW + 3600, NOW - 1800)
ALERT = Alert(1, XAU, 4100, False, "Direnç kırılırsa bak.", "opus", NOW - 900)
CLOSED = ClosedPosition(5, XAU, "long", -0.42, "sl", NOW - 5400, "strategy", True)
THESIS = Thesis(BTC, "short", {"karsi_kanit": "Fonlama negatif.", "risk_beyani": {"yuzde": 4, "gerekce": "Yapı net."}},
                61200, "61200 üstü kapanış", NOW - 7200)
FACTS = Facts(today_count=1, today_stops=1, today_net=-0.42, loss_streak=1, recent=(CLOSED,), fill_rate=(2, 3),
              last_stop={f"{XAU} long": True}, position_origins={7: "strategy"}, order_origins={22: "strategy"},
              unreviewed=(CLOSED,), position_notes={7: (THESIS,)},
              order_notes={22: Thesis(XAU, "long", {"risk_beyani": {"yuzde": 3, "gerekce": "Dip yakın."}}, 4040,
                                      "4040 altı", NOW - 1800)},
              last_call=NOW - 3600, version_start=NOW - 86400)
MEMORY = {"notebook": {"plan": PLAN, "yapi": {"seviyeler_notu": "4050 destek.",
                                              "rakamlar": {XAU: {"4h": {"tepeler": [4120, 4110], "dipler": [4010, 4030]}}}},
                       "gundem": "25.11 ABD verisi."},
          "notebook_at": NOW - 3600}
HISTORY = [{"time": NOW - 3600, "summary": "Altında dip bekleniyor.", "results": ["emir_ver #2 XAUUSDT: verildi."],
            "regimes": {XAU: ("duzenli_trend", "evet"), BTC: ("yatay", "hayir")}, "rejected": False}]


def state(positions=(BTC_SHORT,), orders=(BTC_SL, XAU_LIMIT), equity=12.0) -> AccountState:
    return AccountState(1, equity, equity, tuple(positions), tuple(orders), leverages={XAU: 10})


def ctx(**kw) -> Context:
    args = dict(now=NOW, wakes=[Wake("timer", NOW)], state=state(), prices={XAU: 4070.0, BTC: 60200.0},
                marks={XAU: 4069.5, BTC: 60210.0}, symbols=SYMBOLS, market=None, memory=MEMORY, history=HISTORY,
                admin={"items": ["Teklif: altında acele etme."]}, alerts=[ALERT],
                stats={"count": 3, "wins": 1, "net": -0.3, "fee": 0.04}, start_balance=10.0, facts=FACTS)
    return Context(**{**args, **kw})


# --- Karar ---

def _level(yon, tops, lows) -> dict:
    return {"yon": yon, "tepeler": list(tops), "dipler": list(lows)}


def _pair(top, low, rejim="duzenli_trend", uygun="evet", bekledigi=None) -> dict:
    return {"yapi": {"ust": top, "alt": low},
            "rejim": {"rejim": rejim, "kanit": "4 saatlikte yükselen dipler, RSI 58.", "uygun": uygun,
                      "bekledigi": bekledigi or {"tur": "yok", "fiyat": 0, "saat": ""}}}


XAU_UP = _pair(_level("yukselen", (4100, 4120), (4010, 4030)), _level("yukselen", (4080, 4090), (4045, 4055)))
BTC_FLAT = _pair(_level("yatay", (61500, 61400), (59800, 59900)), _level("alcalan", (61000, 60800), (60100, 60000)),
                 rejim="yatay", uygun="hayir", bekledigi={"tur": "fiyat", "fiyat": 61500, "saat": ""})

ENTRY = {"tip": "piyasa_giris", "sembol": XAU, "yon": "long", "marjin_usd": 2, "kaldirac": 10, "sl": 4050,
         "tp": 4150, "karsi_kanit": "Dolar endeksi güçleniyor, ama altın 4050'yi iki kez tuttu.",
         "risk_beyani": {"yuzde": 3, "gerekce": "Yapı net, stop dibin altında."},
         "gecersizlik": {"metin": "4045 altı kapanış", "fiyat": 4045},
         "son_stoptan_farki": "Bu kez dip oluştuktan sonra giriyorum."}


def decision(**over) -> dict:
    k = {
        "defter_sinamasi": {"plan": {"durum": "gecerli", "gerekce": "Yapı sürüyor."},
                            "yapi": {"durum": "ayni", "gerekce": "Aynı."},
                            "gundem": {"durum": "etkisi_yok", "gerekce": "Yok."},
                            "emirler": [{"numara": 2, "sembol": XAU, "yon": "long", "durum": "gecerli",
                                         "gerekce": "Dip sürüyor."}],
                            "pozisyonlar": [{"sembol": BTC, "yon": "short", "durum": "gecerli", "gerekce": "Bant."}]},
        "pariteler": {XAU: json.loads(json.dumps(XAU_UP)), BTC: json.loads(json.dumps(BTC_FLAT))},
        "kapanan_islemler": [{"pozisyon_no": 5, "sinif": "zamanlama_yanlisti", "aciklama": "Erken girdim."}],
        "analiz": "Altında yükseliş sürüyor.",
        "eylemler": [],
        "not_defteri": {"plan": PLAN, "seviyeler_notu": "4050 destek.", "gundem": "25.11 ABD verisi."},
        "ozet": "Altında long bekliyorum.", "sonraki_cagri_dk": 60, "yoneticiye_cevap": "", "yoneticiye_oneri": "",
    }
    for path, value in over.items():
        node = k
        keys = path.split("__")
        for x in keys[:-1]:
            node = node[x]
        node[keys[-1]] = value
    return k


# Kopyadaki biçim adımlara ayrılmadan önceki tek sonuçtur, yapı adımının
# sonucu onun yanına açılır.
def _review(r: checks.Review) -> dict:
    out = {"errors": r.errors, "cancels": r.cancels, "notes": r.notes}
    part = r.parts["structure"]
    return {**out, **{f.name: getattr(part, f.name) for f in fields(part)}}


def _entry_cmd(a: dict) -> PlaceOrder:
    price = a.get("fiyat")
    type_ = OrderType.LIMIT if a["tip"] == "emir_ver" else OrderType.MARKET
    return PlaceOrder(a["sembol"], Side(a["yon"]), type_, qty=0.005, price=price)


# Bekleyen altın emri olmayan hesap ve ona uyan sınama. Giriş vakaları bununla
# kurulur, yoksa giriş bekleyen emir yüzünden aynı yönde eklemeye de takılırdı.
ONLY_ENTRY = {"defter_sinamasi__emirler": []}
LONE = {"orders": (BTC_SL,)}

# Her vaka: kararın üstüne yazılanlar, hesap, gerçekler ve denenecek giriş.
# Giriş None ise yalnızca validate() çalışır.
CASES = {
    "gecen_karar": ({"defter_sinamasi__emirler": []}, {"orders": (BTC_SL,)}, ENTRY),
    "adim1_eksik_emir": ({"defter_sinamasi__emirler": []}, {}, None),
    "adim1_fazla_pozisyon": ({"defter_sinamasi__pozisyonlar": [
        {"sembol": BTC, "yon": "short", "durum": "gecerli", "gerekce": "x"},
        {"sembol": XAU, "yon": "long", "durum": "gecerli", "gerekce": "x"}]}, {}, None),
    "adim1_bozuldu_kapatmadan": ({"defter_sinamasi__pozisyonlar": [
        {"sembol": BTC, "yon": "short", "durum": "bozuldu", "gerekce": "x"}]}, {}, None),
    "adim1_bozuldu_emir_iptal": ({"defter_sinamasi__emirler": [
        {"numara": 2, "sembol": XAU, "yon": "long", "durum": "bozuldu", "gerekce": "x"}]}, {}, None),
    "adim1_ayni_plan": ({"defter_sinamasi__plan": {"durum": "bozuldu", "gerekce": "x"}}, {}, None),
    "adim1_gecerli_plan_tasinir": ({"not_defteri__plan": {**PLAN, "metin": "Başka bir plan."}}, {}, None),
    "adim2_celiski": ({"pariteler__XAUUSDT__yapi__alt__dipler": [4055, 4045]}, {}, None),
    "adim3_uyumsuz_trend": ({"pariteler__XAUUSDT__yapi__alt__dipler": [4055, 4050],
                             "pariteler__XAUUSDT__yapi__alt__yon": "belirsiz"}, {}, None),
    "adim3_uygun_olmayan_rejim": ({"pariteler__BTCUSDT__rejim__uygun": "evet"}, {}, None),
    "adim3_beklenti_yok": ({"pariteler__XAUUSDT__rejim__uygun": "hayir"}, {"positions": ()}, None),
    "adim3_saat": ({"pariteler__BTCUSDT__rejim__bekledigi": {"tur": "saat", "fiyat": 0,
                                                             "saat": "25.11.2026 02:00"}}, {}, None),
    "adim4_eksik": ({"kapanan_islemler": []}, {}, None),
    "giris_rejim": (ONLY_ENTRY, LONE, {**ENTRY, "sembol": BTC, "yon": "short", "sl": 61200,
                             "gecersizlik": {"metin": "x", "fiyat": 61100}}),
    "giris_yon": ({**ONLY_ENTRY, "pariteler__XAUUSDT__yapi__alt__dipler": [4045, 4055]}, LONE,
                  {**ENTRY, "yon": "short", "sl": 4100, "gecersizlik": {"metin": "x", "fiyat": 4095}}),
    "giris_karsi_kanit": (ONLY_ENTRY, LONE, {**ENTRY, "karsi_kanit": "Yok."}),
    "giris_gecersizlik": (ONLY_ENTRY, LONE, {**ENTRY, "gecersizlik": {"metin": "x", "fiyat": 4080}}),
    "giris_son_stop": (ONLY_ENTRY, LONE, {k: v for k, v in ENTRY.items() if k != "son_stoptan_farki"}),
    "giris_seviye": (ONLY_ENTRY, LONE, {**ENTRY, "tip": "emir_ver", "tur": "limit", "fiyat": 4050}),
    "giris_sl_seviye": (ONLY_ENTRY, LONE, {**ENTRY, "sl": 4060}),
    "giris_ekleme_stop": ({}, {"positions": (Position(8, 1, XAU, Side.LONG, 0.01, 4.06, 4060, NOW - 600, 4070, NOW,
                                                      4060, NOW, 3700, 0, 0),), "orders": ()}, ENTRY),
    "giris_ekleme_stop_giriste": ({}, {"positions": (Position(8, 1, XAU, Side.LONG, 0.01, 4.06, 4040, NOW - 600, 4070,
                                                              NOW, 4040, NOW, 3700, 0, 0),),
                                       "orders": (Order(23, 1, 1, XAU, Side.LONG, OrderType.STOP, True, None, 4040,
                                                        None, None, 0, (), None, NOW - 600),)}, ENTRY),
    "giris_ekleme_bekleyen": ({}, {}, ENTRY),
    "giris_ekleme_iptal_edilen": ({"defter_sinamasi__emirler": [
        {"numara": 2, "sembol": XAU, "yon": "long", "durum": "bozuldu", "gerekce": "x"}]}, {}, ENTRY),
    "giris_risk": ({"defter_sinamasi__emirler": []}, {"orders": (BTC_SL,)}, {**ENTRY, "risk_beyani": {"yuzde": 0.5, "gerekce": "x"}}),
    "giris_risk_slsiz": ({"defter_sinamasi__emirler": []}, {"orders": (BTC_SL,)}, {k: v for k, v in ENTRY.items() if k != "sl"}),
}


def test_system_prompt_is_unchanged():
    """Talimatın metni canlı profillerinkiyle aynıdır: yapı aralıkları, uygun rejimler, tarz ve iki parite."""
    check("system_prompt.txt", _strategy(None).system(ctx()))
    wide = kinds.structured(None, catalog(), Path("/calisma"), {}, Path("/m.db"), "", "opus", "high", 15, 180, 30, 600,
                            structure_high="1d", structure_low="4h", fit_regimes=("duzenli_trend", "yatay"))
    check("system_prompt_wide.txt", wide.system(ctx(symbols=(BTC,))))


def test_schema_is_unchanged():
    """Karar şemasının alanları, sırası, açıklamaları ve sınırları aynıdır."""
    check("schema.json", json.dumps(_strategy(None).schema(list(SYMBOLS)), ensure_ascii=False, indent=1) + "\n")


def test_prompt_is_unchanged():
    """İstemin bölümleri ve hesap satırları aynıdır."""
    check("prompt.txt", prompt.build_prompt(ctx(), "KARTLAR", NOW - 3600))


def test_checks_are_unchanged():
    """Her kontrol adımının sonucu ve ret metni aynıdır."""
    steps = _strategy(None).steps
    out = {}
    for name, (over, acct, entry) in CASES.items():
        k = decision(**over)
        if entry is not None:
            k["eylemler"] = [entry]
        c = ctx(state=state(**acct)) if acct else ctx()
        r = steps.validate(k, c, Notebook(plan=PLAN))
        item = {"review": _review(r)}
        if entry is not None:
            item["entry"] = steps.entry_problem(entry, _entry_cmd(entry), k, r, c, 10, set())
        out[name] = item
    busy = steps.validate(decision(), ctx(state=state(orders=(BTC_SL,))), Notebook(plan=PLAN))
    out["giris_ayni_kararda_ikinci"] = {"entry": steps.entry_problem(
        ENTRY, _entry_cmd(ENTRY), decision(), busy, ctx(state=state(orders=(BTC_SL,))), 10, {(XAU, "long")})}
    check("checks.json", dump(out))


# --- Karar akışı ---

class _Recorder:
    def __init__(self, decision: dict) -> None:
        self.decision = decision
        self.requests = []

    async def call(self, req, on_tool=None):
        self.requests.append(req)
        return LLMResult(self.decision, "{}", [{"name": "piyasa.mumlar", "title": "x"}], {"usage": {}}, 3.0, 0.2)


def _plain(x):
    if is_dataclass(x):
        return {"type": type(x).__name__, **{k: _plain(v) for k, v in asdict(x).items()}}
    if isinstance(x, (list, tuple)):
        return [_plain(v) for v in x]
    if isinstance(x, dict):
        return {str(k): _plain(v) for k, v in x.items()}
    return x


def _strategy(llm) -> llm_strategy.LLMStrategy:
    return kinds.structured(llm, catalog(), Path("/calisma"), {"OPUS_PROFILE": "4"}, Path("/m.db"), STYLE, "opus",
                            "high", 5, 2800, 25, 600, fees=FEES)


def _decide(monkeypatch, k: dict, c: Context):
    async def cards(*a):
        return "KARTLAR"
    monkeypatch.setattr(llm_strategy, "cards_text", cards)
    rec = _Recorder(k)
    d = asyncio.run(_strategy(rec).decide(c))
    return d, rec.requests[0]


@pytest.mark.parametrize("name, over, entries, acct", [
    ("kabul", {}, [ENTRY, {"tip": "alarm_kur", "sembol": XAU, "fiyat": 4100, "not": "bak"}], {"orders": (BTC_SL,)}),
    ("ret", {"kapanan_islemler": []}, [ENTRY], {}),
    ("beklenti", {"pariteler__XAUUSDT__rejim__uygun": "hayir",
                  "pariteler__XAUUSDT__rejim__bekledigi": {"tur": "saat", "fiyat": 0, "saat": "24.11.2026 21:00"}},
     [], {}),
])
def test_decide_is_unchanged(monkeypatch, name, over, entries, acct):
    """Kararın komutları, alarmları, notları, hafızası ve günlük kaydı aynıdır."""
    k = decision(**over)
    k["eylemler"] = entries
    d, req = _decide(monkeypatch, k, ctx(state=state(**acct)) if acct else ctx())
    out = _plain(d)
    out["record"].pop("prompt")
    out["record"].pop("raw")
    sent = {"system": req.system, "prompt": req.prompt, "schema": req.schema, "model": req.model,
            "effort": req.effort, "max_turns": req.max_turns, "timeout_s": req.timeout_s}
    check(f"decide_{name}.json", dump({"decision": out, "request": sent}))


def test_command_is_unchanged(monkeypatch):
    """claude komutu aynıdır: model, bayraklar, araçlar, izinler, ajanlar ve araç sunucusunun ortamı."""
    d, req = _decide(monkeypatch, decision(), ctx())
    cmd = ClaudeCliClient(Path("/py/python"), Path("/kok"), claude="claude").command(req)
    cmd[cmd.index("--system-prompt") + 1] = "TALIMAT"
    cmd[cmd.index("--json-schema") + 1] = "SEMA"
    check("command.json", dump(cmd))
