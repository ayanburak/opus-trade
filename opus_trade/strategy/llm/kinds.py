"""
file: kinds.py
importable: STRUCTURED, STRUCTURED_TEAM, OPEN, OPEN_TEAM, COMMON_PARAMS, team_params, structured, free

LLM strateji türleri ve kayıtları. Her tür evrensel makineyi, LLMStrategy'yi,
kendi karar adımları, talimat sürümü ve hedef metniyle kurar. Ortak
parametreler tek yerde tanımlanır, her tür kendi parametrelerini ekler.

    llm       Opus, yapı okumalı: defter, yapı ve rejim, kapanan işlemler, giriş alanları.
    llm_open  Opus, serbest: defter, kapanan işlemler, giriş alanları. Yöntem Opus'undur.

structured() ve free() türleri kayıt defterini kullanmadan kurar, testler
ve yerel denemeler içindir.

description: LLM strateji türlerinin kayıtları.
"""

# NOTE: Yeni bir yöntem eklemek steps altında bir adım dosyası ve burada bir
# kayıttır. LLMStrategy ve diğer adımlar değişmez.
#
# NOTE: Ekip kotası her türde aynı altı parametredir, varsayılanları türün
# politikasından gelir. Kayıtlı eski bir profil sürümü bu parametreleri
# taşımaz, çalıştırıcı parametreleri doğrulamadan kurucuya verir. Bu yüzden
# kurucu eksik kotayı da türün politikasıyla doldurur, tasarim.md § 5.4.
#
# NOTE: Bir türün talimatı değişince yalnızca o türün sürümü artırılır. llm
# türünün anahtarı ilk kayıttan beri llm'dir, profillerin taşınması gerekmez.

from ...domain.fees import BINANCE_STANDARD, FeeSchedule
from ...registry import Param
from ..base import STRATEGIES, Deps
from .cards import INTERVALS
from .steps.base import Steps
from .steps.book import BookStep
from .steps.closed import ClosedStep
from .steps.entry import EntryStep
from .steps.structure import REGIMES, StructureStep
from ...team.policy import TeamPolicy
from ...team.roles import ROLE_KEYS, ROLES
from .strategy import LLMStrategy, Template

# --- Ortak ---

COMMON_PARAMS = (
    Param("style", "text", "Tarz metni", default="", help="Talimata Çalışma tarzın bölümü olarak eklenir."),
    Param("model", "choice", "Model", default="opus", choices=("opus", "sonnet", "haiku")),
    Param("effort", "choice", "Effort", default="high", choices=("low", "medium", "high")),
    Param("min_wait", "int", "En kısa bekleme, dakika", default=15, low=1, high=1440),
    Param("max_wait", "int", "En uzun bekleme, dakika", default=180, low=1, high=10080),
    Param("step_limit", "int", "Araç adımı sınırı", default=25, low=1, high=100),
    Param("timeout_s", "int", "Çağrı süresi sınırı, saniye", default=600, low=60, high=3600),
)

def team_params(policy: TeamPolicy) -> tuple[Param, ...]:
    """Ekip kotasının parametrelerini türün varsayılan politikasıyla döndürür."""
    calls = tuple(Param(f"team_{r}", "int", f"{ROLES[r].model.capitalize()} görevi, çağrı başına",
                        default=policy.per_call.get(r, 0), low=0, high=100,
                        help=f"Opus'un bir çağrıda {ROLES[r].model} rolüne verebileceği en fazla görev.")
                  for r in ROLE_KEYS)
    days = tuple(Param(f"daily_{r}", "int", f"{ROLES[r].model.capitalize()} görevi, günlük",
                       default=policy.daily.get(r, 0), low=0, high=2000,
                       help="Aboneliği korumak için bir günde en fazla görev. Gün Türkiye saatiyle başlar.")
                 for r in ROLE_KEYS)
    return calls + days


def _policy(default: TeamPolicy, given: dict) -> TeamPolicy:
    known = {f"{k}_{r}" for k in ("team", "daily") for r in ROLE_KEYS}
    if unknown := set(given) - known:
        raise TypeError(f"Bilinmeyen parametre: {', '.join(sorted(unknown))}.")
    return TeamPolicy({r: int(given.get(f"team_{r}", default.per_call.get(r, 0))) for r in ROLE_KEYS},
                      {r: int(given.get(f"daily_{r}", default.daily.get(r, 0))) for r in ROLE_KEYS})


# --- Yapı okumalı ---

STRUCTURED = Template(
    version=8,
    goal="Amacın özkaynağını kalıcı olarak büyütmek. Kazanç iyi kurulumlarda bilerek alınan riskten gelir, kayıp zayıf "
         "kurulumlarda alınan riskten. Yüzde 50'lik bir kaybı geri kazanmak için yüzde 100 kazanç gerekir. Bakiyenin "
         "sıfırlanması hesabın kapanması demektir.")

