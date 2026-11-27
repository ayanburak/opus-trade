"""
file: structure.py
importable: DIRECTIONS, REGIMES, REGIME_HINTS, FIT, LEVELS, StructureReview, StructureStep

Yapı okuması, bir piyasa okuma yöntemidir. İki numaralı adım taşır: yapı,
yani her paritede üst ve alt aralığın yönü ve son iki tepe ile dibi, ve
rejim, yani piyasanın beş rejimden hangisinde olduğu ve stratejiye uygun
olup olmadığı. Girişin yönünü, seviyesini, SL'sini ve aynı yönde eklemeyi
de denetler. Arayüzlerin rozeti, beklenen fiyatın alarmı ve deftere
taşınan tepe ve dipler bu adımdan gelir. REGIMES rejimlerin tek kaynağıdır,
talimat, profil ayarı ve arayüzün etiketi buradan okunur.

    StructureStep(("4h", "1h"), ("duzenli_trend",))

description: Yapı ve rejim adımı.
"""

# NOTE: Yapı alanında aralıklar ust ve alt diye adlanır, hangi aralık olduğu
# açıklamada yazar. Profil aralıkları değiştirse de alan adları aynı kalır,
# denetimler ve arayüz ust ile alt'ı okur.
#
# NOTE: Aynı yönde ekleme kuralı bu adıma aittir, Burak'ın 24.11 kararı.
# Yapısı olmayan türde kârdaki pozisyona eklemek serbesttir, risk beyanı yine
# gerçek riskle bağlar.
#
# NOTE: Girişin üst aralığın yönünde olma kuralı counter_regimes'teki
# rejimlerde uygulanmaz. Opus Multi'de yatay, aşırı uzama ve dönüş
# rejimlerinin oyun planı trende ters girebilir, yön o planın işidir. llm
# türünde liste boştur, talimatın cümlesi ve davranış eskisiyle aynı kalır,
# altın kopya testi bunu korur.
#
# XXX: Alan adları ve seçenekler Opus'a görünür, Türkçe kalır.

from dataclasses import dataclass, field

from ....domain.models import Side
from ....domain.trading import PlaceOrder
from ...base import Context
from .. import notebook as nbk
from ..cards import INTERVALS
from ..checks import Review, cancelled_by, entry_price, fail, worst_stop
from ..notebook import Notebook
from ..texts import INTERVAL_NAMES, price
from .base import DecisionStep

DIRECTIONS = ("yukselen", "alcalan", "yatay", "belirsiz")

# Rejimin Opus'a görünen değeri ve arayüzdeki etiketi.
REGIMES = {"duzenli_trend": "düzenli trend", "yatay": "yatay", "asiri_uzama": "aşırı uzama", "donus": "dönüş",
           "belirsiz": "belirsiz"}

# Rozetin üzerine gelince görünen kısa açıklama. Tanımın tamamı talimattadır.
REGIME_HINTS = {"duzenli_trend": "Tepe ve dipler aynı yönde ilerliyor, alt aralık bu yönde yapı kuruyor.",
                "yatay": "Tepe ve dipler bir bant içinde.",
                "asiri_uzama": "Fiyat kısa sürede olağanın çok ötesine gitti.",
                "donus": "Trendin son yapısı bozuldu, yeni yön yerleşmedi.",
                "belirsiz": "Veri bir rejimi açıkça göstermiyor."}

FIT = ("evet", "hayir")

LEVELS = ("ust", "alt")

_DIR_NAMES = {"yukselen": "yükselen", "alcalan": "alçalan", "yatay": "yatay", "belirsiz": "belirsiz"}

_REASON = {"type": "string", "minLength": 1}


