"""
file: test_team_server.py
importable: yok

Ekip sunucusunun uçtan uca testi. Sunucu claude'un yaptığı gibi ayrı bir
süreç olarak python -m ile başlar ve MCP üzerinden çağrılır. Alt görevleri
gerçek claude yerine aynı akışı yazan sahte bir betik çalıştırır. Sunucu
ortamdan yanlış okursa, claude'un oturum değişkenlerini alt göreve
taşırsa ya da kaydı yazmazsa birim testleri bunu göremez.

description: tools.team için uçtan uca test.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from opus_trade.team import log

ROOT = Path(__file__).resolve().parents[2]

# Sahte claude: aldığı modeli ve ortamda claude'un oturum değişkeni kalıp
# kalmadığını cevap olarak yazar, bir web araması da kaydeder.
FAKE = '''import json, os, sys
model = sys.argv[sys.argv.index("--model") + 1]
task = sys.stdin.read()
out = lambda m: print(json.dumps(m), flush=True)
out({"type": "system", "session_id": "alt"})
out({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "w", "name": "WebSearch",
     "input": {"query": task}}]}})
out({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "w", "content": "sonuç"}]}})
leak = "CLAUDECODE" in os.environ or "CLAUDE_CODE_SESSION_ID" in os.environ
out({"type": "result", "result": f"{model}:{task}:sizinti={leak}:anahtar={os.environ.get('CLAUDE_CODE_OAUTH_TOKEN')}",
     "usage": {"input_tokens": 5, "output_tokens": 2}, "total_cost_usd": 0.001})
'''


def test_team_server_runs_tasks_as_a_separate_process(tmp_path):
    """Sunucu kotayı, kayıt dosyasını ve claude komutunu ortamdan alır. Alt görevin claude'u oturum değişkenlerini
    görmez, abonelik anahtarını claude'un beklediği adla görür. claude sunucuya anahtarı kendi adıyla geçirmez,
    istemci onu OPUS_CLAUDE_TOKEN adıyla verir. Kayıt dosyası her görevi taşır, kota aşımı reddedilir."""
    fake = tmp_path / "claude"
    fake.write_text(f"#!{sys.executable}\n" + FAKE)
    fake.chmod(0o755)
    path = tmp_path / "ekip.jsonl"
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "PYTHONPATH": str(ROOT),
           "OPUS_TEAM_QUOTA": json.dumps({"haiku": 2, "sonnet": 1}), "OPUS_TEAM_LOG": str(path),
           "OPUS_TEAM_DEADLINE_S": "120", "OPUS_CLAUDE": str(fake), "CLAUDECODE": "1",
           "CLAUDE_CODE_SESSION_ID": "ana", "OPUS_CLAUDE_TOKEN": "anahtar"}
    params = StdioServerParameters(command=sys.executable, args=["-m", "opus_trade.tools.team"], env=env,
                                   cwd=str(tmp_path))

    async def go() -> str:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as s:
                await s.initialize()
                res = await s.call_tool("gorev_ver", {"gorevler": [{"rol": "haiku", "gorev": "fed"},
                                                                   {"rol": "sonnet", "gorev": "cpi"},
                                                                   {"rol": "opus", "gorev": "analiz"}]})
                return "".join(getattr(c, "text", "") for c in res.content)

    text = asyncio.run(go())
    assert "haiku:fed:sizinti=False:anahtar=anahtar" in text and "sonnet:cpi:sizinti=False" in text
    assert "Görev 3 çalışmadı: bu çağrıda opus" in text and text.endswith("opus 0, sonnet 0, haiku 1.")
    recs = log.collect(path)
    assert sorted(r["role"] for r in recs) == ["haiku", "sonnet"]
    assert all(r["tools"][0]["name"] == "WebSearch" and r["cost_usd"] == 0.001 for r in recs)
    assert not (tmp_path / "ekip" / "gorev-1").exists()
