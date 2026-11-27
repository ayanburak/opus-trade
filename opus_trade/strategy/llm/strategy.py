"""
file: strategy.py
importable: LLMStrategy, Template, REJECT_WAIT_MIN

LLMStrategy kararı bir dil modeline, bugün Opus'a soran stratejidir. Her
uyanmada talimatı ve istemi kurar, LLMClient ile modeli çağırır, kararı
strateji türünün karar adımlarıyla denetler. Kontrol hatası varsa kararı
reddeder. Geçerse Türkçe eylemleri komutlara ve alarm işlemlerine çevirir,
girişleri adımların giriş denetiminden geçirir, notlarını komutlara bağlar,
defteri eskisiyle birleştirir. Model çağrısı başarısız olursa LLMError
yükselir, yeniden deneme çalıştırıcının işidir. Strateji türleri ve
kayıtları kinds.py'dedir.

description: Kararı Opus'a soran stratejinin evrensel makinesi.
"""

# NOTE: Hafızası Opus'un not defteridir, notebook.py. Kontrol hatasıyla
# reddedilen kararda hafıza boş döner, çalıştırıcı eski defteri korur.
#
# NOTE: Çeviride ya da girişin denetiminde takılan bir eylem kapıya hiç
# gitmez ve reddedilmiş sonuç olarak errors listesine girer. Düzeltme
# çağrısı onu da kapıdan dönen redlerle birlikte gösterir. Çevirinin bir notu
# varsa, örneğin miktar bakiyeye sığdırıldıysa, not kabul edilmiş bir sonuç
# olarak notes listesine girer.
#
# NOTE: Bozuldu denen bekleyen girişlerin iptali denetimin çıktısıdır ve
# eylemlerden önce gönderilir. Opus aynı kararda o yönde yeni bir giriş
# verirse eski emir önce iptal olur.
#
# NOTE: Bu sınıf hiçbir piyasa okuma yöntemini bilmez. Rozet, beklenti
# alarmları, deftere taşınan tepe ve dipler, girişin notuna eklenen alanlar,
# örneğin oyun planı, ve bir sonraki çağrının en geç anı adımlardan gelir.

from dataclasses import dataclass

from ...domain.catalog import Catalog
from ...domain.fees import FeeSchedule, BINANCE_STANDARD
from ...domain.trading import CancelOrder, PlaceOrder, SetLeverage
from ...llm.base import LLMClient, LLMError, LLMRequest
from ...team import log as team_log
from ...team.policy import TeamPolicy
from ..base import ActionResult, Context, Decision, Strategy
from . import actions, prompt
from . import notebook as nbk
from . import schema as schema_
from .cards import cards_text
from .checks import Review
from .steps.base import Steps
from .toolkit import TeamGrant, team_deadline, toolset

# Düzeltme hakkı biten bir retten sonra bir sonraki çağrıya kadar en az bu
# kadar dakika geçer, profilin en kısa beklemesi daha uzunsa o. Kısa bekleyen
# profilde art arda reddedilen çağrılar aboneliğin limitini hızla tüketirdi.
REJECT_WAIT_MIN = 15

_ENTRIES = ("piyasa_giris", "emir_ver")


def _label(a: dict) -> str:
    ref = f" #{a['numara']}" if "numara" in a else ""
    sym = f" {a['sembol']}" if "sembol" in a else ""
    return f"{a.get('tip', '?')}{ref}{sym}"


@dataclass(frozen=True)
class Template:
    """Bir LLM strateji türünün talimata kattığı sabitler.

    Args:
        version: Talimatın sürümü, her çağrının kaydına yazılır.
        goal: Talimatın Hedefin bölümünün metni.
    """

    version: int
    goal: str


# Alt görevlerin araçları ana çağrının araç listesine alt işaretiyle eklenir,
# panel ve herkese açık sayfa onları alt ajan araçları gibi gösterir.
def _sub_tools(team: list[dict]) -> list[dict]:
    return [{**t, "sub": True} for rec in team for t in rec.get("tools", [])]


