"""
file: checks.py
importable: RISK_SLACK, Review, validate, entry_problem, cancelled_by

Opus'un kararındaki atlanamayan kontrol adımlarının kod denetimleri,
talimat/sema.md. validate() kararın bütününe bakar: defter sınaması, yapı,
rejim ve kapanan işlemler. Bulduğu her hata bir kontrol hatasıdır ve bütün
kararı reddeder. Sonuç ayrıca bozuldu denen emirlerin iptalini, rejimi
uygun olmayan paritede beklenen fiyatı ve saati taşır. entry_problem() tek
bir girişe bakar: karşı kanıt, risk beyanı, geçersizlik, son stoptan farkı,
giriş seviyesi, yön ve aynı yönde ekleme. Bulduğu hata yalnızca o girişi
reddeder.

description: Opus'un kontrol adımlarının kod denetimleri ve ret metinleri.
"""

# NOTE: Biçimi şema zorlar, burada yalnızca şemanın ifade edemediği
# denetimler var: Opus'un yazdığı rakamlarla seçtiği yönün çelişmesi, hesapla
# listenin tutması, gerçek riskin hesabı. Her ret metni hangi kontrol olduğunu
# ve rakamlarla nedenini söyler. Kalan düzeltme hakkını istem ekler.
#
# NOTE: Opus'un beyanı Opus'u bağlar, talimat/ilkeler.md. Bir kontrol Opus'un
# yerine karar vermez, yalnızca kendi yazdığıyla çelişen eylemi reddeder.
# Yeni bir kontrol eklenmeden önce bunun bir düşünme adımı mı yoksa Opus'un
# yerine verilen bir karar mı olduğu tartılır.
#
# NOTE: Risk hesabı kâğıt broker'ın formülleriyle yapılır, ayrı bir kâr ve
# zarar formülü yoktur. SL'siz girişin riski izole marjinin tamamıdır, en
# kötü durumda likidasyonda giden odur.
#
# XXX: Ret metinleri Opus'a gider. Düşünce süreci gibi ifadeler kullanılmaz,
# Opus çağrıyı reddediyor.

from dataclasses import dataclass, field

from ...domain import numbers, rules
from ...domain.fees import FeeSchedule
from ...domain.models import Side
from ...domain.trading import OrderType, PlaceOrder, Ref
from ..base import Context
from . import notebook as nbk
from .notebook import Notebook
from .schema import REGIMES
from .texts import INTERVAL_NAMES, price

# Gerçek risk beyanı bu kat aşarsa giriş reddedilir. Komisyon ve kayma gibi
# Opus'un tam hesaplayamayacağı paylar için yer bırakır.
RISK_SLACK = 1.25

# Karşı kanıt alanında yok anlamına gelen kısa cevaplar.
_EMPTY_WORDS = ("yok", "yoktur", "bulunmuyor", "bulunamadı", "görünmüyor", "none", "-", "hiç yok",
                "karşı kanıt yok", "aykırı veri yok")

# Karşı kanıtın en kısa boyu. Bir veri ve neden ağır basmadığı bir cümleden
# kısa olamaz.
_MIN_COUNTER = 15

_DIR_NAMES = {"yukselen": "yükselen", "alcalan": "alçalan", "yatay": "yatay", "belirsiz": "belirsiz"}


@dataclass
class Review:
    """validate() sonucu.

    Args:
        errors: Kontrol hataları, biri bile varsa karar reddedilir.
        cancels: Bozuldu denen bekleyen girişler.
        expectations: Rejimi uygun olmayan paritelerde beklenen fiyat, sembol ve fiyat.
        wait_until: Beklenen saatlerin en erkeni.
        notes: Ret olmayan açıklamalar, örneğin geçerli denen planın taşındığı.
        fit: Parite başına rejimin uygun olup olmadığı.
        aligned: Parite başına uyumun yönü, long, short ya da None.
        trend: Parite başına üst aralığın yönünün izin verdiği giriş yönü, belirsizse None.
    """

    errors: list[str] = field(default_factory=list)
    cancels: list[Ref] = field(default_factory=list)
    expectations: list[tuple[str, float]] = field(default_factory=list)
    wait_until: int | None = None
    notes: list[str] = field(default_factory=list)
    fit: dict[str, bool] = field(default_factory=dict)
    aligned: dict[str, str | None] = field(default_factory=dict)
    trend: dict[str, str | None] = field(default_factory=dict)


