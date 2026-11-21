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

from opus_trade.domain.catalog import Catalog
from opus_trade.domain.fees import FeeSchedule
from opus_trade.domain.models import Side
from opus_trade.domain.trading import (AccountEvent, AccountState, Actor, ClosePosition, EventKind, Ref,
                                       Trade, UpdateProtection)
from opus_trade.strategy.base import ActionResult, Context, Wake
from opus_trade.strategy.llm import actions, prompt
from opus_trade.strategy.llm import strategy as llm_strategy
from opus_trade.strategy.llm.client import ClaudeCliClient, LLMError, LLMRequest, LLMResult
from opus_trade.triggers.price_alert import Alert

NOW = 1_795_000_000
SOL = "SOLUSDT"


# --- Eylemler ---

def test_numbered_action_carries_symbol_and_side():
    """Numarayla verilen eylem parite ve yönü de taşır, kapı numaranın başka kayda geçtiğini böyle yakalar."""
    kind, cmd = actions.translate({"tip": "pozisyon_kapat", "numara": 3, "sembol": SOL, "yon": "short", "yuzde": 50})
    assert kind == "command" and cmd == ClosePosition(Ref(3, SOL, Side.SHORT), 50)


def test_single_tp_and_levels_together_are_refused():
    """İkisi birlikte verilirse hangisinin istendiği bilinmez. Biri sessizce seçilse Opus'un niyeti bozulur."""
    with pytest.raises(ValueError, match="birlikte verilemez"):
        actions.translate({"tip": "long_ac", "sembol": SOL, "kaldirac": 5, "marjin_usd": 10, "tp": 110,
                           "tp_kademeleri": [{"fiyat": 120, "yuzde": 100}]})


def test_protection_zero_tp_removes_levels_and_missing_tp_keeps_them():
    """tp 0 kademeleri kaldırır, hiç verilmemesi değiştirmez. İkisi karışırsa korumalar habersiz silinir."""
    ref = {"numara": 1, "sembol": SOL, "yon": "long"}
    _, removed = actions.translate({"tip": "koruma_guncelle", **ref, "tp": 0})
    _, kept = actions.translate({"tip": "koruma_guncelle", **ref, "sl": 95})
    assert removed.tp_levels == () and kept.tp_levels is None and kept.sl == 95


def test_schema_requires_symbol_and_side_on_every_numbered_action():
    """Şema bu alanları zorunlu tutmazsa Opus onları atlayabilir ve kapı kontrolü boşa çıkar."""
    for option in actions.schema([SOL])["anyOf"]:
        if "numara" in option["properties"]:
            assert {"numara", "sembol", "yon"} <= set(option["required"])


# --- Talimat ve istem ---

def test_system_prompt_has_no_forbidden_words_and_uses_real_fees():
    """Talimatta deney, sanal para ya da dönem geçmez. Komisyon oranı hesabın kendi oranından gelir."""
    text = prompt.system_prompt(10, [Catalog().get(SOL)], FeeSchedule(taker=0.0004, maker=0.0001), "", 15, 180, 25)
    lower = text.lower()
    for word in ("deney", "sanal", "dönem", "gerçek para"):
        assert word not in lower
    assert "yüzde 0.04 öder" in text and "yüzde 0.01 öder" in text


def _ctx(**kw) -> Context:
    state = AccountState(1, 10.0, 10.0, (), ())
    return Context(now=NOW, wakes=kw.pop("wakes", []), state=state, prices={SOL: 100.0}, symbols=(SOL,), market=None,
                   start_balance=10.0, **kw)


def test_correction_section_separates_applied_and_rejected():
    """Düzeltme çağrısında Opus kabul edilenleri tekrar verirse aynı işlem iki kez açılır."""
    results = [ActionResult("long_ac SOLUSDT", True, "açıldı"),
               ActionResult("pozisyon_kapat #4 SOLUSDT", False, "#4 numaralı açık pozisyon yok.")]
    text = prompt.build_prompt(_ctx(wakes=[Wake("correction", NOW, results)]), "KARTLAR")
    assert "tekrar verme" in text
    assert "long_ac SOLUSDT: uygulandı" in text and "pozisyon_kapat #4 SOLUSDT: reddedildi" in text


def test_alert_set_by_admin_is_not_told_as_opus_own():
    """Yöneticinin kurduğu alarm Opus'unmuş gibi anlatılırsa Opus kendi notu sanıp yanlış karar verir."""
    alert = Alert(2, SOL, 95.0, True, "destek", "admin", NOW)
    text = prompt.build_prompt(_ctx(alerts=[alert]), "KARTLAR")
    assert "alarm #2 SOLUSDT 95.00, not: destek, yönetici kurdu" in text


def test_closed_event_is_told_with_its_reason_and_net():
    """SL ile kapanış Opus'a nedeni ve net sonucuyla anlatılır, çağrılma nedenini anlaması buna bağlı."""
    t = Trade(1, 1, 1, 3, SOL, Side.LONG, 10, 1, 10, 100, NOW, 97.98, NOW, "sl", -2.0, 0.1, 0.0, 101, 97)
    e = AccountEvent(EventKind.CLOSED, 1, NOW, Actor.SYSTEM, 3, SOL, Side.LONG, price=97.98, trade=t, reason="sl")
    assert prompt.wake_text(Wake("account", NOW, e), NOW) == \
        "#3 SOLUSDT long pozisyonundan 1 SOL 97,98 seviyesinde SL ile kapandı, net -2,10 dolar."


# --- Strateji ---

class FakeLLM:
    def __init__(self, decision):
        self.decision = decision
        self.requests = []

    async def call(self, req, on_tool=None):
        self.requests.append(req)
        return LLMResult(self.decision, "{}", [], {}, 1.0, 0.1)


def test_decide_turns_actions_into_commands_alarms_and_errors(monkeypatch, tmp_path):
    """Kararın her parçası doğru yere gider: komut kapıya, alarm tetikleyiciye, çeviride takılan eylem düzeltmeye."""
    async def cards(*a):
        return "KARTLAR"
    monkeypatch.setattr(llm_strategy, "cards_text", cards)
    decision = {"analiz": "", "pariteler": {}, "ozet": "özet", "gorus": "yatay", "guven": 50, "not_defteri": "not",
                "sonraki_cagri_dk": 1, "yoneticiye_cevap": "",
                "eylemler": [{"tip": "koruma_guncelle", "numara": 1, "sembol": SOL, "yon": "long", "sl": 95},
                             {"tip": "alarm_kur", "sembol": SOL, "fiyat": 90, "not": "bak"},
                             {"tip": "long_ac", "sembol": SOL, "kaldirac": 5, "marjin_usd": 10, "tp": 110,
                              "tp_kademeleri": [{"fiyat": 120, "yuzde": 100}]}]}
    fake = FakeLLM(decision)
    s = llm_strategy.LLMStrategy(fake, Catalog(), tmp_path, {}, tmp_path / "m.db", "", "opus", "high", 15, 180, 25, 600)
    d = asyncio.run(s.decide(_ctx()))
    assert d.commands == [UpdateProtection(Ref(1, SOL, Side.LONG), None, 95, None)]
    assert d.alarms[0]["op"] == "add" and d.alarms[0]["price"] == 90
    assert not d.errors[0].ok and "birlikte verilemez" in d.errors[0].message
    assert d.wait_s == 15 * 60
    assert d.memory == {"notebook": "not"} and d.record["template_version"] == prompt.TEMPLATE_VERSION
    assert fake.requests[0].max_turns == 30


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