def _note(a: dict) -> dict:
    reason = {"karsi_kanit": a["karsi_kanit"], "risk_beyani": a["risk_beyani"]}
    if a.get("son_stoptan_farki"):
        reason["son_stoptan_farki"] = a["son_stoptan_farki"]
    return {"symbol": a["sembol"], "side": a["yon"], "reason": reason,
            "invalid_price": float(a["gecersizlik"]["fiyat"]), "invalid_text": a["gecersizlik"]["metin"]}


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
        template: Türün talimat sürümü ve hedef metni.
        steps: Türün karar adımları.
        cards: Parite kartlarındaki son tepe ve diplerin aralıkları, üstten alta.
        team: Ekip kotası. Çağrı kotası hep sıfırsa ekip aracı verilmez.
        fees: Komisyon oranları, talimattaki oranlar ve girişin maliyeti için.
    """

    accepts_messages = True
    corrects = True
    uses_model = True

    def __init__(self, llm: LLMClient, catalog: Catalog, work_dir, tool_env: dict, market_db, style: str,
                 model: str, effort: str, min_wait: int, max_wait: int, step_limit: int, timeout_s: int,
                 template: Template, steps: Steps, cards: tuple[str, ...] = ("4h", "1h"),
                 team: TeamPolicy = TeamPolicy(), fees: FeeSchedule = BINANCE_STANDARD) -> None:
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
        self.template = template
        self.steps = steps
        self.cards = tuple(cards)
        self.team = team
        self.fees = fees

    def system(self, ctx: Context) -> str:
        return prompt.system_prompt(ctx.start_balance, [self.catalog.get(s) for s in ctx.symbols], self.fees,
                                    self.style, self.min_wait, self.max_wait, self.step_limit, self.steps,
                                    self.template.goal, self.team.per_call, ctx.now)

    def schema(self, symbols: list[str]) -> dict:
        return schema_.schema(symbols, self.steps)

    async def decide(self, ctx: Context) -> Decision:
        old = nbk.read(ctx.memory)
        cards = await cards_text(ctx.market, self.market_db, ctx.symbols, ctx.prices, ctx.now, self.cards)
        text = prompt.build_prompt(ctx, cards, ctx.memory.get("notebook_at"))
        grant = self._grant(ctx)
        req = LLMRequest(system=self.system(ctx), prompt=text, schema=self.schema(list(ctx.symbols)),
                         model=self.model, effort=self.effort, max_turns=self.step_limit + 5,
                         work_dir=self.work_dir, toolset=toolset(self.tool_env, grant), timeout_s=self.timeout_s,
                         env={"MCP_TOOL_TIMEOUT": str(grant.tool_timeout_ms())} if grant else {})
        if grant:
            grant.log.unlink(missing_ok=True)
        try:
            res = await self.llm.call(req, ctx.on_tool)
        except LLMError as e:
            team = team_log.collect(grant.log) if grant else []
            if team:
                e.tools = e.tools + _sub_tools(team)
                e.record = {**e.record, "team": team}
            raise
        team = team_log.collect(grant.log) if grant else []
        k = res.decision
        review = self.steps.validate(k, ctx, old)
        suggestion = (k.get("yoneticiye_oneri") or "").strip() or None
        record = {"template_version": self.template.version, "model": self.model, "effort": self.effort,
                  "prompt": text, "decision": k, "raw": res.raw, "tools": res.tools + _sub_tools(team),
                  "usage": res.usage, "team": team,
                  "cost_usd": res.cost_usd, "duration_s": res.duration_s, "summary": k["ozet"],
                  "reply": k["yoneticiye_cevap"], "suggestion": suggestion, **self.steps.record(k),
                  "checks": {"errors": review.errors, "notes": review.notes,
                             "cancels": [f"#{r.number} {r.symbol} {r.side}" for r in review.cancels],
                             **self.steps.checks(review)}}
        if review.errors:
            record["rejection"] = review.errors
            return Decision(rejection=review.errors, wait_s=max(self.min_wait, REJECT_WAIT_MIN) * 60, record=record,
                            suggestion=suggestion)

        wait = max(self.min_wait, min(self.max_wait, int(k["sonraki_cagri_dk"]))) * 60
        if (until := self.steps.wait_until(review)) is not None:
            wait = min(wait, max(60, until - ctx.now))
        nb, _ = nbk.merge(old, k["not_defteri"], k["defter_sinamasi"]["plan"]["durum"], ctx.now, self.steps.swings(k))
        d = Decision(wait_s=wait, memory={"notebook": nb.to_memory(), "notebook_at": ctx.now}, record=record,
                     suggestion=suggestion,
                     reviews=[{"position_id": x["pozisyon_no"], "verdict": x["sinif"], "text": x["aciklama"]}
                              for x in k["kapanan_islemler"]])
        d.notes += [ActionResult("defter", True, n) for n in review.notes]
        for ref in review.cancels:
            self._add(d, [CancelOrder(ref)], f"defter sınaması #{ref.number} {ref.symbol}", None)
        busy: set[tuple[str, str]] = set()
        for a in k["eylemler"]:
            self._action(a, k, review, ctx, d, busy)
        d.alarms += self.steps.alarms(k, review)
        return d

    # Ekip hakkı yalnızca çağrı kotası olan türde verilir. Günlük sınır dolsa da
    # araç kalır, talimat onu anlatır, kalan hak sıfır olarak gider.
    def _grant(self, ctx: Context) -> TeamGrant | None:
        if not any(self.team.per_call.values()):
            return None
        return TeamGrant(self.team.remaining(ctx.facts.team_today), self.work_dir / "ekip.jsonl",
                         team_deadline(self.timeout_s))

    @staticmethod
    def _add(d: Decision, cmds: list, label: str, note: dict | None) -> None:
        d.commands += cmds
        d.labels += [label] * len(cmds)
        d.chained += [False] + [True] * (len(cmds) - 1)
        d.annotations += [None] * (len(cmds) - 1) + [note]

    def _action(self, a: dict, k: dict, review: Review, ctx: Context, d: Decision,
                busy: set[tuple[str, str]]) -> None:
        label = _label(a)
        try:
            t = actions.translate(a, ctx.state, ctx.prices, ctx.marks, self.catalog, self.fees)
        except ValueError as e:
            d.errors.append(ActionResult(label, False, f"{label} reddedildi: {e}"))
            return
        if t.kind != "command":
            d.alarms.append({**t.item, "label": label})
            return
        note = None
        if a["tip"] in _ENTRIES:
            entry = next(c for c in t.item if isinstance(c, PlaceOrder))
            lev = next((c.leverage for c in t.item if isinstance(c, SetLeverage)), ctx.state.leverage(entry.symbol))
            if why := self.steps.entry_problem(a, entry, k, review, ctx, lev, busy):
                d.errors.append(ActionResult(label, False, f"{label} reddedildi. {why}"))
                return
            busy.add((entry.symbol, str(entry.side)))
            note = _note(a)
            note["reason"].update(self.steps.entry_note(a, entry, k, review))
        if t.note:
            d.notes.append(ActionResult(label, True, f"{label}: {t.note}"))
        self._add(d, t.item, label, note)
