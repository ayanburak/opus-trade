"""
file: team.py
importable: server, MARKET_VARS, clean_env, restore_token

Opus'un alt ajanlara görev verdiği ekip MCP sunucusu. Tek aracı gorev_ver
bir görev listesi alır, her görev bir rol ve bir metindir. Görevleri
team.runner.TeamRunner çalıştırır, sonuçları ve kalan hakkı tek metin
olarak döndürür. Sunucuyu claude komutu her ana çağrıda başlatır ve çağrı
bitince kapatır.

Ortam değişkenleri:

    OPUS_TEAM_QUOTA       rolden bu çağrıda kalan hakka, JSON
    OPUS_TEAM_LOG         alt görev kayıtlarının dosyası
    OPUS_TEAM_DEADLINE_S  sunucu başladıktan sonra görevlerin bitmesi gereken süre
    OPUS_CLAUDE           claude komutu, istemci verir
    OPUS_CLAUDE_TOKEN     abonelik anahtarı, istemci claude'un ortamıyla verir
    PYTHONPATH            projenin kökü, istemci verir

description: Opus'un alt görev verdiği ekip aracı.
"""

# NOTE: Bu dosya bir kuruluş noktası gibi davranır. Somut ClaudeCliClient
# burada, ortamdan kurulur. Alt görevler ana çağrıyla aynı yorumlayıcıyı ve
# aynı claude komutunu kullanır. claude MCP sunucusuna abonelik anahtarını
# geçirmez, istemci onu OPUS_CLAUDE_TOKEN adıyla verir, sunucu başlarken
# claude'un beklediği ada geri koyar. Anahtar hiçbir yere yazılmaz.
#
# NOTE: Sunucu claude'un ortamını devralır, claude'un oturum değişkenleri de
# gelir: CLAUDECODE, CLAUDE_CODE_SESSION_ID ve benzerleri. Alt görevin claude'u
# kendini o oturumun içinde sanmasın diye bunlar başta silinir. Anahtar ve
# CLAUDE_CONFIG_DIR kalır.
#
# NOTE: Sunucu veritabanına yazmaz. Günlük kota hesaplanmış olarak gelir,
# kayıt dosyasını çekirdek okur.
#
# XXX: Opus aracı sunucu adı, araç adı, parametre adları ve docstring ile
# görür. Bunlar Türkçedir.

import json
import os
import sys
import time
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, Field

from ..llm.claude_cli import TOKEN_PASS, TOKEN_VAR, ClaudeCliClient
from ..team.roles import ROLE_KEYS
from ..team.runner import TeamRunner

server = MCPServer("ekip")

# Alt ajanların piyasa araçlarına geçen değişkenler, ana çağrınınkilerle aynı.
MARKET_VARS = ("OPUS_MARKET_DB", "OPUS_CORE_DB", "OPUS_ACCOUNT", "OPUS_PROFILE", "OPUS_AS_OF")

# claude'un çocuk süreçlerine verdiği oturum değişkenleri. Anahtar,
# CLAUDE_CODE_OAUTH_TOKEN, ve ayar klasörü bu listede değildir.
_SESSION_KEEP = ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR")

_started = time.monotonic()
_runner: TeamRunner | None = None


def clean_env(env: dict) -> None:
    """claude'un oturum değişkenlerini verilen ortamdan siler, anahtarı ve ayar klasörünü bırakır."""
    for k in list(env):
        if k in _SESSION_KEEP:
            continue
        if k == "CLAUDECODE" or k.startswith("CLAUDE_CODE_") or k in ("CLAUDE_PID", "CLAUDE_PROJECT_DIR",
                                                                         "CLAUDE_EFFORT"):
            del env[k]


def restore_token(env: dict) -> None:
    """İstemcinin ayrı adla geçirdiği abonelik anahtarını claude'un beklediği ada geri koyar."""
    if TOKEN_PASS in env:
        env.setdefault(TOKEN_VAR, env.pop(TOKEN_PASS))


def _make() -> TeamRunner:
    quota = json.loads(os.environ.get("OPUS_TEAM_QUOTA") or "{}")
    log_path = Path(os.environ["OPUS_TEAM_LOG"])
    deadline = _started + float(os.environ.get("OPUS_TEAM_DEADLINE_S") or 300)
    root = Path(os.environ.get("PYTHONPATH", ".").split(os.pathsep)[0])
    llm = ClaudeCliClient(Path(sys.executable), root, claude=os.environ.get("OPUS_CLAUDE") or "claude")
    market = {k: os.environ[k] for k in MARKET_VARS if k in os.environ}
    return TeamRunner(llm, quota, market, log_path.parent / "ekip", log_path, deadline)


class Gorev(BaseModel):
    rol: str = Field(description=f"Görevin gideceği rol: {', '.join(ROLE_KEYS)}.")
    gorev: str = Field(description="Görevin kendisi. Alt ajan seninle konuşamaz ve istemini görmez, görevi açık ve "
                                   "kendi içinde yeterli yaz: ne aranacak, hangi zaman aralığı, ne biçimde dönecek.")


@server.tool()
async def gorev_ver(gorevler: list[Gorev]) -> str:
    """Görevleri alt ajanlara verir ve sonuçlarını getirir.

    Görevler aynı anda çalışır. Her alt ajan web'de arar, sayfa okur ve piyasa araçlarını kullanır, hesabına
    dokunamaz. Her görevin sonucu görev numarasıyla döner, en sonda bu çağrıda kalan görev hakkın yazar. Hakkı
    aşan görev çalışmaz.
    """
    global _runner
    if _runner is None:
        _runner = _make()
    return await _runner.run([(g.rol, g.gorev) for g in gorevler])


if __name__ == "__main__":
    restore_token(os.environ)
    clean_env(os.environ)
    server.run("stdio")