def _fail(step: str, why: str) -> str:
    return f"Kontrol: {step}. Neden: {why}"


def _pair_list(xs: list[float]) -> str:
    return " ve ".join(price(x) for x in xs)


# --- Adım 1 ---

def _book(k: dict, ctx: Context, old: Notebook, out: Review) -> None:
    review = k["defter_sinamasi"]
    state = ctx.state
    entries = {(o.number, o.symbol, str(o.side)) for o in state.orders if not o.reduce}
    given = [(x["numara"], x["sembol"], x["yon"]) for x in review["emirler"]]
    for n, sym, side in sorted(entries - set(given)):
        out.errors.append(_fail("adım 1, emirler", f"#{n} {sym} {side} bekleyen giriş emri listede yok. Her "
                                                   f"bekleyen giriş emrini sına."))
    for n, sym, side in sorted(set(given) - entries):
        out.errors.append(_fail("adım 1, emirler", f"#{n} {sym} {side} diye bekleyen bir giriş emrin yok. Yalnızca "
                                                   f"hesap bölümündeki bekleyen giriş emirlerini yaz."))
    if len(given) != len(set(given)):
        out.errors.append(_fail("adım 1, emirler", "Aynı emri birden çok kez yazdın, her emir bir kez sınanır."))

    positions = {(p.symbol, str(p.side)) for p in state.positions}
    named = [(x["sembol"], x["yon"]) for x in review["pozisyonlar"]]
    for sym, side in sorted(positions - set(named)):
        out.errors.append(_fail("adım 1, pozisyonlar", f"{sym} {side} pozisyonu listede yok. Her açık pozisyonu sına."))
    for sym, side in sorted(set(named) - positions):
        out.errors.append(_fail("adım 1, pozisyonlar", f"Açık {sym} {side} pozisyonun yok."))
    if len(named) != len(set(named)):
        out.errors.append(_fail("adım 1, pozisyonlar", "Aynı pozisyonu birden çok kez yazdın."))

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
            out.errors.append(_fail("adım 1, pozisyonlar", f"{x['sembol']} {x['yon']} pozisyonunun tezi bozuldu dedin "
                                                           f"ama aynı kararda pozisyon_kapat ile tamamını kapatmadın. "
                                                           f"Ya kapat ya da sınamanı düzelt."))

    _plan(k, ctx, old, out)


def _plan(k: dict, ctx: Context, old: Notebook, out: Review) -> None:
    new = k["not_defteri"]
    for p in nbk.problems(new):
        out.errors.append(_fail("not defteri", p))
    choice = k["defter_sinamasi"]["plan"]["durum"]
    expired = nbk.plan_state(old, ctx.now) == nbk.EXPIRED
    if choice == "gecerli" and not expired:
        if new["plan"] != old.plan:
            out.notes.append("Planı geçerli dediğin için eski plan aynen taşındı, yeni yazdığın plan kullanılmadı.")
        return
    if (choice == "bozuldu" or expired) and old.plan is not None and new["plan"] == old.plan:
        why = "süresi doldu" if expired else "bozuldu dedin"
        out.errors.append(_fail("adım 1, plan", f"Planın {why}, aynı planı yeniden yazamazsın. Bugünkü veriyle yeni "
                                                f"bir plan yaz."))
    try:
        if nbk.parse_until(new["plan"]["gecerlilik"]) <= ctx.now:
            out.errors.append(_fail("adım 1, plan", f"Yeni planın geçerlilik saati {new['plan']['gecerlilik']} "
                                                    f"geçmişte kalıyor."))
    except ValueError:
        pass


# --- Adım 2 ve 3 ---

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