@dataclass
class StructureReview:
    """Yapı adımının denetim sonucu, Review.parts["structure"] altında durur.

    Args:
        fit: Parite başına rejimin uygun olup olmadığı.
        aligned: Parite başına uyumun yönü, long, short ya da None.
        trend: Parite başına üst aralığın yönünün izin verdiği giriş yönü, belirsizse None.
        expectations: Rejimi uygun olmayan paritelerde beklenen fiyat, sembol ve fiyat.
        wait_until: Beklenen saatlerin en erkeni.
    """

    fit: dict[str, bool] = field(default_factory=dict)
    aligned: dict[str, str | None] = field(default_factory=dict)
    trend: dict[str, str | None] = field(default_factory=dict)
    expectations: list[tuple[str, float]] = field(default_factory=list)
    wait_until: int | None = None


def _pair_list(xs: list[float]) -> str:
    return " ve ".join(price(x) for x in xs)


def _contradiction(level: dict) -> str | None:
    tops, lows = level["tepeler"], level["dipler"]
    up = tops[1] > tops[0] and lows[1] > lows[0]
    down = tops[1] < tops[0] and lows[1] < lows[0]
    d = level["yon"]
    if d == "yukselen" and not up:
        bad = f"tepeler {_pair_list(tops)}" if tops[1] <= tops[0] else f"dipler {_pair_list(lows)}"
        return f"yönü yükselen seçtin ama yazdığın {bad}, yani yükselmiyor."
    if d == "alcalan" and not down:
        bad = f"tepeler {_pair_list(tops)}" if tops[1] >= tops[0] else f"dipler {_pair_list(lows)}"
        return f"yönü alçalan seçtin ama yazdığın {bad}, yani alçalmıyor."
    return None


# Üst aralığın yönü belliyse ve alt aralıkta o yönde ilk yapı oluştuysa uyum
# vardır: düşüşte alçalan tepe, yükselişte yükselen dip, talimat/tarz.md
# Bölüm 2.
def _alignment(top: dict, low: dict) -> str | None:
    if top["yon"] == "alcalan" and low["tepeler"][1] < low["tepeler"][0]:
        return "short"
    if top["yon"] == "yukselen" and low["dipler"][1] > low["dipler"][0]:
        return "long"
    return None


