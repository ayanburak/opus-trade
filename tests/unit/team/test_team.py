"""
file: test_team.py
importable: yok

Ekip katmanının testleri. Kota tutmazsa bir çağrı aboneliğin limitini
tüketir. Kayıt dosyası okunmazsa alt görevlerin neye harcandığı görünmez.
Alt ajanın takımına ekip ya da yazma aracı girerse alt ajan başka ajan
başlatır ya da hesaba dokunur. Gerçek model çağrılmaz, sahte bir istemci
görevleri kaydeder.

description: opus_trade.team ve tools.team için birim testleri.
"""

import asyncio
import json
import time

from opus_trade.llm.base import LLMError, LLMResult
from opus_trade.llm.claude_cli import ClaudeCliClient
from opus_trade.llm.toolset import McpServer, Toolset
from opus_trade.strategy.llm.toolkit import TeamGrant, team_deadline, toolset
from opus_trade.team import log
from opus_trade.team.policy import TeamPolicy
from opus_trade.team.roles import ROLES, toolset as team_toolset
from opus_trade.team.runner import TeamRunner
from opus_trade.tools import team as team_server


class FakeLLM:
    """Her görevi kaydeder, aynı anda kaç görevin çalıştığını sayar."""

    def __init__(self, fail: str = "", delay: float = 0.01) -> None:
        self.requests = []
        self.running = 0
        self.peak = 0
        self.fail = fail
        self.delay = delay

    async def call(self, req, on_tool=None):
        self.requests.append(req)
        self.running += 1
        self.peak = max(self.peak, self.running)
        req.work_dir.mkdir(parents=True, exist_ok=True)
        await asyncio.sleep(self.delay)
        self.running -= 1
        tools = [{"name": "WebSearch", "title": "altın", "input": {}, "sub": False, "id": "t1", "result": "x" * 9000,
                  "is_error": False, "duration_s": 0.5}]
        if self.fail and self.fail in req.prompt:
            raise LLMError("zaman aşımı", tools)
        return LLMResult(None, "{}", tools, {"usage": {"input_tokens": 10, "output_tokens": 5}}, 1.0, 0.01,
                         text=f"cevap: {req.prompt}")


def runner(tmp_path, quota, llm=None, deadline=600.0, parallel=4) -> TeamRunner:
    return TeamRunner(llm or FakeLLM(), quota, {"OPUS_PROFILE": "4"}, tmp_path / "ekip", tmp_path / "ekip.jsonl",
                      time.monotonic() + deadline, parallel)


# --- Politika ---

def test_remaining_is_the_smaller_of_call_quota_and_what_is_left_today():
    """Günlük sınıra yaklaşınca çağrı kotası daralır, sınır geçildiyse sıfırdır, eksi olmaz."""
    p = TeamPolicy({"opus": 3, "haiku": 10}, {"opus": 15, "haiku": 100})
    assert p.remaining({}) == {"opus": 3, "sonnet": 0, "haiku": 10}
    assert p.remaining({"haiku": 95, "opus": 20}) == {"opus": 0, "sonnet": 0, "haiku": 5}


# --- Kayıt ---

def test_log_is_read_in_order_skips_broken_lines_and_is_removed(tmp_path):
    """Çekirdek dosyayı okuyunca siler, bir sonraki çağrıya eski görev karışmaz."""
    path = tmp_path / "ekip.jsonl"
    log.write(path, {"role": "haiku"})
    with open(path, "a") as f:
        f.write("{bozuk\n")
    log.write(path, {"role": "opus"})
    assert [r["role"] for r in log.collect(path)] == ["haiku", "opus"]
    assert not path.exists() and log.collect(path) == []


# --- Çalıştırıcı ---

def test_call_quota_refuses_the_extra_task_and_tells_what_is_left(tmp_path):
    """Kotayı aşan görev çalışmaz, Opus'a Türkçe ret ve kalan hak döner. Kota ikinci araç çağrısında da düşer."""
    llm = FakeLLM()
    r = runner(tmp_path, {"haiku": 2}, llm)
    out = asyncio.run(r.run([("haiku", "bir"), ("haiku", "iki"), ("haiku", "üç"), ("sonnet", "dört")]))
    assert "Görev 1, haiku:\ncevap: bir" in out and "Görev 2, haiku:\ncevap: iki" in out
    assert "Görev 3 çalışmadı: bu çağrıda haiku için görev hakkın kalmadı" in out
    assert "Görev 4 çalışmadı: bu çağrıda sonnet" in out
    assert out.endswith("Bu çağrıda kalan görev hakkın: opus 0, sonnet 0, haiku 0.")
    assert "çalışmadı" in asyncio.run(r.run([("haiku", "beş")])) and len(llm.requests) == 2


