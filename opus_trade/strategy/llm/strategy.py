"""
file: strategy.py
importable: LLMStrategy, REJECT_WAIT_MIN

LLMStrategy kararı bir dil modeline, bugün Opus'a soran stratejidir. Her
uyanmada talimatı ve istemi kurar, LLMClient ile modeli çağırır, kararın
kontrol adımlarını checks.py ile denetler. Kontrol hatası varsa kararı
reddeder. Geçerse Türkçe eylemleri komutlara ve alarm işlemlerine çevirir,
girişlerin notlarını komutlara bağlar, defteri eskisiyle birleştirir.
Model çağrısı başarısız olursa LLMError yükselir, yeniden deneme
çalıştırıcının işidir.

description: Kararı Opus'a soran strateji türü.
"""

# NOTE: Hafızası Opus'un not defteridir, notebook.py. Kontrol hatasıyla
# reddedilen kararda hafıza boş döner, çalıştırıcı eski defteri korur.
#
# NOTE: Çeviride ya da girişin kontrolünde takılan bir eylem kapıya hiç
# gitmez ve reddedilmiş sonuç olarak errors listesine girer. Düzeltme
# çağrısı onu da kapıdan dönen redlerle birlikte gösterir. Çevirinin bir notu
# varsa, örneğin miktar bakiyeye sığdırıldıysa, not kabul edilmiş bir sonuç
# olarak notes listesine girer.
#
# NOTE: Bozuldu denen bekleyen girişlerin iptali kontrolün çıktısıdır ve
# eylemlerden önce gönderilir. Opus aynı kararda o yönde yeni bir giriş
# verirse eski emir önce iptal olur.

from ...domain.catalog import Catalog
from ...domain.fees import FeeSchedule, BINANCE_STANDARD
from ...domain.trading import CancelOrder, PlaceOrder, SetLeverage
from ...registry import Param
from ..base import STRATEGIES, ActionResult, Context, Decision, Deps, Strategy
from . import actions, checks, prompt
from . import notebook as nbk
from . import schema as schema_
from .cards import INTERVALS, cards_text
from .client import LLMClient, LLMRequest

# Düzeltme hakkı biten bir retten sonra bir sonraki çağrıya kadar en az bu
# kadar dakika geçer, profilin en kısa beklemesi daha uzunsa o. Kısa bekleyen
# profilde art arda reddedilen çağrılar aboneliğin limitini hızla tüketirdi.
REJECT_WAIT_MIN = 15

_ENTRIES = ("piyasa_giris", "emir_ver")


def _label(a: dict) -> str:
    ref = f" #{a['numara']}" if "numara" in a else ""
    sym = f" {a['sembol']}" if "sembol" in a else ""
    return f"{a.get('tip', '?')}{ref}{sym}"


# Opus'un 2. adımda yazdığı tepe ve dipler deftere aralığın adıyla taşınır.
def _swings(k: dict, structure: tuple[str, str]) -> dict:
    return {sym: {interval: {"tepeler": pair["yapi"][key]["tepeler"], "dipler": pair["yapi"][key]["dipler"]}
                  for key, interval in zip(schema_.LEVELS, structure)}
            for sym, pair in k["pariteler"].items()}