class StructureStep(DecisionStep):
    """Yapı ve rejim adımı.

    Args:
        structure: Üst ve alt aralık, cards.INTERVALS'tan.
        fit_regimes: Stratejinin çalıştığı rejimler, REGIMES anahtarları.
        counter_regimes: Girişin üst aralığın yönünde olma kuralının uygulanmadığı rejimler.

    Raises:
        ValueError: Üst aralık alt aralıktan uzun değilse.
    """

    key = "structure"
    size = 2

    def __init__(self, structure: tuple[str, str], fit_regimes: tuple[str, ...],
                 counter_regimes: tuple[str, ...] = ()) -> None:
        super().__init__()
        if INTERVALS.index(structure[0]) <= INTERVALS.index(structure[1]):
            raise ValueError("Yapının üst aralığı alt aralığından uzun olmalı.")
        self.structure = tuple(structure)
        self.fit_regimes = tuple(fit_regimes)
        self.counter_regimes = tuple(counter_regimes)

    def _part(self, review: Review) -> StructureReview:
        return review.parts.setdefault(self.key, StructureReview())

    # --- Talimat ---

    def texts(self) -> list[str]:
        top, low = INTERVAL_NAMES[self.structure[0]], INTERVAL_NAMES[self.structure[1]]
        fit = ", ".join(REGIMES[x] for x in self.fit_regimes)
        return [f"Yapıyı okumak, pariteler alanında yapi. Yön tahminle değil tepe ve dip sırasıyla okunsun diye var. Her "
                f"parite için {top} ve {low} grafikte yönü ve son iki tepe ile son iki dibi, eskiden yeniye yazarsın. "
                f"Parite kartında sistemin bulduğu son tepe ve dipler hazır gelir. Hangisinin önemli olduğu, bir "
                f"hareketin geri çekilme mi dönüş mü olduğu senin yorumundur. Yükselen dersen iki tepe de iki dip de "
                f"yükselir, alçalan dersen ikisi de alçalır. Yatay ve belirsiz senin okumandır. Uyumu sistem senin "
                f"rakamlarından çıkarır: {top} yön belliyse ve {low} grafikte o yönde ilk yapı oluştuysa, düşüşte ilk "
                f"alçalan tepe, yükselişte ilk yükselen dip, uyum vardır.",
                f"Rejim ve uygunluk, pariteler alanında rejim. Stratejinin çalışmadığı piyasada işlem açılmasın diye "
                f"var. Piyasanın rejimini okur, kanıtını rakamla yazar ve stratejine uygun olup olmadığını söylersin. "
                f"Stratejin şu rejimlerde çalışır: {fit}. Uyum yoksa düzenli trend seçemezsin. Uygun değilse neyi "
                f"beklediğini yazarsın, bir fiyat ya da bir saat. Beklediğin fiyata sistem alarm kurar, beklediğin "
                f"saat sonraki çağrının en geç zamanı olur. Paritede açık pozisyonun varsa ve yalnızca onu "
                f"yönetiyorsan beklediğin bir şey olmayabilir, o zaman yok seçersin. Uygun olmayan paritede giriş "
                f"yapamazsın."]

    def entry_rules(self) -> str:
        top, low = INTERVAL_NAMES[self.structure[0]], INTERVAL_NAMES[self.structure[1]]
        return (f"Sistem girişleri ayrıca şöyle denetler. Short girişin {low} grafikte yazdığın son tepenin altında "
                f"olur, SL'n o tepenin üstünde. Long girişin yazdığın son dibin üstünde olur, SL'n o dibin altında. Bir "
                f"önceki tepe ya da dip stopunun yeridir, girişinin değil. {self._direction_rule(top)} Aynı yönde açık "
                f"pozisyonun stopu girişe taşınmadıysa ya da aynı yönde bekleyen bir giriş emrin varsa yeni giriş "
                f"reddedilir.")

    # Yön kuralının talimattaki cümlesi. counter_regimes boşsa llm türünün
    # cümlesi harfi harfine aynı kalır.
    def _direction_rule(self, top: str) -> str:
        if not self.counter_regimes:
            return f"{top} yönü yükselen ya da alçalan yazdıysan giriş o yönde olur."
        names = [REGIMES[x] for x in self.counter_regimes]
        free = names[0] if len(names) == 1 else f"{', '.join(names[:-1])} ve {names[-1]}"
        return (f"Rejimi düzenli trend okuduysan ve {top} yönü yükselen ya da alçalan yazdıysan giriş o yönde olur. "
                f"{free[0].upper()}{free[1:]} rejimlerinde girişin yönü o rejimin oyun planındadır.")

    def sections(self) -> list[str]:
        top, low = INTERVAL_NAMES[self.structure[0]], INTERVAL_NAMES[self.structure[1]]
        return [f"""### Rejimler

- Düzenli trend: {top} grafikte tepe ve dipler aynı yönde ilerliyor, {low} grafik bu yönde yapı \
kuruyor, hareketler olağan oynaklık içinde.
- Yatay: Tepe ve dipler bir bant içinde, yükselen ya da alçalan bir sıra yok.
- Aşırı uzama: Fiyat kısa sürede olağanın çok ötesine gitmiş: {top} ATR'nin birkaç katı hareket, uç RSI \
değerleri, büyük likidasyon dalgaları. Ardından çoğu zaman sert ve iki yönlü hareket gelir.
- Dönüş: Trendin son yapısı bozulmuş, ters yönde ilk tepe ve dipler oluşuyor, yeni yön henüz yerleşmemiş.
- Belirsiz: Veri bu dört durumdan birini açıkça göstermiyor.

Rejimlerin rakam eşiği yoktur, rakamları kanıt alanında sen yazarsın."""]

    def notebook_note(self) -> tuple[str, str]:
        return (f"Tepe ve dip rakamlarını sistem {self.first}. adımda yazdıklarından deftere kendisi taşır.",
                "Tepe ve dip rakamlarını sistem pariteler alanından taşır.")

    def alarm_rule(self) -> str:
        return ("Rejimi uygun bulmadığın bir paritede beklediğin fiyata sistemin kurduğu alarm da listede görünür, yeni "
                "bir beklenen fiyat yazınca eskisinin yerini alır.")

    # --- Şema ---

    def fields(self, symbols: list[str]) -> dict[str, dict]:
        def level(interval: str) -> dict:
            two = {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2}
            return {"type": "object", "additionalProperties": False, "required": ["yon", "tepeler", "dipler"],
                    "description": f"{INTERVAL_NAMES[interval]} grafik.",
                    "properties": {"yon": {"type": "string", "enum": list(DIRECTIONS)},
                                   "tepeler": {**two, "description": "Son iki tepe, eskiden yeniye."},
                                   "dipler": {**two, "description": "Son iki dip, eskiden yeniye."}}}

        waiting = {"type": "object", "additionalProperties": False, "required": ["tur", "fiyat", "saat"],
                   "description": "Uygun değilse neyi beklediğin. Fiyat beklersen sistem oraya alarm kurar, saat "
                                  "beklersen o saatte çağrılırsın. Uygunsa tur yok olur. Uygun değilken yok yalnızca "
                                  "paritede açık pozisyonun varsa seçilebilir.",
                   "properties": {"tur": {"type": "string", "enum": ["fiyat", "saat", "yok"]},
                                  "fiyat": {"type": "number", "description": "tur fiyat ise beklenen fiyat, değilse 0."},
                                  "saat": {"type": "string", "description": "tur saat ise 22.11.2026 18:00 biçiminde, "
                                                                            "değilse boş."}}}
        pair = {
            "type": "object", "additionalProperties": False, "required": ["yapi", "rejim"],
            "properties": {
                "yapi": {"type": "object", "additionalProperties": False, "required": list(LEVELS),
                         "description": f"Adım {self.first}. Yönü kendi yazdığın tepe ve diplerle çelişemez.",
                         "properties": {"ust": level(self.structure[0]), "alt": level(self.structure[1])}},
                "rejim": {"type": "object", "additionalProperties": False,
                          "required": ["rejim", "kanit", "uygun", "bekledigi"],
                          "description": f"Adım {self.first + 1}. Piyasanın rejimi ve stratejine uygun olup olmadığı.",
                          "properties": {"rejim": {"type": "string", "enum": list(REGIMES)},
                                         "kanit": {**_REASON, "description": "Rakamlarla: hareketin büyüklüğü, RSI, "
                                                                             "ATR'deki değişim, likidasyon dalgası."},
                                         "uygun": {"type": "string", "enum": list(FIT)},
                                         "bekledigi": waiting}},
            },
        }
        return {"pariteler": {"type": "object", "additionalProperties": False, "required": list(symbols),
                              "properties": {s: pair for s in symbols}}}

    # --- Denetim ---

    def validate(self, k: dict, ctx: Context, old: Notebook, review: Review) -> None:
        out = self._part(review)
        shape, regime = f"adım {self.first}", f"adım {self.first + 1}"
        for sym, pair in k["pariteler"].items():
            levels = pair["yapi"]
            for key, interval in zip(LEVELS, self.structure):
                if why := _contradiction(levels[key]):
                    review.errors.append(fail(f"{shape}, {sym} {INTERVAL_NAMES[interval]} yapı", why))
            aligned = _alignment(levels["ust"], levels["alt"])
            out.aligned[sym] = aligned
            top = levels["ust"]["yon"]
            out.trend[sym] = {"yukselen": "long", "alcalan": "short"}.get(top)

            r = pair["rejim"]
            kind, fit, wait = r["rejim"], r["uygun"] == "evet", r["bekledigi"]
            out.fit[sym] = fit
            if kind == "duzenli_trend" and aligned is None:
                low = levels["alt"]
                review.errors.append(fail(
                    f"{regime}, {sym} rejim",
                    f"düzenli trend seçtin ama {self.first}. adımda uyum yok: {INTERVAL_NAMES[self.structure[0]]} yön "
                    f"{_DIR_NAMES[top]}, {INTERVAL_NAMES[self.structure[1]]} son tepeler {_pair_list(low['tepeler'])}, "
                    f"son dipler {_pair_list(low['dipler'])}. Düzenli trend için üst yön belli olmalı ve alt aralıkta "
                    f"o yönde ilk yapı oluşmalı."))
            if fit and kind not in self.fit_regimes:
                names = ", ".join(REGIMES[x] for x in self.fit_regimes)
                review.errors.append(fail(f"{regime}, {sym} rejim", f"uygun dedin ama {REGIMES[kind]} stratejinin "
                                                                    f"çalıştığı rejimlerden değil. Uygun rejimler: "
                                                                    f"{names}."))
            if fit:
                continue
            # Paritede açık pozisyon varken Opus yalnızca onu yönetiyor olabilir,
            # beklediği bir şey yoksa uydurmak zorunda kalmaz. Giriş yasağı sürer.
            holding = any(p.symbol == sym for p in ctx.state.positions)
            if wait["tur"] == "yok" and holding:
                continue
            if wait["tur"] == "fiyat" and wait["fiyat"] > 0:
                out.expectations.append((sym, float(wait["fiyat"])))
            elif wait["tur"] == "saat":
                try:
                    t = nbk.parse_until(wait["saat"])
                except ValueError as e:
                    review.errors.append(fail(f"{regime}, {sym} bekledigi", str(e)))
                    continue
                if t <= ctx.now:
                    review.errors.append(fail(f"{regime}, {sym} bekledigi", f"beklediğin saat {wait['saat']} "
                                                                            f"geçmişte."))
                else:
                    out.wait_until = t if out.wait_until is None else min(out.wait_until, t)
            else:
                review.errors.append(fail(f"{regime}, {sym} bekledigi", "rejimi uygun bulmadın ama neyi beklediğini "
                                                                        "yazmadın. Bir fiyat ya da bir saat yaz. "
                                                                        "Paritede açık pozisyonun yoksa yok seçilemez."))

    def entry_problem(self, a: dict, cmd: PlaceOrder, k: dict, review: Review, ctx: Context, leverage: int,
                      busy: set[tuple[str, str]], at: int) -> str | None:
        part = self._part(review)
        sym, side = cmd.symbol, cmd.side
        key = (sym, str(side))
        entry = entry_price(cmd, ctx)
        if not part.fit.get(sym, False):
            return fail(f"adım {self.first + 1}, {sym} rejim", "bu çağrıda rejimi stratejine uygun bulmadın, giriş "
                                                              "yapamazsın.")
        trend = part.trend.get(sym)
        regime = k["pariteler"][sym]["rejim"]["rejim"]
        if trend is not None and trend != str(side) and regime not in self.counter_regimes:
            return fail(f"adım {at}, {sym} yön", f"üst aralığın yönünü {'yükselen' if trend == 'long' else 'alçalan'} "
                                                 f"yazdın, {side} giriş bununla çelişiyor.")

        low = k["pariteler"][sym]["yapi"]["alt"]
        sl = a.get("sl")
        if side is Side.SHORT:
            top = low["tepeler"][-1]
            if entry >= top:
                return fail(f"adım {at}, giriş seviyesi", f"short giriş {price(entry)}, yazdığın son alt aralık "
                                                          f"tepesi {price(top)} ya da üstünde. Giriş o tepenin altında "
                                                          f"olur. Önceki tepe stopunun yeridir, girişin değil.")
            if sl is not None and sl <= top:
                return fail(f"adım {at}, giriş seviyesi", f"SL {price(sl)}, son alt aralık tepesi {price(top)} ya da "
                                                          f"altında. Stop o tepenin ötesinde durur.")
        else:
            bottom = low["dipler"][-1]
            if entry <= bottom:
                return fail(f"adım {at}, giriş seviyesi", f"long giriş {price(entry)}, yazdığın son alt aralık dibi "
                                                          f"{price(bottom)} ya da altında. Giriş o dibin üstünde olur. "
                                                          f"Önceki dip stopunun yeridir, girişin değil.")
            if sl is not None and sl >= bottom:
                return fail(f"adım {at}, giriş seviyesi", f"SL {price(sl)}, son alt aralık dibi {price(bottom)} ya da "
                                                          f"üstünde. Stop o dibin ötesinde durur.")

        # Aynı yönde ekleme: açık pozisyonun stopu girişe taşınmadıysa, aynı yönde
        # bekleyen bir giriş varsa ya da bu kararda aynı yönde bir giriş zaten
        # verildiyse reddedilir. Bu kararda iptal edilen bekleyen giriş sayılmaz.
        p = ctx.state.position(sym, side)
        if p is not None:
            stop = worst_stop(ctx, sym, side)
            safe = stop is not None and (stop >= p.entry_price if side is Side.LONG else stop <= p.entry_price)
            if not safe:
                return fail(f"adım {at}, aynı yönde ekleme", f"açık {sym} {side} pozisyonunun stopu girişe, "
                                                             f"{price(p.entry_price)}, taşınmadı. Önce stopu girişe "
                                                             f"taşı.")
        pending = [o for o in ctx.state.orders if not o.reduce and o.symbol == sym and o.side is side]
        if (pending and key not in cancelled_by(k, review)) or key in busy:
            return fail(f"adım {at}, aynı yönde ekleme", f"{sym} {side} yönünde zaten bekleyen bir giriş var. Aynı "
                                                         f"yönde ikinci giriş ilk pozisyonun stopu girişe taşınmadan "
                                                         f"verilmez.")
        return None

    # --- Kararın etkileri ---

    # Arayüzlerin ortak rozeti kayıttan okunur, etiketler burada bir kez kurulur.
    # waiting uygun olmayan rejimde beklenen fiyat ya da saattir, sayıyı arayüz
    # kendi biçimiyle yazar.
    def record(self, k: dict) -> dict:
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
                                      for key, interval in zip(LEVELS, self.structure)],
                        "regime": REGIMES[r["rejim"]], "fit": r["uygun"] == "evet", "waiting": waiting,
                        "hint": REGIME_HINTS[r["rejim"]]}
        return {"badge": out}

    def checks(self, review: Review) -> dict:
        part = self._part(review)
        return {"expectations": [[s, p] for s, p in part.expectations], "wait_until": part.wait_until}

    # Opus'un yazdığı tepe ve dipler deftere aralığın adıyla taşınır.
    def swings(self, k: dict) -> dict:
        return {sym: {interval: {"tepeler": pair["yapi"][key]["tepeler"], "dipler": pair["yapi"][key]["dipler"]}
                      for key, interval in zip(LEVELS, self.structure)}
                for sym, pair in k["pariteler"].items()}

    # Bir paritenin beklenti alarmı yalnızca bu kararın beklediği fiyattır.
    # Rejim uygun okunduysa ya da Opus bir saat bekliyorsa eski alarm kalkar,
    # yoksa Opus artık beklemediği bir fiyatta yanıltıcı notla çağrılırdı.
    def alarms(self, k: dict, review: Review) -> list[dict]:
        part = self._part(review)
        out = []
        expected = {sym for sym, _ in part.expectations}
        for sym, level in part.expectations:
            out.append({"op": "expect", "symbol": sym, "price": level, "label": f"beklenti {sym}",
                        "note": "Rejimi uygun bulmadığın çağrıda beklediğin fiyat."})
        for sym in k["pariteler"]:
            if sym not in expected:
                out.append({"op": "unexpect", "symbol": sym, "label": f"beklenti {sym}"})
        return out

    def wait_until(self, review: Review) -> int | None:
        return self._part(review).wait_until
