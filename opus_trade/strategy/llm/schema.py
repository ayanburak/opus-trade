"""
file: schema.py
importable: DIRECTIONS, REGIMES, REGIME_HINTS, FIT, VERDICTS, ITEM_REVIEWS, LEVELS, schema

Opus'un kararının JSON şeması. schema() profilin paritelerine ve yapı
aralıklarına göre şemayı kurar. Alanların sırası Opus'un düşünme sırasıdır:
önce kontrol adımları, sonra analiz ve eylemler, en son defter ve özet.
REGIMES rejimlerin tek kaynağıdır, talimat, profil ayarı ve arayüzün etiketi
buradan okunur.

description: Opus'un karar şeması ve şemadaki seçeneklerin sabitleri.
"""

# NOTE: claude komutu şemayı tamamen zorlar: maxLength, minItems, enum, const,
# pattern ve iç içe anyOf. Uymayan çıktı modele hatasıyla döner ve model aynı
# çağrıda düzeltir, 22.11 ölçümü. Bu yüzden biçim şemada durur, rakam ve
# hesap isteyen denetimler checks.py'dedir.
#
# NOTE: Yapı alanında aralıklar ust ve alt diye adlanır, hangi aralık olduğu
# açıklamada yazar. Profil aralıkları değiştirse de alan adları aynı kalır,
# kontroller ve arayüz ust ile alt'ı okur.
#
# XXX: Talimatta ve açıklamalarda düşünce süreci gibi ifadeler kullanılmaz,
# Opus çağrıyı reddediyor. Kontrol, gerekçe ve sınama kullanılır.
#
# XXX: Alan adları ve seçenekler Opus'a görünür, Türkçe kalır.

from . import actions, notebook
from .texts import INTERVAL_NAMES

# --- Seçenekler ---

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

# Kapanan pozisyonun sınıfları ve arayüzdeki etiketleri.
VERDICTS = {"tez_dogruydu": "tez doğruydu", "tez_yanlisti": "tez yanlıştı", "zamanlama_yanlisti": "zamanlama yanlıştı",
            "koruma_yanlis_yerdeydi": "koruma yanlış yerdeydi"}

# Bekleyen emir ve açık pozisyon için defter sınamasındaki seçenekler.
ITEM_REVIEWS = ("gecerli", "zayifladi", "bozuldu")

LEVELS = ("ust", "alt")

_TEXT = {"type": "string"}
_REASON = {"type": "string", "minLength": 1}


def _review(options: tuple[str, ...], text: str) -> dict:
    return {"type": "object", "additionalProperties": False, "required": ["durum", "gerekce"], "description": text,
            "properties": {"durum": {"type": "string", "enum": list(options)}, "gerekce": _REASON}}


def _book_review() -> dict:
    order = {"type": "object", "additionalProperties": False, "required": ["numara", "sembol", "yon", "durum", "gerekce"],
             "properties": {"numara": {"type": "integer"}, "sembol": _TEXT, "yon": {"type": "string", "enum": ["long", "short"]},
                            "durum": {"type": "string", "enum": list(ITEM_REVIEWS)}, "gerekce": _REASON}}
    position = {"type": "object", "additionalProperties": False, "required": ["sembol", "yon", "durum", "gerekce"],
                "properties": {"sembol": _TEXT, "yon": {"type": "string", "enum": ["long", "short"]},
                               "durum": {"type": "string", "enum": list(ITEM_REVIEWS)}, "gerekce": _REASON}}
    return {
        "type": "object", "additionalProperties": False,
        "description": "Adım 1. Eski defterin her bölümünü ve hesaptaki her bekleyen giriş emrini ve açık pozisyonu "
                       "bugünkü veriyle sına. Hepsi listede olmalı, fazlası da eksiği de karar reddine yol açar.",
        "required": ["plan", "yapi", "gundem", "emirler", "pozisyonlar"],
        "properties": {
            "plan": _review(notebook.PLAN_REVIEWS, "Plan bugünkü veriyle geçerli mi, zayıfladı mı, bozuldu mu. "
                                                   "Süresi dolan planı burada geçerli desen de yenisini yazarsın."),
            "yapi": _review(notebook.STRUCTURE_REVIEWS, "Defterdeki yapı aynı mı, güncellendi mi, bozuldu mu."),
            "gundem": _review(notebook.AGENDA_REVIEWS, "Yakın olaylar bugünkü kararı etkiliyor mu. Olay yoksa "
                                                       "gerekçe tek kelime olabilir."),
            "emirler": {"type": "array", "items": order,
                        "description": "Her bekleyen giriş emri için bir satır. Koruma emirleri yazılmaz. Bozuldu "
                                       "dediğin emri sistem iptal eder."},
            "pozisyonlar": {"type": "array", "items": position,
                            "description": "Her açık pozisyon için bir satır. Bozuldu dediğin pozisyonu aynı kararda "
                                           "pozisyon_kapat ile kapatırsın, yönetici açtıysa zorunlu değil."},
        },
    }


