"""
file: strategy.py
importable: LLMStrategy

LLMStrategy kararı bir dil modeline, bugün Opus'a soran stratejidir. Her
uyanmada talimatı ve istemi kurar, LLMClient ile modeli çağırır, cevaptaki
Türkçe eylemleri komutlara ve alarm işlemlerine çevirir. Model çağrısı
başarısız olursa LLMError yükselir, yeniden deneme çalıştırıcının işidir.

description: Kararı Opus'a soran strateji türü.
"""

# NOTE: Hafızası Opus'un not defteridir. Opus defteri her çağrıda baştan yazar,
# çalıştırıcı onu saklayıp bir sonraki uyanmada geri verir.
#
# NOTE: Çeviride takılan bir eylem, örneğin tp ile tp_kademeleri birlikte
# verilmişse, kapıya hiç gitmez ve reddedilmiş sonuç olarak errors listesine
# girer. Düzeltme çağrısı onu da kapıdan dönen redlerle birlikte gösterir.
# Çevirinin bir notu varsa, örneğin miktar bakiyeye sığdırıldıysa, not kabul
# edilmiş bir sonuç olarak notes listesine girer.

from ...domain.catalog import Catalog
from ...domain.fees import FeeSchedule, BINANCE_STANDARD
from ...registry import Param
from ..base import STRATEGIES, ActionResult, Context, Decision, Deps, Strategy
from . import actions, prompt
from .cards import cards_text
from .client import LLMClient, LLMRequest


def _label(a: dict) -> str:
    ref = f" #{a['numara']}" if "numara" in a else ""
    sym = f" {a['sembol']}" if "sembol" in a else ""
    return f"{a.get('tip', '?')}{ref}{sym}"


class LLMStrategy(Strategy):
    """Kararı modele soran strateji.

    Args:
        llm: Model istemcisi.
        catalog: Parite kuralları, talimattaki tablo için.
        work_dir: claude komutunun çalışma klasörü.
        tool_env: Piyasa araçları sunucusunun ortam değişkenleri.
        market_db: Toplayıcının deposu, parite kartlarındaki likidasyonlar için.
        style: Tarz metni.
        model: Model takma adı.
        effort: low, medium ya da high.
        min_wait: En kısa bekleme, dakika.
        max_wait: En uzun bekleme, dakika.
        step_limit: Opus'a bildirilen araç adımı sınırı. Sert sınır bunun beş üstüdür.
        timeout_s: Bir çağrının en uzun süresi.
        fees: Komisyon oranları, talimattaki oranlar ve girişin maliyeti için.
    """

    accepts_messages = True
    corrects = True
    uses_model = True

    def __init__(self, llm: LLMClient, catalog: Catalog, work_dir, tool_env: dict, market_db, style: str,
                 model: str, effort: str, min_wait: int, max_wait: int, step_limit: int, timeout_s: int,
                 fees: FeeSchedule = BINANCE_STANDARD) -> None:
        self.llm = llm
        self.catalog = catalog
        self.work_dir = work_dir
        self.tool_env = tool_env
        self.market_db = market_db
        self.style = style
        self.model = model
        self.effort = effort
        self.min_wait = min_wait
        self.max_wait = max_wait
        self.step_limit = step_limit
        self.timeout_s = timeout_s
        self.fees = fees

    def system(self, ctx: Context) -> str:
        return prompt.system_prompt(ctx.start_balance, [self.catalog.get(s) for s in ctx.symbols], self.fees,
                                    self.style, self.min_wait, self.max_wait, self.step_limit)

    async def decide(self, ctx: Context) -> Decision:
        text = prompt.build_prompt(ctx, await cards_text(ctx.market, self.market_db, ctx.symbols, ctx.prices,
                                                         ctx.now))
        req = LLMRequest(system=self.system(ctx), prompt=text, schema=prompt.schema(list(ctx.symbols)),
                         model=self.model, effort=self.effort, max_turns=self.step_limit + 5,
                         work_dir=self.work_dir, tool_env=self.tool_env, timeout_s=self.timeout_s)
        res = await self.llm.call(req, ctx.on_tool)
        k = res.decision
        d = Decision(wait_s=max(self.min_wait, min(self.max_wait, int(k["sonraki_cagri_dk"]))) * 60,
                     memory={"notebook": k["not_defteri"]})
        for a in k["eylemler"]:
            try:
                t = actions.translate(a, ctx.state, ctx.prices, ctx.marks, self.catalog, self.fees)
            except ValueError as e:
                d.errors.append(ActionResult(_label(a), False, f"{_label(a)} reddedildi: {e}"))
                continue
            if t.note:
                d.notes.append(ActionResult(_label(a), True, f"{_label(a)}: {t.note}"))
            if t.kind == "command":
                d.commands += t.item
                d.labels += [_label(a)] * len(t.item)
                d.chained += [False] + [True] * (len(t.item) - 1)
            else:
                d.alarms.append({**t.item, "label": _label(a)})
        d.record = {"template_version": prompt.TEMPLATE_VERSION, "model": self.model, "effort": self.effort,
                    "prompt": text, "decision": k, "raw": res.raw, "tools": res.tools, "usage": res.usage,
                    "cost_usd": res.cost_usd, "duration_s": res.duration_s, "summary": k["ozet"],
                    "reply": k["yoneticiye_cevap"]}
        return d


def _build(deps: Deps, **p) -> LLMStrategy:
    return LLMStrategy(deps.llm, deps.catalog, deps.paths["work_dir"], deps.paths["tool_env"],
                       deps.paths["market_db"], **p)


STRATEGIES.register(
    "llm", "Opus",
    "Kararı her uyanmada Opus'a sorar. Opus piyasayı araçlarla inceler, talimattaki kurallar ve tarz metniyle karar verir.",
    (Param("style", "text", "Tarz metni", default="", help="Talimata Çalışma tarzın bölümü olarak eklenir."),
     Param("model", "choice", "Model", default="opus", choices=("opus", "sonnet", "haiku")),
     Param("effort", "choice", "Effort", default="high", choices=("low", "medium", "high")),
     Param("min_wait", "int", "En kısa bekleme, dakika", default=15, low=1, high=1440),
     Param("max_wait", "int", "En uzun bekleme, dakika", default=180, low=1, high=10080),
     Param("step_limit", "int", "Araç adımı sınırı", default=25, low=1, high=100),
     Param("timeout_s", "int", "Çağrı süresi sınırı, saniye", default=600, low=60, high=3600)),
    _build,
    LLMStrategy,
)
