"""
file: notebook.py
importable: PLAN_TEXT_MAX, PLAN_BREAK_MAX, NOTE_MAX, AGENDA_MAX, PLAN_REVIEWS, STRUCTURE_REVIEWS, AGENDA_REVIEWS,
            EXPIRED, Notebook, schema, read, parse_until, plan_state, problems, merge, text

Opus'un not defteri. Defter üç alandır: plan, yapı ve gündem. read()
hafızadaki defteri okur, eski biçimdeki düz metni de tanır. plan_state()
planın süresinin dolup dolmadığını söyler. merge() Opus'un defter sınamasına
göre eski defterle yenisini birleştirir: geçerli denen plan aynen taşınır,
diğer durumlarda yeni plan alınır. problems() bir defterin sınırlarını ve
biçimini denetler, text() istemdeki Not defterin bölümünü yazar. schema()
karar şemasındaki not_defteri alanıdır.

description: Not defterinin şeması, sınırları, okunması ve birleştirilmesi.
"""

# NOTE: Defter hafızada memory["notebook"] altında sözlük olarak durur. Eski
# talimatın defteri düz metindi. O metin silinmez, istemde eski biçim diye
# gösterilir ve planın süresi dolmuş sayılır. Opus ilk çağrıda yeni plan
# yazmak zorunda kalır, eski bilgiyi yeni biçime kendisi taşır.
#
# NOTE: Yapının tepe ve dip rakamlarını Opus defterde yazmaz. Kararın
# pariteler alanında 2. adımda yazdığı değerler sistem tarafından deftere
# taşınır, talimat/defter.md. Opus yalnızca seviyelerin notunu yazar.
#
# NOTE: Sınırlar hem şemada maxLength olarak hem burada durur. Şema claude
# komutunda zorlanır, Opus aşan metni aynı çağrıda kısaltır. problems()
# yönetici panelden defteri elle değiştirdiğinde aynı sınırları uygular.
#
# XXX: Alan adları ve seçenekler Opus'a görünür, Türkçe kalır.

from dataclasses import dataclass, field
from datetime import datetime

from .texts import INTERVAL_NAMES, TZ, clock, price

# --- Sınırlar ---

# Plan toplam dört yüz karakter kadardır, iki üç cümlelik metin ve bir cümlelik
# bozulma koşulu. Yapı notu kilit seviyeleri anlatır, gündem tarihli olayları.
PLAN_TEXT_MAX = 300
PLAN_BREAK_MAX = 100
NOTE_MAX = 600
AGENDA_MAX = 500

# Geçerlilik saatinin biçimi, Türkiye saatiyle.
_UNTIL_FORMAT = "%d.%m.%Y %H:%M"
_UNTIL_PATTERN = r"^[0-3][0-9]\.[01][0-9]\.20[0-9]{2} [0-2][0-9]:[0-5][0-9]$"

# --- Defter sınaması ---

# Opus'un eski defteri sınarken seçtikleri, talimat/defter.md. Süresi doldu
# Opus'un seçeneği değildir, saati geçen plana sistem koyar. Gerçekleşti,
# planın beklediği kurulumun oluştuğudur, bozuldu gibi yeni plan ister.
PLAN_REVIEWS = ("gecerli", "zayifladi", "gerceklesti", "bozuldu")
STRUCTURE_REVIEWS = ("ayni", "guncellendi", "bozuldu")
AGENDA_REVIEWS = ("etkisi_yok", "etkiliyor")
EXPIRED = "suresi_doldu"


@dataclass(frozen=True)
class Notebook:
    """Okunmuş bir defter.

    Args:
        plan: metin, bozulma ve gecerlilik alanlarıyla plan, hiç yoksa None.
        note: Yapı notu.
        swings: Parite ve aralık başına sistemin taşıdığı tepeler ve dipler.
        agenda: Gündem.
        legacy: Eski biçimdeki düz metin, yeni biçimde boş.
    """

    plan: dict | None = None
    note: str = ""
    swings: dict = field(default_factory=dict)
    agenda: str = ""
    legacy: str = ""

    def to_memory(self) -> dict:
        return {"plan": self.plan, "yapi": {"seviyeler_notu": self.note, "rakamlar": self.swings},
                "gundem": self.agenda}


