"""
file: toolset.py
importable: McpServer, Toolset

Bir model çağrısına verilen araç takımı. Toolset yerleşik araçları, MCP
sunucularını, izin listesini ve alt ajanları taşır. İstemci komutu bu
takımdan kurar, kendi sabit bir listesi yoktur. McpServer bir Python
modülünü stdio sunucusu olarak çalıştırır. Hangi yorumlayıcının
çalıştıracağını istemci bilir, takım yalnızca modülü ve ortamını söyler.

    Toolset(builtin=("WebSearch",), servers={"piyasa": McpServer("opus_trade.tools.market", env)},
            allowed=("WebSearch", "mcp__piyasa"))

description: Model çağrısının araç takımı.
"""

# NOTE: Takımı kim kurarsa çağrının güvenliğinden o sorumludur. Dosya,
# terminal ve kod çalıştırma araçları hiçbir takıma konmaz. builtin boşsa
# claude'un yerleşik araçlarının hepsi kapalıdır, varsayılanı değil.
#
# NOTE: allowed izin listesidir. Bir MCP sunucusunun bütün araçları
# mcp__ad biçimiyle tek satırda izinlenir. Listede olmayan araç çağrılırsa
# claude dontAsk modunda onu reddeder.

from dataclasses import dataclass, field


@dataclass(frozen=True)
class McpServer:
    """Python modülü olarak çalışan bir MCP sunucusu.

    Args:
        module: python -m ile çalıştırılan modül.
        env: Sunucunun ortam değişkenleri.
        model: Sunucu kendisi de model çağırıyorsa True. İstemci ona model komutunu OPUS_CLAUDE ile bildirir.
    """

    module: str
    env: dict[str, str] = field(default_factory=dict)
    model: bool = False


@dataclass(frozen=True)
class Toolset:
    """Bir çağrının araçları.

    Args:
        builtin: claude'un yerleşik araçlarından açılanlar, örneğin WebSearch.
        servers: Sunucunun modele görünen adından sunucuya.
        allowed: İzin listesi, sorulmadan çalışan araçlar.
        agents: claude'un Agent aracıyla çağrılan alt ajanlar, boşsa ajan yoktur.
    """

    builtin: tuple[str, ...] = ()
    servers: dict[str, McpServer] = field(default_factory=dict)
    allowed: tuple[str, ...] = ()
    agents: dict[str, dict] = field(default_factory=dict)
