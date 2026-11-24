"""
file: claude_cli.py
importable: ClaudeCliClient, process_env, TOKEN_VAR, TOKEN_PASS

ClaudeCliClient LLMClient'ı Burak'ın Claude aboneliğinden claude komutunun
print moduyla uygular. Komutu isteğin araç takımından kurar, araç kullanımını
izler, her araç için on_tool'u çağırır ve son satırdaki cevabı döndürür.
Şemalı istekte cevap yapılandırılmış çıktıdır, şemasızda modelin son metni.

    llm = ClaudeCliClient(Path(sys.executable), root)

description: Model çağrısının claude komutuyla uygulaması.
"""

# NOTE: Kullanıcı ayarları, hook'lar ve kullanıcının MCP sunucuları yüklenmez,
# oturum diske yazılmaz. Modelin hangi araçları kullanabileceğini yalnızca
# isteğin araç takımı söyler. Takımda yerleşik araç yoksa --tools boş verilir
# ve claude'un bütün yerleşik araçları kapanır.
#
# NOTE: MCP sunucuları istemcinin Python yorumlayıcısıyla, proje kökü
# PYTHONPATH'te olacak şekilde başlar. Takım yalnızca modülü ve ortamı verir.
# Model çağıran sunucu, ekip sunucusu gibi, alt görevleri aynı yorumlayıcı ve
# aynı claude komutuyla çalıştırır, komutun adı ona OPUS_CLAUDE ile gider.
# Sunucu claude'un ortamını devralır ama abonelik anahtarını almaz, anahtar
# ona process_env() ile ayrı adla gider.
#
# NOTE: claude komutu oturumu diske yazmasa da Agent aracıyla bir alt ajan
# çalıştığında ~/.claude/projects altında çalışma klasörünün adıyla küçük
# bir alt ajan kaydı bırakıyor. Her çağrıdan sonra o çağrının klasörü silinir,
# klasör boş kalırsa o da silinir. Klasör adı çalışma yolundaki her / ve nokta
# yerine - konarak oluşur.
#
# XXX: Talimatta düşünce süreci ya da iç akıl yürütme gibi ifadeler
# kullanılmamalı. Opus'un güvenlik filtresi bunları akıl yürütmeyi dışarı
# çıkarma isteği sayıp çağrıyı reddediyor. Analiz ve gerekçe kelimeleri sorunsuz.

import asyncio
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable

from .base import LLMClient, LLMError, LLMRequest, LLMResult

# Abonelik anahtarının claude'daki adı ve model çağıran MCP sunucusuna
# taşındığı ad.
TOKEN_VAR = "CLAUDE_CODE_OAUTH_TOKEN"
TOKEN_PASS = "OPUS_CLAUDE_TOKEN"

# claude'un stderr çıktısından saklanan son bayt sayısı. Hata mesajına bunun
# yalnızca son beş yüz karakteri girer.
_STDERR_KEEP = 8192


# --- claude komutu ---

def _trace_dir(work_dir: Path) -> Path:
    return Path.home() / ".claude" / "projects" / str(work_dir).replace("/", "-").replace(".", "-")


def _clean_trace(work_dir: Path, sessions: set[str]) -> None:
    d = _trace_dir(work_dir)
    for s in sessions:
        shutil.rmtree(d / s, ignore_errors=True)
    if d.is_dir() and not any(d.iterdir()):
        d.rmdir()


def _result_text(content: Any) -> str:
    if isinstance(content, list):
        return "\n".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return "" if content is None else str(content)


# MCP araçlarının adı mcp__sunucu__araç biçimindedir, arayüzde sunucu.araç
# olarak görünür, örneğin piyasa.mumlar.
def _short_name(name: str) -> str:
    if name.startswith("mcp__") and name.count("__") >= 2:
        server, tool = name[5:].split("__", 1)
        return f"{server}.{tool}"
    return name


# Bir araç çağrısının arayüzde gösterilecek kısa hali. Girdinin tamamı ayrıca
# saklanır, burada yalnızca okunabilir bir başlık üretilir.
def _tool_record(block: dict, sub: bool) -> dict:
    name, inp = block["name"], block.get("input", {})
    if name == "WebSearch":
        title = inp.get("query", "")
    elif name == "WebFetch":
        title = inp.get("url", "")
    elif name == "Agent":
        title = inp.get("description") or inp.get("prompt", "")[:120]
    else:
        title = ", ".join(f"{k}={v}" for k, v in inp.items())
    return {"name": _short_name(name), "title": title, "input": inp, "sub": sub,
            "id": block.get("id"), "result": None, "is_error": None, "duration_s": None}


# claude MCP sunucularına abonelik anahtarını geçirmez, 24.11'de sunucuda
# doğrulandı: CLAUDE_CODE_OAUTH_TOKEN düşer, diğer değişkenler geçer. Model
# çağıran sunucu varsa anahtar claude sürecinin ortamına OPUS_CLAUDE_TOKEN
# adıyla da konur, sunucu onu eski adına çevirir. Anahtar komut satırına
# hiç yazılmaz, ps ile görünmez.
def process_env(req: LLMRequest) -> dict[str, str] | None:
    """claude sürecinin ortamını döndürür. Eklenecek bir şey yoksa None, süreç çekirdeğin ortamını devralır."""
    extra = dict(req.env)
    token = os.environ.get(TOKEN_VAR)
    if token and any(s.model for s in req.toolset.servers.values()):
        extra[TOKEN_PASS] = token
    return {**os.environ, **extra} if extra else None