def _pairs(k: dict, ctx: Context, structure: tuple[str, str], fit_regimes: tuple[str, ...], out: Review) -> None:
    for sym, pair in k["pariteler"].items():
        levels = pair["yapi"]
        for key, interval in zip(("ust", "alt"), structure):
            if why := _contradiction(levels[key]):
                out.errors.append(_fail(f"adım 2, {sym} {INTERVAL_NAMES[interval]} yapı", why))
        aligned = _alignment(levels["ust"], levels["alt"])
        out.aligned[sym] = aligned
        top = levels["ust"]["yon"]
        out.trend[sym] = {"yukselen": "long", "alcalan": "short"}.get(top)

        r = pair["rejim"]
        regime, fit, wait = r["rejim"], r["uygun"] == "evet", r["bekledigi"]
        out.fit[sym] = fit
        if regime == "duzenli_trend" and aligned is None:
            low = levels["alt"]
            out.errors.append(_fail(f"adım 3, {sym} rejim",
                                    f"düzenli trend seçtin ama 2. adımda uyum yok: {INTERVAL_NAMES[structure[0]]} yön "
                                    f"{_DIR_NAMES[top]}, {INTERVAL_NAMES[structure[1]]} son tepeler "
                                    f"{_pair_list(low['tepeler'])}, son dipler {_pair_list(low['dipler'])}. Düzenli "
                                    f"trend için üst yön belli olmalı ve alt aralıkta o yönde ilk yapı oluşmalı."))
        if fit and regime not in fit_regimes:
            names = ", ".join(REGIMES[x] for x in fit_regimes)
            out.errors.append(_fail(f"adım 3, {sym} rejim", f"uygun dedin ama {REGIMES[regime]} stratejinin çalıştığı "
                                                            f"rejimlerden değil. Uygun rejimler: {names}."))
        if fit:
            continue
        if wait["tur"] == "fiyat" and wait["fiyat"] > 0:
            out.expectations.append((sym, float(wait["fiyat"])))
        elif wait["tur"] == "saat":
            try:
                t = nbk.parse_until(wait["saat"])
            except ValueError as e:
                out.errors.append(_fail(f"adım 3, {sym} bekledigi", str(e)))
                continue
            if t <= ctx.now:
                out.errors.append(_fail(f"adım 3, {sym} bekledigi", f"beklediğin saat {wait['saat']} geçmişte."))
            else:
                out.wait_until = t if out.wait_until is None else min(out.wait_until, t)
        else:
            out.errors.append(_fail(f"adım 3, {sym} bekledigi", "rejimi uygun bulmadın ama neyi beklediğini yazmadın. "
                                                               "Bir fiyat ya da bir saat yaz."))


# --- Adım 4 ---

def _closed(k: dict, ctx: Context, out: Review) -> None:
    want = {c.position_id for c in ctx.facts.unreviewed}
    given = [x["pozisyon_no"] for x in k["kapanan_islemler"]]
    if missing := sorted(want - set(given)):
        out.errors.append(_fail("adım 4, kapanan işlemler", f"Sınıflandırılmayı bekleyen pozisyonlar listede yok: "
                                                            f"{', '.join(f'#{n}' for n in missing)}."))
    if extra := sorted(set(given) - want):
        out.errors.append(_fail("adım 4, kapanan işlemler", f"Sınıflandırılmayı beklemeyen pozisyonları yazdın: "
                                                            f"{', '.join(f'#{n}' for n in extra)}. Yalnızca hesap "
                                                            f"bölümünde bekleyenleri yaz."))
    if len(given) != len(set(given)):
        out.errors.append(_fail("adım 4, kapanan işlemler", "Bir pozisyonu birden çok kez sınıflandırdın."))


def validate(k: dict, ctx: Context, structure: tuple[str, str], fit_regimes: tuple[str, ...],
             old: Notebook) -> Review:
    """Kararın kontrol adımlarını denetler.

    Args:
        k: Opus'un kararı, şemaya uymuş.
        ctx: Uyanmanın bağlamı, hesap ve hazır gerçekler.
        structure: Profilin üst ve alt yapı aralığı.
        fit_regimes: Profilin uygun rejimleri.
        old: Hafızadaki defter, planın sınaması için.
    """
    out = Review()
    _book(k, ctx, old, out)
    _pairs(k, ctx, structure, fit_regimes, out)
    _closed(k, ctx, out)
    return out