# Yapı okumalı türün ekibi yalnızca Haiku'dur, tasarim.md § 5.6.
STRUCTURED_TEAM = TeamPolicy({"haiku": 10}, {"haiku": 100})

_STRUCTURE_PARAMS = (
    Param("structure_high", "choice", "Yapının üst aralığı", default="4h", choices=INTERVALS,
          help="Ana yönün okunduğu grafik. Alt aralıktan uzun olmalı."),
    Param("structure_low", "choice", "Yapının alt aralığı", default="1h", choices=INTERVALS,
          help="Geri çekilmenin bittiğinin okunduğu grafik."),
    Param("fit_regimes", "multi", "Uygun rejimler", default=("duzenli_trend",), choices=tuple(REGIMES),
          labels=tuple(REGIMES.values()), help="Stratejinin işlem açabildiği rejimler."),
)


def structured(llm, catalog, work_dir, tool_env: dict, market_db, style: str, model: str, effort: str, min_wait: int,
               max_wait: int, step_limit: int, timeout_s: int, structure_high: str = "4h", structure_low: str = "1h",
               fit_regimes: tuple[str, ...] = ("duzenli_trend",), fees: FeeSchedule = BINANCE_STANDARD,
               **team: int) -> LLMStrategy:
    """Yapı okumalı türü kurar. Parametreler LLMStrategy'ninkilerle, türün yapı parametreleriyle ve ekip kotasının
    team_ ve daily_ parametreleriyle aynıdır. Verilmeyen kota türün varsayılanıdır.

    Raises:
        ValueError: Üst aralık alt aralıktan uzun değilse.
    """
    structure = (structure_high, structure_low)
    steps = Steps([BookStep(), StructureStep(structure, tuple(fit_regimes)), ClosedStep(), EntryStep(fees)])
    return LLMStrategy(llm, catalog, work_dir, tool_env, market_db, style, model, effort, min_wait, max_wait,
                       step_limit, timeout_s, STRUCTURED, steps, structure, _policy(STRUCTURED_TEAM, team), fees)


def _deps(deps: Deps) -> tuple:
    return deps.llm, deps.catalog, deps.paths["work_dir"], deps.paths["tool_env"], deps.paths["market_db"]


STRATEGIES.register(
    "llm", "Opus",
    "Kararı her uyanmada Opus'a sorar. Opus piyasayı araçlarla inceler, talimattaki kontrol adımları ve tarz "
    "metniyle karar verir.",
    COMMON_PARAMS + _STRUCTURE_PARAMS + team_params(STRUCTURED_TEAM),
    lambda deps, **p: structured(*_deps(deps), **p),
    LLMStrategy,
)


# --- Serbest ---

# Serbest türün hedefi tarz metnindedir. Kalıcı büyüme cümlesi bir süreli
# hedefle çelişirdi, tasarim.md § 4.5.
OPEN = Template(
    version=1,
    goal="Hedefin çalışma tarzın bölümünde yazar. Kazanç bilerek alınan riskten gelir. Bakiyenin sıfırlanması hesabın "
         "kapanması demektir.")

OPEN_TEAM = TeamPolicy({"opus": 3, "sonnet": 10, "haiku": 50}, {"opus": 15, "sonnet": 60, "haiku": 300})

# Serbest türde yapı aralığı yoktur. Parite kartlarındaki son tepe ve dipler
# sabit aralıklarla gelir.
_OPEN_CARDS = ("4h", "1h")


def free(llm, catalog, work_dir, tool_env: dict, market_db, style: str, model: str, effort: str, min_wait: int,
         max_wait: int, step_limit: int, timeout_s: int, fees: FeeSchedule = BINANCE_STANDARD,
         **team: int) -> LLMStrategy:
    """Serbest türü kurar. Parametreler ortak parametreler ve ekip kotasıdır, verilmeyen kota türün varsayılanıdır."""
    steps = Steps([BookStep(), ClosedStep(), EntryStep(fees)])
    return LLMStrategy(llm, catalog, work_dir, tool_env, market_db, style, model, effort, min_wait, max_wait,
                       step_limit, timeout_s, OPEN, steps, _OPEN_CARDS, _policy(OPEN_TEAM, team), fees)


STRATEGIES.register(
    "llm_open", "Opus, serbest",
    "Kararı her uyanmada Opus'a sorar. Sabit bir yöntem yoktur, yönü, yöntemi, zamanlamayı ve riski Opus seçer. "
    "Defter sınaması, kapanan işlemlerin sınıflandırılması ve girişlerin gerekçesi zorunludur. Opus işini alt "
    "ajanlara bölebilir.",
    COMMON_PARAMS + team_params(OPEN_TEAM),
    lambda deps, **p: free(*_deps(deps), **p),
    LLMStrategy,
)