def _pair(structure: tuple[str, str]) -> dict:
    def level(interval: str) -> dict:
        two = {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2}
        return {"type": "object", "additionalProperties": False, "required": ["yon", "tepeler", "dipler"],
                "description": f"{INTERVAL_NAMES[interval]} grafik.",
                "properties": {"yon": {"type": "string", "enum": list(DIRECTIONS)},
                               "tepeler": {**two, "description": "Son iki tepe, eskiden yeniye."},
                               "dipler": {**two, "description": "Son iki dip, eskiden yeniye."}}}

    waiting = {"type": "object", "additionalProperties": False, "required": ["tur", "fiyat", "saat"],
               "description": "Uygun değilse neyi beklediğin. Fiyat beklersen sistem oraya alarm kurar, saat "
                              "beklersen o saatte çağrılırsın. Uygunsa tur yok olur.",
               "properties": {"tur": {"type": "string", "enum": ["fiyat", "saat", "yok"]},
                              "fiyat": {"type": "number", "description": "tur fiyat ise beklenen fiyat, değilse 0."},
                              "saat": {"type": "string", "description": "tur saat ise 22.11.2026 18:00 biçiminde, "
                                                                        "değilse boş."}}}
    return {
        "type": "object", "additionalProperties": False, "required": ["yapi", "rejim"],
        "properties": {
            "yapi": {"type": "object", "additionalProperties": False, "required": list(LEVELS),
                     "description": "Adım 2. Yönü kendi yazdığın tepe ve diplerle çelişemez.",
                     "properties": {"ust": level(structure[0]), "alt": level(structure[1])}},
            "rejim": {"type": "object", "additionalProperties": False,
                      "required": ["rejim", "kanit", "uygun", "bekledigi"],
                      "description": "Adım 3. Piyasanın rejimi ve stratejine uygun olup olmadığı.",
                      "properties": {"rejim": {"type": "string", "enum": list(REGIMES)},
                                     "kanit": {**_REASON, "description": "Rakamlarla: hareketin büyüklüğü, RSI, "
                                                                         "ATR'deki değişim, likidasyon dalgası."},
                                     "uygun": {"type": "string", "enum": list(FIT)},
                                     "bekledigi": waiting}},
        },
    }


def _closed() -> dict:
    item = {"type": "object", "additionalProperties": False, "required": ["pozisyon_no", "sinif", "aciklama"],
            "properties": {"pozisyon_no": {"type": "integer"}, "sinif": {"type": "string", "enum": list(VERDICTS)},
                           "aciklama": {**_REASON, "description": "Bir cümle."}}}
    return {"type": "array", "items": item,
            "description": "Adım 4. Hesap bölümünde sınıflandırılmayı bekleyen her pozisyon bir kez. Bekleyen yoksa "
                           "boş liste."}


def schema(symbols: list[str], structure: tuple[str, str]) -> dict:
    """Opus'un kararının uyması gereken JSON şemasını döndürür.

    Args:
        symbols: Profilin pariteleri. Her biri pariteler alanında zorunludur.
        structure: Profilin üst ve alt yapı aralığı, örneğin 4h ve 1h.
    """
    pair = _pair(structure)
    props = {
        "defter_sinamasi": _book_review(),
        "pariteler": {"type": "object", "additionalProperties": False, "required": list(symbols),
                      "properties": {s: pair for s in symbols}},
        "kapanan_islemler": _closed(),
        "analiz": {"type": "string", "description": "Adımlara sığmayan değerlendirmen ve kararının gerekçesi."},
        "eylemler": {"type": "array", "items": actions.schema(list(symbols))},
        "not_defteri": notebook.schema(),
        "ozet": {"type": "string", "description": "Tek cümle."},
        "sonraki_cagri_dk": {"type": "integer"},
        "yoneticiye_cevap": {"type": "string", "description": "Yöneticinin sorularına, verdiği haberlere ve "
                             "araştırmanı istediği konulara cevabın. Böyle bir müdahale yoksa boş bırak."},
        "yoneticiye_oneri": {"type": "string", "description": "İşlemlerinde gördüğün bir örüntü ve tarzına önerin. "
                             "Yoksa boş bırak. Deftere değil buraya yazılır."},
    }
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}