# Arayüzlerin ortak rozeti kayıttan okunur, etiketler burada bir kez kurulur.
# waiting uygun olmayan rejimde beklenen fiyat ya da saattir, sayıyı arayüz
# kendi biçimiyle yazar.
def _badge(k: dict, structure: tuple[str, str]) -> dict:
    out = {}
    for sym, pair in k["pariteler"].items():
        r = pair["rejim"]
        wait = r["bekledigi"]
        waiting = None
        if r["uygun"] != "evet" and wait["tur"] == "fiyat":
            waiting = {"price": wait["fiyat"]}
        elif r["uygun"] != "evet" and wait["tur"] == "saat":
            waiting = {"time": wait["saat"]}
        out[sym] = {"structure": [{"interval": interval, "dir": pair["yapi"][key]["yon"]}
                                  for key, interval in zip(schema_.LEVELS, structure)],
                    "regime": schema_.REGIMES[r["rejim"]], "fit": r["uygun"] == "evet", "waiting": waiting,
                    "hint": schema_.REGIME_HINTS[r["rejim"]]}
    return out


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
        structure_high: Yapının üst aralığı, cards.INTERVALS'tan biri.
        structure_low: Yapının alt aralığı, üst aralıktan kısa olmalı.
        fit_regimes: Stratejinin çalıştığı rejimler, schema.REGIMES anahtarları.
        fees: Komisyon oranları, talimattaki oranlar ve girişin maliyeti için.

    Raises:
        ValueError: Üst aralık alt aralıktan uzun değilse.
    """

    accepts_messages = True
    corrects = True
    uses_model = True

    def __init__(self, llm: LLMClient, catalog: Catalog, work_dir, tool_env: dict, market_db, style: str,
                 model: str, effort: str, min_wait: int, max_wait: int, step_limit: int, timeout_s: int,
                 structure_high: str = "4h", structure_low: str = "1h",
                 fit_regimes: tuple[str, ...] = ("duzenli_trend",), fees: FeeSchedule = BINANCE_STANDARD) -> None:
        if INTERVALS.index(structure_high) <= INTERVALS.index(structure_low):
            raise ValueError("Yapının üst aralığı alt aralığından uzun olmalı.")
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
        self.structure = (structure_high, structure_low)
        self.fit_regimes = tuple(fit_regimes)
        self.fees = fees

    def system(self, ctx: Context) -> str:
        return prompt.system_prompt(ctx.start_balance, [self.catalog.get(s) for s in ctx.symbols], self.fees,
                                    self.style, self.min_wait, self.max_wait, self.step_limit, self.structure,
                                    self.fit_regimes, ctx.now)

    async def decide(self, ctx: Context) -> Decision:
        old = nbk.read(ctx.memory)
        cards = await cards_text(ctx.market, self.market_db, ctx.symbols, ctx.prices, ctx.now, self.structure)
        text = prompt.build_prompt(ctx, cards, ctx.memory.get("notebook_at"))
        req = LLMRequest(system=self.system(ctx), prompt=text, schema=schema_.schema(list(ctx.symbols), self.structure),
                         model=self.model, effort=self.effort, max_turns=self.step_limit + 5,
                         work_dir=self.work_dir, tool_env=self.tool_env, timeout_s=self.timeout_s)
        res = await self.llm.call(req, ctx.on_tool)
        k = res.decision
        review = checks.validate(k, ctx, self.structure, self.fit_regimes, old)
        suggestion = (k.get("yoneticiye_oneri") or "").strip() or None
        record = {"template_version": prompt.TEMPLATE_VERSION, "model": self.model, "effort": self.effort,
                  "prompt": text, "decision": k, "raw": res.raw, "tools": res.tools, "usage": res.usage,
                  "cost_usd": res.cost_usd, "duration_s": res.duration_s, "summary": k["ozet"],
                  "reply": k["yoneticiye_cevap"], "suggestion": suggestion, "badge": _badge(k, self.structure),
                  "checks": {"errors": review.errors, "notes": review.notes,
                             "cancels": [f"#{r.number} {r.symbol} {r.side}" for r in review.cancels],
                             "expectations": [[s, p] for s, p in review.expectations],
                             "wait_until": review.wait_until}}
        if review.errors:
            record["rejection"] = review.errors
            return Decision(rejection=review.errors, wait_s=max(self.min_wait, REJECT_WAIT_MIN) * 60, record=record,
                            suggestion=suggestion)

        wait = max(self.min_wait, min(self.max_wait, int(k["sonraki_cagri_dk"]))) * 60
        if review.wait_until is not None:
            wait = min(wait, max(60, review.wait_until - ctx.now))
        nb, _ = nbk.merge(old, k["not_defteri"], k["defter_sinamasi"]["plan"]["durum"], _swings(k, self.structure),
                          ctx.now)
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
        for sym, level in review.expectations:
            d.alarms.append({"op": "expect", "symbol": sym, "price": level, "label": f"beklenti {sym}",
                             "note": "Rejimi uygun bulmadığın çağrıda beklediğin fiyat."})
        return d

    @staticmethod
    def _add(d: Decision, cmds: list, label: str, note: dict | None) -> None:
        d.commands += cmds
        d.labels += [label] * len(cmds)
        d.chained += [False] + [True] * (len(cmds) - 1)
        d.annotations += [None] * (len(cmds) - 1) + [note]

    def _action(self, a: dict, k: dict, review: checks.Review, ctx: Context, d: Decision,
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
            if why := checks.entry_problem(a, entry, k, review, ctx, self.fees, lev, busy):
                d.errors.append(ActionResult(label, False, f"{label} reddedildi. {why}"))
                return
            busy.add((entry.symbol, str(entry.side)))
            note = _note(a)
        if t.note:
            d.notes.append(ActionResult(label, True, f"{label}: {t.note}"))
        self._add(d, t.item, label, note)


def _build(deps: Deps, **p) -> LLMStrategy:
    return LLMStrategy(deps.llm, deps.catalog, deps.paths["work_dir"], deps.paths["tool_env"],
                       deps.paths["market_db"], **p)


STRATEGIES.register(
    "llm", "Opus",
    "Kararı her uyanmada Opus'a sorar. Opus piyasayı araçlarla inceler, talimattaki kontrol adımları ve tarz "
    "metniyle karar verir.",
    (Param("style", "text", "Tarz metni", default="", help="Talimata Çalışma tarzın bölümü olarak eklenir."),
     Param("model", "choice", "Model", default="opus", choices=("opus", "sonnet", "haiku")),
     Param("effort", "choice", "Effort", default="high", choices=("low", "medium", "high")),
     Param("min_wait", "int", "En kısa bekleme, dakika", default=15, low=1, high=1440),
     Param("max_wait", "int", "En uzun bekleme, dakika", default=180, low=1, high=10080),
     Param("step_limit", "int", "Araç adımı sınırı", default=25, low=1, high=100),
     Param("timeout_s", "int", "Çağrı süresi sınırı, saniye", default=600, low=60, high=3600),
     Param("structure_high", "choice", "Yapının üst aralığı", default="4h", choices=INTERVALS,
           help="Ana yönün okunduğu grafik. Alt aralıktan uzun olmalı."),
     Param("structure_low", "choice", "Yapının alt aralığı", default="1h", choices=INTERVALS,
           help="Geri çekilmenin bittiğinin okunduğu grafik."),
     Param("fit_regimes", "multi", "Uygun rejimler", default=("duzenli_trend",), choices=tuple(schema_.REGIMES),
           labels=tuple(schema_.REGIMES.values()), help="Stratejinin işlem açabildiği rejimler.")),
    _build,
    LLMStrategy,
)
