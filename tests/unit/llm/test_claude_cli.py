"""
file: test_claude_cli.py
importable: yok

claude komutuyla çalışan model istemcisinin testleri. claude çıktısı yanlış
okunursa karar ya da araç kaydı kaybolur. Komut isteğin araç takımından
yanlış kurulursa model verilmemiş bir araca erişir. Gerçek claude çağrılmaz,
yerine aynı akışı yazan küçük bir betik çalışır.

description: opus_trade.llm.claude_cli için birim testleri.
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

from opus_trade.llm.base import LLMError, LLMRequest
from opus_trade.llm.claude_cli import ClaudeCliClient
from opus_trade.llm.toolset import McpServer, Toolset

TOOLS = Toolset(builtin=("WebSearch",), servers={"piyasa": McpServer("opus_trade.tools.market", {"OPUS_PROFILE": "4"})},
                allowed=("WebSearch", "mcp__piyasa"))


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
elif mode == "text":
    out({"type": "result", "result": "serbest cevap", "usage": {}, "total_cost_usd": 0.01})
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


def _req(tmp_path, model: str, schema: dict | None = None, toolset: Toolset = TOOLS) -> LLMRequest:
    return LLMRequest("talimat", "istem", schema if schema is not None else {"type": "object"}, model, "high", 30,
                      tmp_path / "work", toolset, timeout_s=20)


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


def test_command_is_built_from_the_requests_toolset(tmp_path):
    """Komutta isteğin takımında olmayan araç yoktur. Takım boşsa claude'un bütün yerleşik araçları kapanır,
    sunucu, ajan ve izin listesi verilmez. Dosya ya da terminal aracı ancak takıma konursa açılır."""
    cmd = _client(tmp_path).command(_req(tmp_path, "ok"))
    assert cmd[cmd.index("--tools") + 1] == "WebSearch"
    assert cmd[cmd.index("--allowedTools") + 1] == "WebSearch,mcp__piyasa"
    server = json.loads(cmd[cmd.index("--mcp-config") + 1])["mcpServers"]["piyasa"]
    assert server["args"] == ["-m", "opus_trade.tools.market"]
    assert server["env"] == {"PYTHONPATH": str(tmp_path), "OPUS_PROFILE": "4"}
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == {"type": "object"}
    bare = _client(tmp_path).command(_req(tmp_path, "ok", toolset=Toolset()))
    assert bare[bare.index("--tools") + 1] == "" and "--allowedTools" not in bare and "--agents" not in bare
    assert json.loads(bare[bare.index("--mcp-config") + 1]) == {"mcpServers": {}}


def test_call_without_schema_returns_text(tmp_path):
    """Alt görev serbest metin döndürür. Şemasız istekte --json-schema verilmez, cevap text'e yazılır."""
    req = LLMRequest("talimat", "istem", None, "text", "low", 5, tmp_path / "work", TOOLS, timeout_s=20)
    assert "--json-schema" not in _client(tmp_path).command(req)
    res = asyncio.run(_client(tmp_path).call(req))
    assert res.decision is None and res.text == "serbest cevap" and res.cost_usd == 0.01


def test_extra_env_reaches_the_process(tmp_path):
    """İsteğin ortamı claude sürecine eklenir, örneğin MCP araçlarının zaman aşımı."""
    script = tmp_path / "claude"
    script.write_text(f"#!{sys.executable}\nimport json, os, sys\nsys.stdin.read()\n"
                      "print(json.dumps({'type': 'result', 'result': os.environ.get('OPUS_DENEME', '')}))\n")
    script.chmod(0o755)
    req = LLMRequest("t", "i", None, "x", "low", 5, tmp_path / "work", Toolset(), timeout_s=20,
                     env={"OPUS_DENEME": "var"})
    res = asyncio.run(ClaudeCliClient(Path(sys.executable), tmp_path, claude=str(script)).call(req))
    assert res.text == "var"
