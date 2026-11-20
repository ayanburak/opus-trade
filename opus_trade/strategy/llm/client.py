"""
file: client.py
importable: LLMRequest, LLMResult, LLMError, LLMClient, ClaudeCliClient, BUILTIN_TOOLS, RESEARCHER

LLMClient bir istemi modele gönderip şemaya uyan kararı döndüren parçanın
soyut sınıfıdır. ClaudeCliClient onu Burak'ın Claude aboneliğinden claude
komutunun print moduyla uygular: araç kullanımını izler, her araç için
on_tool'u çağırır ve son satırdaki yapılandırılmış çıktıyı döndürür. Çağrı
başarısız olursa LLMError fırlatır, o ana kadar kullanılan araçlar hatada
da taşınır.

description: Model çağrısının soyut sınıfı ve claude komutuyla uygulaması.
"""

# NOTE: Kullanıcı ayarları, hook'lar ve kullanıcının MCP sunucuları yüklenmez,
# oturum diske yazılmaz. Opus'a yalnızca web arama, web sayfası okuma, Haiku
# araştırmacısı ve piyasa araçları verilir. Dosya, terminal ve kod çalıştırma
# araçları güvenlik için bilerek verilmez.
#
# NOTE: claude komutu oturumu diske yazmasa da Haiku araştırmacısı
# kullanıldığında ~/.claude/projects altında çalışma klasörünün adıyla küçük
# bir alt ajan kaydı bırakıyor. Her çağrıdan sonra o çağrının klasörü silinir,
# klasör boş kalırsa o da silinir. Klasör adı çalışma yolundaki her / ve nokta
# yerine - konarak oluşur.
#
# XXX: Talimatta düşünce süreci ya da iç akıl yürütme gibi ifadeler
# kullanılmamalı. Opus'un güvenlik filtresi bunları akıl yürütmeyi dışarı
# çıkarma isteği sayıp çağrıyı reddediyor. Analiz ve gerekçe kelimeleri sorunsuz.

import asyncio
import json
import shutil
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# Opus'un piyasa araçları dışında kullanabildiği yerleşik araçlar. Agent
# araştırmacıyı çağırmak içindir. Kontrol API'si de listeyi buradan okur.
BUILTIN_TOOLS = ("WebSearch", "WebFetch", "Agent")

# Ajanın adı Opus'a görünür, Agent aracını bu adla çağırır.
RESEARCHER = {
    "arastirmaci": {
        "description": "Web'de haber ve piyasa araştırması yapıp özet getiren yardımcı. Ne araştırılacağını açıkça yaz.",
        "prompt": "Bir kripto ve emtia yatırımcısı için araştırma yapan yardımcısın. Verilen konuyu web'de araştır, "
                  "güncel ve güvenilir kaynaklara öncelik ver. Bulduklarını tarih ve kaynak belirterek, "
                  "kısa ve yoğun bir Türkçe özetle döndür. Yorum katmadan bulguları aktar.",
        "model": "haiku",
        "tools": ["WebSearch", "WebFetch"],
    }
}

# claude'un stderr çıktısından saklanan son bayt sayısı. Hata mesajına bunun
# yalnızca son beş yüz karakteri girer.
_STDERR_KEEP = 8192


@dataclass(frozen=True)
class LLMRequest:
    """Bir model çağrısı.

    Args:
        system: Talimat.
        prompt: Bu çağrının istemi.
        schema: Cevabın uyması gereken JSON şeması.
        model: Model takma adı, örneğin opus.
        effort: low, medium ya da high.
        max_turns: Araç adımlarının sert sınırı.
        work_dir: claude komutunun çalışma klasörü.
        tool_env: Piyasa araçları sunucusunun ortam değişkenleri.
        timeout_s: Çağrı bundan uzun sürerse süreç öldürülür.
    """

    system: str
    prompt: str
    schema: dict
    model: str
    effort: str
    max_turns: int
    work_dir: Path
    tool_env: dict[str, str] = field(default_factory=dict)
    timeout_s: int = 600


@dataclass(frozen=True)
class LLMResult:
    """Başarılı bir çağrının sonucu. cost_usd çağrının API liste fiyatıyla karşılığıdır, abonelikten ayrıca ücret alınmaz."""

    decision: dict
    raw: str
    tools: list[dict]
    usage: dict
    duration_s: float
    cost_usd: float | None


class LLMError(Exception):
    def __init__(self, message: str, tools: list[dict] | None = None) -> None:
        super().__init__(message)
        self.tools = tools or []


class LLMClient(ABC):
    @abstractmethod
    async def call(self, req: LLMRequest, on_tool: Callable[[dict], None] | None = None) -> LLMResult:
        """İstemi modele gönderir ve kararı döndürür.

        Raises:
            LLMError: Çağrı başarısız olursa, zaman aşımına ya da adım sınırına uğrarsa veya karar okunamazsa.
        """


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
    return {"name": name.replace("mcp__piyasa__", "piyasa."), "title": title, "input": inp, "sub": sub,
            "id": block.get("id"), "result": None, "is_error": None, "duration_s": None}


class ClaudeCliClient(LLMClient):
    """claude komutunu alt süreç olarak çalıştıran istemci.

    Args:
        python: Piyasa araçları sunucusunu çalıştıracak Python yorumlayıcısı.
        root: Projenin kökü, araç sunucusunun PYTHONPATH'i.
        claude: claude komutunun adı ya da yolu.
    """

    def __init__(self, python: Path, root: Path, claude: str = "claude") -> None:
        self.python = python
        self.root = root
        self.claude = claude

    def command(self, req: LLMRequest) -> list[str]:
        env = {"PYTHONPATH": str(self.root), **req.tool_env}
        servers = {"piyasa": {"type": "stdio", "command": str(self.python), "args": ["-m", "opus_trade.tools.market"],
                              "env": env}}
        return [
            self.claude, "-p",
            "--model", req.model,
            "--effort", req.effort,
            "--no-session-persistence",
            "--setting-sources", "",
            "--strict-mcp-config",
            "--mcp-config", json.dumps({"mcpServers": servers}),
            "--agents", json.dumps(RESEARCHER, ensure_ascii=False),
            "--tools", ",".join(BUILTIN_TOOLS),
            "--allowedTools", ",".join((*BUILTIN_TOOLS, "mcp__piyasa")),
            "--permission-mode", "dontAsk",
            "--max-turns", str(req.max_turns),
            "--system-prompt", req.system,
            "--output-format", "stream-json",
            "--verbose",
            "--json-schema", json.dumps(req.schema, ensure_ascii=False),
        ]

    async def call(self, req: LLMRequest, on_tool: Callable[[dict], None] | None = None) -> LLMResult:
        started = time.monotonic()
        req.work_dir.mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            *self.command(req), cwd=req.work_dir, limit=16 * 1024 * 1024,
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
        if result.get("is_error") or not result.get("structured_output"):
            raise LLMError(f"Model geçerli bir karar döndürmedi: {str(result.get('result'))[:500]}", tools)
        return LLMResult(decision=result["structured_output"], raw=json.dumps(result, ensure_ascii=False),
                         tools=tools,
                         usage={"usage": result.get("usage"), "model_usage": result.get("modelUsage"),
                                "num_turns": result.get("num_turns")},
                         duration_s=round(time.monotonic() - started, 1), cost_usd=result.get("total_cost_usd"))
