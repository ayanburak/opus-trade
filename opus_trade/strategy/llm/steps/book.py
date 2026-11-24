"""
file: book.py
importable: ITEM_REVIEWS, BookStep

Defter sınaması, her strateji türünde ilk adımdır. Opus eski defterin
planını, yapı notunu ve gündemini, hesaptaki her bekleyen giriş emrini ve
her açık pozisyonu bugünkü veriyle sınar. Denetim listenin hesapla
tuttuğuna, bozuldu denen pozisyonun kapatıldığına ve planın kurallarına
bakar. Bozuldu denen bekleyen girişler iptal edilmek üzere sonuca girer.

description: Defter sınaması adımı.
"""

# NOTE: Not defteri bütünüyle evrenseldir, plan, yapi ve gundem alanlarının
# sınaması burada durur. Yapı adımı yalnızca deftere taşınan tepe ve dip
# rakamlarını ekler.
#
# XXX: Alan adları ve seçenekler Opus'a görünür, Türkçe kalır.

from ....domain.models import Side
from ....domain.trading import Ref
from ...base import Context
from .. import notebook as nbk
from ..checks import Review, fail
from ..notebook import Notebook
from .base import DecisionStep

# Bekleyen emir ve açık pozisyon için defter sınamasındaki seçenekler.
ITEM_REVIEWS = ("gecerli", "zayifladi", "bozuldu")

_TEXT = {"type": "string"}
_REASON = {"type": "string", "minLength": 1}


def _review(options: tuple[str, ...], text: str) -> dict:
    return {"type": "object", "additionalProperties": False, "required": ["durum", "gerekce"], "description": text,
            "properties": {"durum": {"type": "string", "enum": list(options)}, "gerekce": _REASON}}


