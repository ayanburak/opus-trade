"""
file: toolkit.py
importable: BUILTIN_TOOLS, TeamGrant, toolset, team_deadline

LLM stratejisinin Opus'a verdiği araç takımı tek yerde kurulur. toolset()
piyasa araçları sunucusunun ortamını ve ekip hakkını alır ve web araması,
web sayfası okuma, piyasa araçları ve ekip aracından oluşan takımı
döndürür. Kontrol API'si de Opus'a verilen araçların listesini buradan okur.
team_deadline() bir çağrının süresinden alt görevlerin bitmesi gereken anı
hesaplar.

    grant = TeamGrant({"haiku": 10}, work_dir / "ekip.jsonl", team_deadline(600))
    req = LLMRequest(..., toolset=toolset({"OPUS_PROFILE": "4"}, grant))

description: LLM stratejisinin araç takımı.
"""

# NOTE: Dosya, terminal ve kod çalıştırma araçları güvenlik için bilerek
# verilmez. Piyasa araçları salt okunurdur. Alt ajanlar claude'un Agent
# aracıyla değil ekip sunucusuyla çalışır, kota kodda uygulanır ve her alt
# görevin tüketimi kaydedilir, tasarim.md § 5.
#
# NOTE: Süreler iç içedir: alt görev ekip hakkının son anından önce biter,
# claude'un araç zaman aşımı ondan sonra gelir, ana çağrının süresi en son
# biter. Aradaki pay Opus'un alt görevlerden sonra kararını yazmasına kalır.

import json
from dataclasses import dataclass
from pathlib import Path

from ...llm.toolset import McpServer, Toolset
from ...tools import MARKET_MODULE, TEAM_MODULE

# Opus'un piyasa araçları dışında kullanabildiği yerleşik araçlar.
BUILTIN_TOOLS = ("WebSearch", "WebFetch")

# Araç zaman aşımının alt görevlerin son anından sonraki payı, saniye.
_TOOL_SLACK_S = 30


@dataclass(frozen=True)
class TeamGrant:
    """Bir çağrının ekip hakkı.

    Args:
        quota: Rolden bu çağrıda kalan görev hakkına.
        log: Alt görev kayıtlarının dosyası.
        deadline_s: Ekip sunucusu başladıktan sonra alt görevlerin bitmesi gereken süre.
    """

    quota: dict[str, int]
    log: Path
    deadline_s: int

    def tool_timeout_ms(self) -> int:
        return (self.deadline_s + _TOOL_SLACK_S) * 1000


def team_deadline(timeout_s: int) -> int:
    """Çağrının süresinden alt görevlere kalan süreyi döndürür. Opus'a kararını yazmak için en az iki dakika kalır."""
    return max(60, timeout_s - max(120, timeout_s // 4))


def toolset(env: dict[str, str], grant: TeamGrant | None = None) -> Toolset:
    """Opus'un araç takımını döndürür.

    Args:
        env: Piyasa araçları sunucusunun ortamı: veritabanlarının yolu, hesap ve profil.
        grant: Ekip hakkı. None ise ekip aracı verilmez.
    """
    servers = {"piyasa": McpServer(MARKET_MODULE, dict(env))}
    allowed = [*BUILTIN_TOOLS, "mcp__piyasa"]
    if grant is not None:
        servers["ekip"] = McpServer(TEAM_MODULE, {**env, "OPUS_TEAM_QUOTA": json.dumps(grant.quota),
                                                  "OPUS_TEAM_LOG": str(grant.log),
                                                  "OPUS_TEAM_DEADLINE_S": str(grant.deadline_s)}, model=True)
        allowed.append("mcp__ekip")
    return Toolset(builtin=BUILTIN_TOOLS, servers=servers, allowed=tuple(allowed))