# --- Adım 5 ---

def cancelled_by(k: dict, review: Review) -> set[tuple[str, str]]:
    """Bu kararda iptal edilen bekleyen girişlerin parite ve yönleri: bozuldu denenler ve emir_iptal eylemleri."""
    out = {(r.symbol, str(r.side)) for r in review.cancels}
    out |= {(a["sembol"], a["yon"]) for a in k["eylemler"] if a.get("tip") == "emir_iptal"}
    return out


# Pozisyonun en kötü stopu. İz süren stopun seviyesi gördüğü en iyi fiyattan
# hesaplanır. Stop yoksa None.
def _worst_stop(ctx: Context, symbol: str, side: Side) -> float | None:
    levels = []
    for o in ctx.state.orders:
        if not (o.reduce and o.symbol == symbol and o.side is side):
            continue
        if o.type is OrderType.STOP:
            levels.append(o.price)
        elif o.type is OrderType.TRAILING_STOP and o.best:
            levels.append(rules.trail_level(side, o.best, o.trail_pct))
    if not levels:
        return None
    return min(levels) if side is Side.LONG else max(levels)


def _loss(side: Side, entry: float, qty: float, stop: float | None, margin: float, taker: float) -> float:
    if stop is None:
        return margin + qty * entry * taker
    return max(0.0, -rules.unrealized_pnl(side, entry, qty, stop)) + qty * entry * taker + qty * stop * taker


def _entry_price(cmd: PlaceOrder, ctx: Context) -> float:
    return cmd.price if cmd.price is not None else ctx.prices[cmd.symbol]


def _counter(text: str) -> bool:
    t = text.strip().lower().rstrip(".!")
    return len(t) >= _MIN_COUNTER and t not in _EMPTY_WORDS and not t.startswith("yok")