class ClaudeCliClient(LLMClient):
    """claude komutunu alt süreç olarak çalıştıran istemci.

    Args:
        python: MCP sunucularını çalıştıracak Python yorumlayıcısı.
        root: Projenin kökü, sunucuların PYTHONPATH'i.
        claude: claude komutunun adı ya da yolu.
    """

    def __init__(self, python: Path, root: Path, claude: str = "claude") -> None:
        self.python = python
        self.root = root
        self.claude = claude

    def command(self, req: LLMRequest) -> list[str]:
        ts = req.toolset
        servers = {name: {"type": "stdio", "command": str(self.python), "args": ["-m", srv.module],
                          "env": {"PYTHONPATH": str(self.root), **srv.env,
                                  **({"OPUS_CLAUDE": self.claude} if srv.model else {})}}
                   for name, srv in ts.servers.items()}
        cmd = [
            self.claude, "-p",
            "--model", req.model,
            "--effort", req.effort,
            "--no-session-persistence",
            "--setting-sources", "",
            "--strict-mcp-config",
            "--mcp-config", json.dumps({"mcpServers": servers}),
        ]
        if ts.agents:
            cmd += ["--agents", json.dumps(ts.agents, ensure_ascii=False)]
        cmd += ["--tools", ",".join(ts.builtin)]
        if ts.allowed:
            cmd += ["--allowedTools", ",".join(ts.allowed)]
        cmd += [
            "--permission-mode", "dontAsk",
            "--max-turns", str(req.max_turns),
            "--system-prompt", req.system,
            "--output-format", "stream-json",
            "--verbose",
        ]
        if req.schema is not None:
            cmd += ["--json-schema", json.dumps(req.schema, ensure_ascii=False)]
        return cmd

    async def call(self, req: LLMRequest, on_tool: Callable[[dict], None] | None = None) -> LLMResult:
        started = time.monotonic()
        req.work_dir.mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            *self.command(req), cwd=req.work_dir, limit=16 * 1024 * 1024,
            env=process_env(req),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        proc.stdin.write(req.prompt.encode())
        await proc.stdin.drain()
        proc.stdin.close()

        tools: list[dict] = []
        by_id: dict[str, tuple[dict, float]] = {}
        sessions: set[str] = set()
        tail: list[str] = []
        result: dict | None = None

        # Akış satır satır okunur, her satır bir mesajdır. Araç kullanımları
        # geldikçe bildirilir. Araç sonuçları claude'un araca cevabı olarak
        # user mesajında gelir, süre isteğin ve sonucun satırları arasındaki
        # zamandır. Son satırdaki result mesajı kararı taşır.
        async def read() -> None:
            nonlocal result
            async for raw in proc.stdout:
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                tail[:] = (tail + [line])[-3:]
                try:
                    m = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if m.get("session_id"):
                    sessions.add(m["session_id"])
                if m.get("type") == "assistant":
                    for block in m.get("message", {}).get("content", []):
                        if block.get("type") == "tool_use" and block.get("name") != "StructuredOutput":
                            rec = _tool_record(block, bool(m.get("parent_tool_use_id")))
                            tools.append(rec)
                            by_id[rec["id"]] = (rec, time.monotonic())
                            if on_tool:
                                on_tool(dict(rec))
                elif m.get("type") == "user":
                    content = m.get("message", {}).get("content", [])
                    for block in content if isinstance(content, list) else []:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            found = by_id.get(block.get("tool_use_id"))
                            if found:
                                rec, t0 = found
                                rec.update(result=_result_text(block.get("content")),
                                           is_error=bool(block.get("is_error")),
                                           duration_s=round(time.monotonic() - t0, 1))
                elif m.get("type") == "result":
                    result = m

        # stderr de çağrı sürerken okunur. Okunmazsa claude çok yazdığında boru
        # dolar, claude yazmak için bekler ve çağrı zaman aşımına kadar takılır.
        err = bytearray()

        async def read_err() -> None:
            while chunk := await proc.stderr.read(65536):
                err.extend(chunk)
                del err[:-_STDERR_KEEP]

        try:
            await asyncio.wait_for(asyncio.gather(read(), read_err(), proc.wait()), req.timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise LLMError(f"Model {req.timeout_s} saniye içinde karar vermedi, o ana kadar {len(tools)} araç "
                           f"kullandı.", tools) from None
        finally:
            _clean_trace(req.work_dir, sessions)

        if result is None:
            text = err.decode(errors="replace") or "\n".join(tail)
            raise LLMError(f"claude komutu sonuç vermedi: {text[-500:]}", tools)
        if result.get("subtype") == "error_max_turns":
            raise LLMError(f"Model {req.max_turns} adım sınırına ulaştı ve karar yazmadı.", tools)
        # Şemalı çağrının hata metni eskisi gibi kalır, model erişiminin limit
        # uyarısı bu metnin içinde limit ifadelerini arar.
        if result.get("is_error"):
            head = "Model geçerli bir karar döndürmedi" if req.schema is not None else "Model hata döndürdü"
            raise LLMError(f"{head}: {str(result.get('result'))[:500]}", tools)
        usage = {"usage": result.get("usage"), "model_usage": result.get("modelUsage"),
                 "num_turns": result.get("num_turns")}
        raw = json.dumps(result, ensure_ascii=False)
        duration = round(time.monotonic() - started, 1)
        if req.schema is None:
            return LLMResult(decision=None, raw=raw, tools=tools, usage=usage, duration_s=duration,
                             cost_usd=result.get("total_cost_usd"), text=str(result.get("result") or ""))
        if not result.get("structured_output"):
            raise LLMError(f"Model geçerli bir karar döndürmedi: {str(result.get('result'))[:500]}", tools)
        return LLMResult(decision=result["structured_output"], raw=raw, tools=tools, usage=usage,
                         duration_s=duration, cost_usd=result.get("total_cost_usd"))