def test_unknown_role_and_empty_task_are_refused(tmp_path):
    out = asyncio.run(runner(tmp_path, {"haiku": 5}).run([("gpt", "x"), ("haiku", "  ")]))
    assert "gpt diye bir rol yok" in out and "görev metni boş" in out


def test_parallel_limit_holds_even_with_a_big_quota(tmp_path):
    """Elli görev birden açılırsa sunucunun belleği yetmez. Aynı anda en fazla parallel kadar görev çalışır."""
    llm = FakeLLM(delay=0.02)
    asyncio.run(runner(tmp_path, {"haiku": 10}, llm, parallel=3).run([("haiku", str(i)) for i in range(10)]))
    assert len(llm.requests) == 10 and llm.peak == 3


def test_each_task_is_a_schemaless_call_with_the_roles_settings_and_leaves_no_folder(tmp_path):
    """Alt görev rolün modeli, effort'u ve adım sınırıyla şemasız çalışır. Süresi kalan zamanı aşmaz. Geçici
    çalışma klasörü görev bitince silinir."""
    llm = FakeLLM()
    asyncio.run(runner(tmp_path, {"opus": 1}, llm, deadline=200).run([("opus", "analiz")]))
    req = llm.requests[0]
    assert req.schema is None and req.model == "opus" and req.max_turns == ROLES["opus"].max_turns
    assert req.system == ROLES["opus"].system and req.timeout_s <= 200
    assert not req.work_dir.exists()


def test_every_task_is_logged_with_its_usage_and_errors(tmp_path):
    """Hata veren görev de kaydedilir, araçları ve hatasıyla. Uzun araç sonucu kısaltılır, kimliği atılır."""
    llm = FakeLLM(fail="bozuk")
    out = asyncio.run(runner(tmp_path, {"haiku": 2}, llm).run([("haiku", "iyi"), ("haiku", "bozuk")]))
    assert "Görev 2, haiku:\nHata: zaman aşımı" in out
    recs = log.collect(tmp_path / "ekip.jsonl")
    assert sorted(r["task"] for r in recs) == ["bozuk", "iyi"]
    good = next(r for r in recs if r["task"] == "iyi")
    bad = next(r for r in recs if r["task"] == "bozuk")
    assert good["result"] == "cevap: iyi" and good["usage"]["usage"]["input_tokens"] == 10 and good["error"] is None
    assert bad["error"] == "zaman aşımı" and bad["tools"][0]["name"] == "WebSearch"
    assert len(good["tools"][0]["result"]) == 4000 and "id" not in good["tools"][0]


def test_no_task_starts_when_the_call_has_no_time_left(tmp_path):
    """Süre bitmek üzereyse görev başlamaz, başlasa ana çağrının zaman aşımıyla birlikte kesilirdi."""
    llm = FakeLLM()
    out = asyncio.run(runner(tmp_path, {"haiku": 1}, llm, deadline=5).run([("haiku", "x")]))
    assert "süresi bu görevi çalıştırmaya yetmedi" in out and llm.requests == []
    assert log.collect(tmp_path / "ekip.jsonl")[0]["error"]


# --- Araç takımları ---

def test_sub_agents_get_no_team_tool_and_no_write_tool():
    """Alt ajan başka alt ajan başlatamaz, hesaba dokunamaz, dosya ya da terminal kullanamaz."""
    ts = team_toolset({"OPUS_PROFILE": "4"})
    assert ts.builtin == ("WebSearch", "WebFetch") and list(ts.servers) == ["piyasa"] and not ts.agents
    assert ts.allowed == ("WebSearch", "WebFetch", "mcp__piyasa")


def test_main_toolset_has_the_team_server_only_with_a_grant(tmp_path):
    """Ekip sunucusu kotayı, kayıt dosyasını ve süresini ortamdan alır. Agent aracı yoktur."""
    assert list(toolset({}).servers) == ["piyasa"]
    ts = toolset({"OPUS_PROFILE": "4"}, TeamGrant({"haiku": 3}, tmp_path / "e.jsonl", 450))
    team = ts.servers["ekip"]
    assert team.model and team.env["OPUS_PROFILE"] == "4" and json.loads(team.env["OPUS_TEAM_QUOTA"]) == {"haiku": 3}
    assert team.env["OPUS_TEAM_DEADLINE_S"] == "450" and "Agent" not in ts.builtin and "mcp__ekip" in ts.allowed