def entry_problem(a: dict, cmd: PlaceOrder, k: dict, review: Review, ctx: Context, fees: FeeSchedule,
                  leverage: int, busy: set[tuple[str, str]]) -> str | None:
    """Bir girişin adım 5 denetimlerinden ilk takıldığının ret metnini döndürür, geçerse None.

    Args:
        a: Opus'un giriş eylemi.
        cmd: Eylemin çevrildiği giriş komutu, miktar ve fiyat buradan okunur.
        k: Kararın tamamı, Opus'un yazdığı yapı için.
        review: validate() sonucu.
        ctx: Uyanmanın bağlamı.
        fees: Komisyon oranları.
        leverage: Girişin kaldıracı, SL'siz girişin marjini için.
        busy: Bu kararda daha önce giriş verilen parite ve yönler.
    """
    sym, side = cmd.symbol, cmd.side
    key = (sym, str(side))
    entry = _entry_price(cmd, ctx)
    if not review.fit.get(sym, False):
        return _fail(f"adım 3, {sym} rejim", "bu çağrıda rejimi stratejine uygun bulmadın, giriş yapamazsın.")
    trend = review.trend.get(sym)
    if trend is not None and trend != str(side):
        return _fail(f"adım 5, {sym} yön", f"üst aralığın yönünü {'yükselen' if trend == 'long' else 'alçalan'} "
                                           f"yazdın, {side} giriş bununla çelişiyor.")
    if not _counter(a["karsi_kanit"]):
        return _fail("adım 5, karşı kanıt", "girişe aykırı en güçlü veriyi ve neden ağır basmadığını yaz. Boş ya da "
                                            "yok anlamında bir cevap kabul edilmez.")
    inv = a["gecersizlik"]["fiyat"]
    if (side is Side.SHORT and inv <= entry) or (side is Side.LONG and inv >= entry):
        where = "üstünde" if side is Side.SHORT else "altında"
        return _fail("adım 5, geçersizlik", f"{side} girişte geçersizlik fiyatı girişin {where} olmalı. Giriş "
                                            f"{price(entry)}, geçersizlik {price(inv)}.")
    if ctx.facts.last_stop.get(f"{sym} {side}") and not (a.get("son_stoptan_farki") or "").strip():
        return _fail("adım 5, son stoptan farkı", f"{sym} {side} yönündeki son işlemin stopla kapandı. Bu girişin "
                                                  f"ondan farkını son_stoptan_farki alanına yaz.")

    low = k["pariteler"][sym]["yapi"]["alt"]
    sl = a.get("sl")
    if side is Side.SHORT:
        top = low["tepeler"][-1]
        if entry >= top:
            return _fail("adım 5, giriş seviyesi", f"short giriş {price(entry)}, yazdığın son alt aralık tepesi "
                                                   f"{price(top)} ya da üstünde. Giriş o tepenin altında olur. Önceki "
                                                   f"tepe stopunun yeridir, girişin değil.")
        if sl is not None and sl <= top:
            return _fail("adım 5, giriş seviyesi", f"SL {price(sl)}, son alt aralık tepesi {price(top)} ya da "
                                                   f"altında. Stop o tepenin ötesinde durur.")
    else:
        bottom = low["dipler"][-1]
        if entry <= bottom:
            return _fail("adım 5, giriş seviyesi", f"long giriş {price(entry)}, yazdığın son alt aralık dibi "
                                                   f"{price(bottom)} ya da altında. Giriş o dibin üstünde olur. Önceki "
                                                   f"dip stopunun yeridir, girişin değil.")
        if sl is not None and sl >= bottom:
            return _fail("adım 5, giriş seviyesi", f"SL {price(sl)}, son alt aralık dibi {price(bottom)} ya da "
                                                   f"üstünde. Stop o dibin ötesinde durur.")

    # Aynı yönde ekleme: açık pozisyonun stopu girişe taşınmadıysa, aynı yönde
    # bekleyen bir giriş varsa ya da bu kararda aynı yönde bir giriş zaten
    # verildiyse reddedilir. Bu kararda iptal edilen bekleyen giriş sayılmaz.
    p = ctx.state.position(sym, side)
    if p is not None:
        stop = _worst_stop(ctx, sym, side)
        safe = stop is not None and (stop >= p.entry_price if side is Side.LONG else stop <= p.entry_price)
        if not safe:
            return _fail("adım 5, aynı yönde ekleme", f"açık {sym} {side} pozisyonunun stopu girişe, "
                                                      f"{price(p.entry_price)}, taşınmadı. Önce stopu girişe taşı.")
    pending = [o for o in ctx.state.orders if not o.reduce and o.symbol == sym and o.side is side]
    if (pending and key not in cancelled_by(k, review)) or key in busy:
        return _fail("adım 5, aynı yönde ekleme", f"{sym} {side} yönünde zaten bekleyen bir giriş var. Aynı yönde "
                                                  f"ikinci giriş ilk pozisyonun stopu girişe taşınmadan verilmez.")

    # Gerçek risk: bu girişin SL'deki zararı ve iki yönlü taker komisyonu,
    # aynı yöndeki açık pozisyonun en kötü stopundaki zararla birlikte.
    margin = cmd.qty * entry / leverage
    loss = _loss(side, entry, cmd.qty, sl, margin, fees.taker)
    if p is not None:
        loss += _loss(side, p.entry_price, p.qty, _worst_stop(ctx, sym, side), p.margin, fees.taker)
    equity = ctx.state.equity
    real = loss / equity * 100 if equity > 0 else float("inf")
    said = float(a["risk_beyani"]["yuzde"])
    if real > said * RISK_SLACK + 1e-9:
        basis = "SL'deki zarar ve komisyon" if sl is not None else "SL olmadığı için marjinin tamamı ve komisyon"
        return _fail("adım 5, risk beyanı", f"beyanın yüzde {numbers.num(said, 2)}, gerçek risk yüzde "
                                            f"{numbers.num(real, 2)}: {basis} {numbers.num(loss, 2)} dolar, özkaynak "
                                            f"{numbers.num(equity, 2)} dolar"
                                            f"{', aynı yöndeki açık pozisyon dahil' if p else ''}. Beyanını gerçek "
                                            f"rakamla yaz ya da girişi değiştir.")
    return None
