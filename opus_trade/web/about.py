"""
file: about.py
importable: about

about() Hakkında sayfasının ihtiyaç duyduğu bütün bilgiyi programın kendi
ayarlarından ve kodundan toplar: model ve çağrı ayarları, komisyon ve parite
kataloğu, her profilin ayarları, tam talimatı ve karar şeması, Opus'un
gördüğü piyasa araçlarının tanımları ve borsa seansları. Sayfaya hiçbir
rakam ya da metin elle yazılmaz, bir ayar değişince doküman da değişir.

description: Hakkında sayfasının verisini programın ayarlarından ve kodundan toplayan modül.
"""

# NOTE: Araç tanımları piyasa sunucusunun kendi list_tools çıktısıdır,
# yani Opus'un gördüğü ad, açıklama ve parametre şemasının birebir aynısıdır.
# tools.market burada yalnızca okunmak için içe aktarılır, MCP sunucusu
# çalıştırılmaz.
#
# NOTE: Talimat her profilin kendi dönem tarihleriyle kurulur. Dönem henüz
# başlamamışsa tarihler yerine o anki zaman kullanılır.

import time

from .. import settings
from ..market import hours as market_hours
from ..opus import agent
from ..tools import market as market_mcp

# Opus'a claude komutunun kendi araçları olarak verilenler. Açıklamalar
# talimattakilerle aynı anlamdadır, dokümanda gösterilmek için burada durur.
_BUILTIN = [
    {"name": "WebSearch", "description": "Web'de arama yapar."},
    {"name": "WebFetch", "description": "Bir web sayfasını okur."},
    {"name": "Agent", "description": "Bir araştırma görevini Haiku modeline devreder. Haiku yalnızca WebSearch ve "
                                     "WebFetch kullanabilir, bulduklarını özet olarak döndürür."},
]


async def about(periods: dict[str, tuple[int | None, int | None]]) -> dict:
    """Hakkında sayfasının verisini döndürür.

    Args:
        periods: Profil kimliğinden dönemin başlangıç ve bitiş anına sözlük.
            Dönem başlamadıysa ikisi de None olabilir.

    Returns:
        JSON'a çevrilebilir bir sözlük.
    """
    now = int(time.time())
    tools = await market_mcp.server.list_tools()
    profiles = []
    for p in settings.PROFILES.values():
        start, end = periods.get(p.id, (None, None))
        start = start or now
        end = end or start + p.period_s
        profiles.append({
            "id": p.id, "name": p.name, "short": p.short, "balance": p.balance, "symbols": list(p.symbols),
            "period_hours": p.period_s // 3600, "min_wait": p.min_wait, "max_wait": p.max_wait, "style": p.style,
            "start_time": start, "end_time": end,
            "system_prompt": agent.system_prompt(p, start, end), "schema": agent.schema(p),
        })
    return {
        "model": settings.MODEL, "effort": settings.EFFORT, "researcher_model": settings.RESEARCHER_MODEL,
        "tool_step_limit": settings.TOOL_STEP_LIMIT, "hard_step_limit": settings.HARD_STEP_LIMIT,
        "call_timeout_s": settings.CALL_TIMEOUT_S, "error_retry_min": settings.ERROR_RETRY_MIN,
        "past_decisions": settings.PAST_DECISIONS, "end_equity": settings.END_EQUITY,
        "fee_maker": settings.FEE_MAKER, "fee_taker": settings.FEE_TAKER, "timezone": settings.TIMEZONE_LABEL,
        "catalog": {s: {k: v for k, v in x.items() if k != "icon"} for s, x in settings.SYMBOLS.items()},
        "profiles": profiles,
        "tools": [{"name": t.name, "description": t.description, "parameters": t.input_schema} for t in tools],
        "builtin_tools": _BUILTIN,
        "markets": [{"name": m.name, "tz": m.tz, "sessions": m.sessions, "half_close": m.half_close}
                    for m in market_hours.MARKETS],
    }
