"""
file: test_records.py
importable: yok

Kayıt düzeninin testleri: işlem olaylarının yapanıyla yazılması, en iyi ve
en kötü fiyatın zamanıyla tutulması ve Opus çağrısındaki araç sonuçlarının,
sürelerinin ve token kullanımının okunması. Proje klasöründe uv run pytest
ile çalışır.

description: trade_events, fiyat uçları ve agent.call akış ayrıştırması için testler.
"""

# NOTE: agent.call testi gerçek claude komutunu çağırmaz. PATH'in başına
# hazır stream-json satırlarını basan sahte bir claude betiği konur, abonelik
# limitinden hiçbir şey düşmez. Satırların biçimi claude komutunun print
# modundaki çıktısının bugünkü halidir, claude bu biçimi değiştirirse test
# de güncellenir.

import asyncio
import json
import os
import stat

from opus_trade import settings
from opus_trade.engine.commands import Command, execute
from opus_trade.opus import agent

from conftest import NOW

PRICES = {"BTCUSDT": 50000.0, "SOLUSDT": 100.0}


def _events(account) -> list[dict]:
    return [dict(r) for r in account.con.execute("SELECT * FROM trade_events ORDER BY id")]


# --- İşlem olayları ---

def test_requests_are_logged_with_source_and_rules_as_system(account):
    execute(account, PRICES, Command("open", {"symbol": "BTCUSDT", "side": "long", "leverage": 10, "margin": 50,
                                              "tp_levels": [{"price": 50500, "pct": 50}]}, "admin"), NOW)
    execute(account, PRICES, Command("set_alert", {"symbol": "BTCUSDT", "price": 49000, "note": "x"}), NOW)
    account.check_price("BTCUSDT", 50600, 50000, NOW + 60)
    found = [(e["actor"], e["kind"], e["position_id"], e["alert_id"]) for e in _events(account)]
    assert found == [("admin", "open", 1, None), ("opus", "set_alert", None, 1), ("system", "tp", 1, None)]
    assert json.loads(_events(account)[2]["detail"])["parent_id"] == 1


def test_rejected_request_is_not_logged(account):
    try:
        execute(account, PRICES, Command("close", {"position_id": 5}), NOW)
    except Exception:
        pass
    assert _events(account) == []


def test_funding_is_logged_as_system(account):
    execute(account, PRICES, Command("open", {"symbol": "BTCUSDT", "side": "long", "leverage": 10, "margin": 50}), NOW)
    account.apply_funding("BTCUSDT", 0.0001, 50000, NOW + 60)
    e = _events(account)[-1]
    assert (e["actor"], e["kind"], e["position_id"]) == ("system", "funding", 1)
    assert json.loads(e["detail"])["rate"] == 0.0001


# --- Fiyat uçları ---

def test_best_and_worst_price_kept_with_time_and_copied_to_parts(account):
    execute(account, PRICES, Command("open", {"symbol": "BTCUSDT", "side": "long", "leverage": 10, "margin": 90}), NOW)
    account.check_price("BTCUSDT", 50300, 49800, NOW + 60)
    account.check_price("BTCUSDT", 50200, 49900, NOW + 120)
    p = account.positions()[0]
    assert (p["best_price"], p["best_time"], p["worst_price"], p["worst_time"]) == (50300, NOW + 60, 49800, NOW + 60)
    execute(account, {"BTCUSDT": 50100.0}, Command("close", {"position_id": 1, "pct": 50}), NOW + 180)
    part = account.con.execute("SELECT * FROM trades WHERE parent_id = 1").fetchone()
    assert (part["best_price"], part["worst_time"]) == (50300, NOW + 60)


def test_unchanged_price_writes_nothing(account):
    execute(account, PRICES, Command("open", {"symbol": "BTCUSDT", "side": "long", "leverage": 10, "margin": 50}), NOW)
    account.check_price("BTCUSDT", 50100, 49900, NOW + 60)
    before = account.con.total_changes
    account.check_price("BTCUSDT", 50050, 49950, NOW + 120)
    assert account.con.total_changes == before


# --- Opus çağrısının akışı ---

_LINES = [
    {"type": "system", "subtype": "init", "session_id": "s1"},
    {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "mcp__piyasa__mumlar", "input": {"sembol": "BTCUSDT"}},
        {"type": "tool_use", "id": "t2", "name": "WebFetch", "input": {"url": "https://example.com"}}]}},
    {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "mum verisi"}]},
        {"type": "tool_result", "tool_use_id": "t2", "content": "sayfa açılamadı", "is_error": True}]}},
    {"type": "result", "subtype": "success", "is_error": False, "num_turns": 3, "total_cost_usd": 0.2,
     "usage": {"input_tokens": 10, "output_tokens": 5}, "modelUsage": {"opus": {"inputTokens": 10}},
     "structured_output": {"ozet": "tamam"}},
]


def test_call_reads_tool_results_errors_and_usage(tmp_path, monkeypatch):
    lines = tmp_path / "lines.jsonl"
    lines.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in _LINES) + "\n")
    fake = tmp_path / "bin" / "claude"
    fake.parent.mkdir()
    fake.write_text(f"#!/bin/sh\ncat > /dev/null\ncat {lines}\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{fake.parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path / "data")
    p = settings.Profile(id="test", name="Test Opus", short="Test", balance=100.0,
                         symbols={"BTCUSDT": settings.SYMBOLS["BTCUSDT"]}, period_s=86400, min_wait=5,
                         max_wait=240, style="")

    answer = asyncio.run(agent.call(p, "talimat", "istem"))
    tools = answer["tools"]
    assert [t["name"] for t in tools] == ["piyasa.mumlar", "WebFetch"]
    assert tools[0]["result"] == "mum verisi" and tools[0]["is_error"] is False
    assert tools[1]["result"] == "sayfa açılamadı" and tools[1]["is_error"] is True
    assert all(t["duration_s"] is not None and "started" not in t for t in tools)
    assert answer["usage"]["num_turns"] == 3 and answer["usage"]["usage"]["output_tokens"] == 5
    assert answer["decision"] == {"ozet": "tamam"}