def schema() -> dict:
    """Karar şemasındaki not_defteri alanını döndürür."""
    plan = {"type": "object", "additionalProperties": False, "required": ["metin", "bozulma", "gecerlilik"],
            "properties": {
                "metin": {"type": "string", "maxLength": PLAN_TEXT_MAX,
                          "description": "İki üç cümle: hangi koşulda ne yapacağın."},
                "bozulma": {"type": "string", "maxLength": PLAN_BREAK_MAX,
                            "description": "Planı bozan tek koşul, mümkünse bir fiyatla."},
                "gecerlilik": {"type": "string", "pattern": _UNTIL_PATTERN,
                               "description": "Planın geçerli olduğu son an, Türkiye saatiyle, 22.11.2026 18:00 "
                                              "biçiminde."}}}
    return {"type": "object", "additionalProperties": False, "required": ["plan", "seviyeler_notu", "gundem"],
            "description": "Not defterin. Defter sınamasında planı geçerli dediysen planın metni ve bozulma "
                           "koşulu aynen taşınır, buradaki planından yalnızca geçerlilik saati alınır.",
            "properties": {
                "plan": plan,
                "seviyeler_notu": {"type": "string", "maxLength": NOTE_MAX,
                                   "description": "Kilit seviyeler ve neden önemli oldukları. Tepe ve dip "
                                                  "rakamlarını sistem pariteler alanından taşır."},
                "gundem": {"type": "string", "maxLength": AGENDA_MAX,
                           "description": "Etkisi süren haberler ve yaklaşan olaylar, tarihleriyle."}}}


def read(memory: dict) -> Notebook:
    """Hafızadaki defteri okur. Düz metin eski biçimdir, legacy alanına girer."""
    raw = memory.get("notebook")
    if not raw:
        return Notebook()
    if isinstance(raw, str):
        return Notebook(legacy=raw)
    yapi = raw.get("yapi") or {}
    return Notebook(plan=raw.get("plan"), note=yapi.get("seviyeler_notu", ""), swings=yapi.get("rakamlar") or {},
                    agenda=raw.get("gundem", ""))


def parse_until(text: str) -> int:
    """Geçerlilik saatini unix saniyeye çevirir.

    Raises:
        ValueError: Biçim 22.11.2026 18:00 değilse.
    """
    try:
        return int(datetime.strptime(text, _UNTIL_FORMAT).replace(tzinfo=TZ).timestamp())
    except (TypeError, ValueError):
        raise ValueError(f"Geçerlilik saati 22.11.2026 18:00 biçiminde olmalı, yazılan: {text}") from None


def plan_state(nb: Notebook, now: int) -> str | None:
    """Plan yoksa ya da süresi dolduysa EXPIRED, yoksa None döndürür. Okunamayan saat süresi dolmuş sayılır."""
    if nb.plan is None:
        return EXPIRED
    try:
        return EXPIRED if parse_until(nb.plan.get("gecerlilik", "")) <= now else None
    except ValueError:
        return EXPIRED


def problems(payload: dict) -> list[str]:
    """not_defteri biçimindeki bir defterin sınır ve biçim hatalarını Türkçe döndürür, hata yoksa boş liste."""
    out = []
    plan = payload.get("plan") or {}
    limits = (("plan metni", plan.get("metin", ""), PLAN_TEXT_MAX),
              ("bozulma koşulu", plan.get("bozulma", ""), PLAN_BREAK_MAX),
              ("seviyeler notu", payload.get("seviyeler_notu", ""), NOTE_MAX),
              ("gündem", payload.get("gundem", ""), AGENDA_MAX))
    for name, value, limit in limits:
        if not isinstance(value, str):
            out.append(f"{name} metin olmalı.")
        elif len(value) > limit:
            out.append(f"{name} {len(value)} karakter, en fazla {limit} olabilir.")
    if not plan.get("metin", "").strip():
        out.append("plan metni boş olamaz.")
    try:
        parse_until(plan.get("gecerlilik", ""))
    except ValueError as e:
        out.append(str(e))
    return out


