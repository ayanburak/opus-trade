"""
file: roles.py
importable: AgentRole, ROLES, ROLE_KEYS, toolset

Alt ajanların rolleri. Her rol Opus'a görünen anahtarı, Türkçe adı ve
açıklaması, modeli, effort'u, sistem metni, süre sınırı ve adım sınırıyla
tanımlanır. Opus görevin hangi role gideceğini kendisi seçer: kapsamlı
analiz Opus'a, orta görev Sonnet'e, küçük görev Haiku'ya. toolset() alt
ajanların araç takımını kurar: web araması, web sayfası okuma ve salt
okunur piyasa araçları.

    role = ROLES["haiku"]
    ts = toolset({"OPUS_MARKET_DB": "...", "OPUS_CORE_DB": "...", "OPUS_ACCOUNT": "1", "OPUS_PROFILE": "4"})

description: Alt ajan rolleri ve araç takımları.
"""

# NOTE: Alt ajanın takımında ekip aracı yoktur, bir alt ajan başka bir alt
# ajan başlatamaz. Hesabı değiştiren hiçbir araç da yoktur. Dosya, terminal
# ve kod çalıştırma araçları verilmez, talimat/ilkeler.md.
#
# NOTE: Haiku'nun sistem metni eski araştırmacının metnidir. Talimattaki
# deney ve sanal para yasağı bu metinler için de geçerlidir.
#
# XXX: Anahtarlar, adlar ve sistem metinleri Opus'a ya da alt ajana görünür,
# Türkçe kalır.

from dataclasses import dataclass

from ..llm.toolset import McpServer, Toolset
from ..tools import MARKET_MODULE

_BASE = ("Bir kripto ve emtia yatırımcısının ekibindesin. Yatırımcı sana bir görev verir, sen görevi araçlarınla "
         "yaparsın ve sonucu ona yazarsın. Hesaba erişimin yoktur, yalnızca araştırır ve analiz edersin. Web'de "
         "güncel ve güvenilir kaynaklara öncelik ver, bulduğun her bilginin tarihini ve kaynağını yaz. Piyasa "
         "araçlarıyla Binance'in gerçek ve canlı verisine bakabilirsin. Cevabını Türkçe yaz.")


@dataclass(frozen=True)
class AgentRole:
    """Bir alt ajan rolü.

    Args:
        key: Opus'un görevde yazdığı rol, opus, sonnet ya da haiku.
        name: Talimatta görünen kısa ad.
        description: Rolün hangi görevler için olduğu, talimatta görünür.
        model: Model takma adı.
        effort: low, medium ya da high.
        system: Alt ajanın sistem metni.
        timeout_s: Bir görevin en uzun süresi.
        max_turns: Bir görevin araç adımı sınırı.
    """

    key: str
    name: str
    description: str
    model: str
    effort: str
    system: str
    timeout_s: int
    max_turns: int


ROLES = {r.key: r for r in (
    AgentRole("opus", "kapsamlı analiz", "Birden çok kaynağı ve veriyi birleştirip yorum gerektiren geniş analiz.",
              "opus", "high",
              _BASE + " Görev kapsamlı bir analizdir. Veriyi ve haberleri birlikte değerlendir, karşıt görüşleri de "
                      "yaz, vardığın sonucu gerekçesiyle ver.", 600, 30),
    AgentRole("sonnet", "orta görev", "Birkaç kaynağı okuyup özetleme ya da belli bir soruyu veriyle cevaplama.",
              "sonnet", "medium",
              _BASE + " Görev orta büyüklüktedir. Soruyu doğrudan cevapla, kısa ve yoğun yaz.", 300, 20),
    AgentRole("haiku", "küçük görev", "Tek bir haberi, takvimi ya da rakamı bulup getirme.",
              "haiku", "low",
              _BASE + " Görev küçüktür. Bulduklarını yorum katmadan, kısa ve yoğun bir özetle aktar.", 180, 12),
)}

ROLE_KEYS = tuple(ROLES)

_BUILTIN = ("WebSearch", "WebFetch")


def toolset(market_env: dict[str, str]) -> Toolset:
    """Alt ajanların araç takımını döndürür.

    Args:
        market_env: Piyasa araçları sunucusunun ortamı.
    """
    return Toolset(builtin=_BUILTIN, servers={"piyasa": McpServer(MARKET_MODULE, dict(market_env))},
                   allowed=(*_BUILTIN, "mcp__piyasa"))