def test_times_nest_task_tool_and_call():
    """Alt görev araç zaman aşımından, araç zaman aşımı ana çağrının süresinden önce biter."""
    for timeout in (120, 600, 1800, 3600):
        deadline = team_deadline(timeout)
        grant = TeamGrant({}, None, deadline)
        assert deadline < grant.tool_timeout_ms() / 1000 < timeout


def test_client_tells_a_model_server_its_claude_command(tmp_path):
    """Ekip sunucusu alt görevleri ana çağrıyla aynı claude komutuyla çalıştırır, piyasa sunucusu bunu almaz."""
    from opus_trade.llm.base import LLMRequest
    ts = Toolset(servers={"piyasa": McpServer("m"), "ekip": McpServer("t", model=True)})
    cmd = ClaudeCliClient(tmp_path / "py", tmp_path, claude="claude2").command(
        LLMRequest("s", "p", None, "opus", "high", 5, tmp_path, ts))
    servers = json.loads(cmd[cmd.index("--mcp-config") + 1])["mcpServers"]
    assert servers["ekip"]["env"]["OPUS_CLAUDE"] == "claude2" and "OPUS_CLAUDE" not in servers["piyasa"]["env"]


# --- Ekip sunucusu ---

def test_gorev_ver_runs_through_mcp(tmp_path, monkeypatch):
    """Opus aracı MCP üzerinden adıyla ve Türkçe parametreleriyle çağırır."""
    llm = FakeLLM()
    monkeypatch.setattr(team_server, "_runner", runner(tmp_path, {"haiku": 1}, llm))
    names = [t.name for t in asyncio.run(team_server.server.list_tools())]
    assert names == ["gorev_ver"]
    res = asyncio.run(team_server.server.call_tool("gorev_ver", {"gorevler": [{"rol": "haiku", "gorev": "fed"}]}))
    text = "".join(getattr(c, "text", "") for c in res.content)
    assert "cevap: fed" in text and len(llm.requests) == 1


def test_server_builds_its_runner_from_the_environment(tmp_path, monkeypatch):
    """Kota, kayıt dosyası, claude komutu ve piyasa değişkenleri ortamdan gelir."""
    monkeypatch.setenv("OPUS_TEAM_QUOTA", json.dumps({"sonnet": 2}))
    monkeypatch.setenv("OPUS_TEAM_LOG", str(tmp_path / "e.jsonl"))
    monkeypatch.setenv("OPUS_CLAUDE", "claude2")
    monkeypatch.setenv("OPUS_MARKET_DB", "/m.db")
    r = team_server._make()
    assert r.left["sonnet"] == 2 and r.llm.claude == "claude2" and r.market_env["OPUS_MARKET_DB"] == "/m.db"
    assert r.work_dir == tmp_path / "ekip"


def test_session_variables_are_dropped_but_the_key_stays():
    """Alt görevin claude'u kendini ana oturumun içinde sanmasın. Abonelik anahtarı ve ayar klasörü kalır."""
    env = {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "s", "CLAUDE_CODE_ENTRYPOINT": "sdk", "CLAUDE_PID": "9",
           "CLAUDE_CODE_OAUTH_TOKEN": "gizli", "CLAUDE_CONFIG_DIR": "/c", "HOME": "/h", "PATH": "/b"}
    team_server.clean_env(env)
    assert env == {"CLAUDE_CODE_OAUTH_TOKEN": "gizli", "CLAUDE_CONFIG_DIR": "/c", "HOME": "/h", "PATH": "/b"}


def test_subscription_key_reaches_the_team_server_under_its_own_name(tmp_path, monkeypatch):
    """claude MCP sunucusuna CLAUDE_CODE_OAUTH_TOKEN geçirmez, alt görev giriş yapamazdı, 24.11 sunucuda görüldü.
    İstemci anahtarı model çağıran sunucu varsa ayrı adla claude'un ortamına koyar, komut satırına yazmaz. Sunucu
    başlarken eski adına çevirir."""
    from opus_trade.llm.base import LLMRequest
    from opus_trade.llm.claude_cli import process_env
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "gizli")
    with_team = LLMRequest("s", "p", None, "opus", "high", 5, tmp_path,
                           Toolset(servers={"ekip": McpServer("t", model=True)}))
    env = process_env(with_team)
    assert env["OPUS_CLAUDE_TOKEN"] == "gizli"
    assert "gizli" not in " ".join(ClaudeCliClient(tmp_path / "py", tmp_path).command(with_team))
    assert process_env(LLMRequest("s", "p", None, "opus", "high", 5, tmp_path, Toolset())) is None
    served = {"OPUS_CLAUDE_TOKEN": "gizli", "HOME": "/h"}
    team_server.restore_token(served)
    assert served == {"CLAUDE_CODE_OAUTH_TOKEN": "gizli", "HOME": "/h"}