def merge(old: Notebook, new: dict, review: str, swings: dict, now: int) -> tuple[Notebook, bool]:
    """Eski defterle Opus'un yeni defterini birleştirir.

    Args:
        old: Hafızadaki defter.
        new: Kararın not_defteri alanı.
        review: Opus'un defter sınamasında plan için seçtiği, PLAN_REVIEWS'tan biri.
        swings: Kararın pariteler alanından parite ve aralık başına tepeler ve dipler.
        now: Şu an, planın süresi için.

    Returns:
        Yeni defter ve eski planın metniyle bozulma koşulunun Opus'un farklı yazdığına rağmen taşınıp taşınmadığı.
    """
    # Geçerli denen planın metni ve bozulma koşulu kilitlidir, geçerlilik
    # saati yenisinden alınır. Opus süren bir planı yeniden yazmadan uzatabilir.
    keep = review == "gecerli" and plan_state(old, now) is None
    if keep:
        plan = {**old.plan, "gecerlilik": new["plan"]["gecerlilik"]}
        carried = (new["plan"]["metin"], new["plan"]["bozulma"]) != (old.plan.get("metin"), old.plan.get("bozulma"))
    else:
        plan, carried = new["plan"], False
    return Notebook(plan=plan, note=new["seviyeler_notu"], swings=swings, agenda=new["gundem"]), carried


def _swings_text(swings: dict) -> list[str]:
    lines = []
    for symbol, by_interval in swings.items():
        for interval, v in by_interval.items():
            tops = ", ".join(price(x) for x in v.get("tepeler", [])) or "yok"
            lows = ", ".join(price(x) for x in v.get("dipler", [])) or "yok"
            lines.append(f"- {symbol} {INTERVAL_NAMES.get(interval, interval)}: tepeler {tops}, dipler {lows}")
    return lines


def text(nb: Notebook, now: int, written_at: int | None = None) -> str:
    """İstemdeki Not defterin bölümünü yazar. Süresi dolan plan öyle işaretlenir."""
    out = ["# Not defterin", "Sınanacak eski hali. Kararının defter_sinamasi alanında her bölümünü bugünkü veriyle "
           "değerlendir."]
    if nb.legacy:
        out.append("Defterin eski biçimde, serbest metin. Planın süresi dolmuş sayılır, yeni biçimde yeni bir plan "
                   "yazmalısın. Eski metin:\n\n" + nb.legacy)
        return "\n\n".join(out)
    if nb.plan is None:
        out.append("## Plan\n\nPlan yok, yeni bir plan yazmalısın.")
    else:
        expired = plan_state(nb, now) == EXPIRED
        head = "## Plan, süresi doldu, yeni bir plan yazmalısın" if expired else "## Plan"
        out.append(f"{head}\n\n{nb.plan.get('metin', '')}\nBozulma koşulu: {nb.plan.get('bozulma', '')}\n"
                   f"Geçerlilik: {nb.plan.get('gecerlilik', '')}")
    structure = ["## Yapı"]
    if lines := _swings_text(nb.swings):
        when = f", {clock(written_at)} kararından" if written_at else ""
        structure.append(f"Son okuduğun tepe ve dipler{when}:\n" + "\n".join(lines))
    structure.append(f"Seviyelerin notu: {nb.note or 'boş'}")
    out.append("\n\n".join(structure))
    out.append(f"## Gündem\n\n{nb.agenda or 'Boş.'}")
    return "\n\n".join(out)