class BookStep(DecisionStep):
    key = "book"

    def texts(self) -> list[str]:
        return ["Defteri sınamak, defter_sinamasi. Önceki çağrıda vardığın sonuçlar yeni veriyle sınanmadan sürmesin "
                "diye var. Eski defterin planını, yapısını ve gündemini bugünkü veriyle değerlendirirsin. Hesaptaki her "
                "bekleyen giriş emri ve her açık pozisyon için geçerli, zayıfladı ya da bozuldu yazarsın, gerekçesiyle. "
                "Bozuldu dediğin emri sistem iptal eder. Bozuldu dediğin pozisyonu aynı kararda pozisyon_kapat ile "
                "kapatırsın. Yöneticinin açtığı pozisyonu değerlendirirsin ama kapatmak zorunda değilsin."]

    def fields(self, symbols: list[str]) -> dict[str, dict]:
        side = {"type": "string", "enum": ["long", "short"]}
        state = {"type": "string", "enum": list(ITEM_REVIEWS)}
        order = {"type": "object", "additionalProperties": False,
                 "required": ["numara", "sembol", "yon", "durum", "gerekce"],
                 "properties": {"numara": {"type": "integer"}, "sembol": _TEXT, "yon": side, "durum": state,
                                "gerekce": _REASON}}
        position = {"type": "object", "additionalProperties": False, "required": ["sembol", "yon", "durum", "gerekce"],
                    "properties": {"sembol": _TEXT, "yon": side, "durum": state, "gerekce": _REASON}}
        return {"defter_sinamasi": {
            "type": "object", "additionalProperties": False,
            "description": f"Adım {self.first}. Eski defterin her bölümünü ve hesaptaki her bekleyen giriş emrini ve "
                           f"açık pozisyonu bugünkü veriyle sına. Hepsi listede olmalı, fazlası da eksiği de karar "
                           f"reddine yol açar.",
            "required": ["plan", "yapi", "gundem", "emirler", "pozisyonlar"],
            "properties": {
                "plan": _review(nbk.PLAN_REVIEWS, "Plan bugünkü veriyle geçerli mi, zayıfladı mı, beklediği kurulum "
                                                  "oluşup gerçekleşti mi, bozuldu mu. Süresi dolan planı burada "
                                                  "geçerli desen de yenisini yazarsın."),
                "yapi": _review(nbk.STRUCTURE_REVIEWS, "Defterdeki yapı aynı mı, güncellendi mi, bozuldu mu."),
                "gundem": _review(nbk.AGENDA_REVIEWS, "Yakın olaylar bugünkü kararı etkiliyor mu. Olay yoksa "
                                                      "gerekçe tek kelime olabilir."),
                "emirler": {"type": "array", "items": order,
                            "description": "Her bekleyen giriş emri için bir satır. Koruma emirleri yazılmaz. Bozuldu "
                                           "dediğin emri sistem iptal eder."},
                "pozisyonlar": {"type": "array", "items": position,
                                "description": "Her açık pozisyon için bir satır. Bozuldu dediğin pozisyonu aynı kararda "
                                               "pozisyon_kapat ile kapatırsın, yönetici açtıysa zorunlu değil."},
            },
        }}

    def validate(self, k: dict, ctx: Context, old: Notebook, review: Review) -> None:
        self._items(k, ctx, review)
        self._plan(k, ctx, old, review)

    def _items(self, k: dict, ctx: Context, out: Review) -> None:
        step = f"adım {self.first}"
        review = k["defter_sinamasi"]
        state = ctx.state
        entries = {(o.number, o.symbol, str(o.side)) for o in state.orders if not o.reduce}
        given = [(x["numara"], x["sembol"], x["yon"]) for x in review["emirler"]]
        for n, sym, side in sorted(entries - set(given)):
            out.errors.append(fail(f"{step}, emirler", f"#{n} {sym} {side} bekleyen giriş emri listede yok. Her "
                                                       f"bekleyen giriş emrini sına."))
        for n, sym, side in sorted(set(given) - entries):
            out.errors.append(fail(f"{step}, emirler", f"#{n} {sym} {side} diye bekleyen bir giriş emrin yok. "
                                                       f"Yalnızca hesap bölümündeki bekleyen giriş emirlerini yaz."))
        if len(given) != len(set(given)):
            out.errors.append(fail(f"{step}, emirler", "Aynı emri birden çok kez yazdın, her emir bir kez sınanır."))

        positions = {(p.symbol, str(p.side)) for p in state.positions}
        named = [(x["sembol"], x["yon"]) for x in review["pozisyonlar"]]
        for sym, side in sorted(positions - set(named)):
            out.errors.append(fail(f"{step}, pozisyonlar", f"{sym} {side} pozisyonu listede yok. Her açık pozisyonu "
                                                           f"sına."))
        for sym, side in sorted(set(named) - positions):
            out.errors.append(fail(f"{step}, pozisyonlar", f"Açık {sym} {side} pozisyonun yok."))
        if len(named) != len(set(named)):
            out.errors.append(fail(f"{step}, pozisyonlar", "Aynı pozisyonu birden çok kez yazdın."))

        for x in review["emirler"]:
            if x["durum"] == "bozuldu" and (x["numara"], x["sembol"], x["yon"]) in entries:
                out.cancels.append(Ref(x["numara"], x["sembol"], Side(x["yon"])))

        closes = {(a["sembol"], a["yon"]) for a in k["eylemler"]
                  if a.get("tip") == "pozisyon_kapat" and float(a.get("yuzde") or 100) >= 100}
        for x in review["pozisyonlar"]:
            p = state.position(x["sembol"], Side(x["yon"]))
            if x["durum"] != "bozuldu" or p is None or ctx.facts.position_origins.get(p.id) == "admin":
                continue
            if (x["sembol"], x["yon"]) not in closes:
                out.errors.append(fail(f"{step}, pozisyonlar", f"{x['sembol']} {x['yon']} pozisyonunun tezi bozuldu "
                                                               f"dedin ama aynı kararda pozisyon_kapat ile tamamını "
                                                               f"kapatmadın. Ya kapat ya da sınamanı düzelt."))

    def _plan(self, k: dict, ctx: Context, old: Notebook, out: Review) -> None:
        step = f"adım {self.first}, plan"
        new = k["not_defteri"]
        for p in nbk.problems(new):
            out.errors.append(fail("not defteri", p))
        choice = k["defter_sinamasi"]["plan"]["durum"]
        expired = nbk.plan_state(old, ctx.now) == nbk.EXPIRED
        if choice == "gecerli" and not expired:
            if (new["plan"]["metin"], new["plan"]["bozulma"]) != (old.plan.get("metin"), old.plan.get("bozulma")):
                out.notes.append("Planı geçerli dediğin için metni ve bozulma koşulu aynen taşındı, yeni yazdığın "
                                 "plandan yalnızca geçerlilik saati alındı.")
        elif choice in ("bozuldu", "gerceklesti") or expired:
            if old.plan is not None and (new["plan"]["metin"], new["plan"]["bozulma"]) == \
                    (old.plan.get("metin"), old.plan.get("bozulma")):
                why = "süresi doldu" if expired else f"{'gerçekleşti' if choice == 'gerceklesti' else 'bozuldu'} dedin"
                out.errors.append(fail(step, f"Planın {why}, aynı planı yeniden yazamazsın. Bugünkü veriyle yeni bir "
                                             f"plan yaz."))
        try:
            if nbk.parse_until(new["plan"]["gecerlilik"]) <= ctx.now:
                out.errors.append(fail(step, f"Yeni planın geçerlilik saati {new['plan']['gecerlilik']} geçmişte "
                                             f"kalıyor."))
        except ValueError:
            pass
