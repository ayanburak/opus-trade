"""
file: actions.py
importable: ACTIONS, schema, translate

Opus'un karar şemasındaki eylemler. Her eylem tek bir kayıttır: Türkçe tipi,
Opus'a görünen açıklaması, zorunlu ve isteğe bağlı alanları. schema() eylem
listesinin bir elemanının JSON şemasını profilin paritelerine göre kurar.
translate() Opus'un bir eylemini bir broker komutuna ya da bir alarm
işlemine çevirir, eylem kendi içinde çelişkiliyse ValueError fırlatır.

description: Opus'un eylemlerinin şeması ve komuta çevirisi.
"""

# NOTE: Numarayla bir kayda dokunan her eylem numara, sembol ve yon alanlarını
# birlikte ister. Kapı numaranın hâlâ o pariteyi ve yönü gösterdiğini kontrol
# eder, numara arada başka bir kayda geçtiyse eylem reddedilir.
#
# NOTE: Şemada her eylem ayrı bir seçenektir, tip alanı sabit değerdir. claude
# komutunun --json-schema seçeneği bu anyOf yapısını kabul ediyor. Böylece
# zorunlu bir alanın eksikliğini şema daha çağrıdayken yakalar.
#
# XXX: Alan adları ve eylem tipleri Opus'a görünür, Türkçe kalır.

from ...domain.models import Side, TpLevel
from ...domain.trading import (AddToPosition, CancelOrder, ClosePosition, Command, OpenPosition, PlaceOrder, Ref,
                               UpdateProtection)

_LEVEL = {"type": "object", "additionalProperties": False, "required": ["fiyat", "yuzde"],
          "properties": {"fiyat": {"type": "number"}, "yuzde": {"type": "number"}}}

_FIELDS = {
    "yon": {"type": "string", "enum": ["long", "short"]},
    "numara": {"type": "integer"},
    "tetik_fiyat": {"type": "number"},
    "kaldirac": {"type": "integer"},
    "marjin_usd": {"type": "number"},
    "yuzde": {"type": "number"},
    "tp": {"type": "number"},
    "tp_kademeleri": {"type": "array", "items": _LEVEL},
    "sl": {"type": "number"},
    "iz_yuzde": {"type": "number"},
    "gecerlilik_dk": {"type": "integer"},
    "fiyat": {"type": "number"},
    "not": {"type": "string"},
    "alarm_no": {"type": "integer"},
}

_PROTECTION = ["tp", "tp_kademeleri", "sl", "iz_yuzde"]
_REF = ["numara", "sembol", "yon"]

# tip, açıklama, zorunlu alanlar, isteğe bağlı alanlar.
ACTIONS = [
    ("long_ac", "Piyasa fiyatından yeni bir long pozisyon açar. tp ya da tp_kademeleri, sl ve iz_yuzde isteğe bağlı.",
     ["sembol", "kaldirac", "marjin_usd"], _PROTECTION),
    ("short_ac", "Piyasa fiyatından yeni bir short pozisyon açar. tp ya da tp_kademeleri, sl ve iz_yuzde isteğe bağlı.",
     ["sembol", "kaldirac", "marjin_usd"], _PROTECTION),
    ("emir_ver", "Bekleyen emir verir. tp ya da tp_kademeleri, sl, iz_yuzde ve gecerlilik_dk isteğe bağlı. Seviyeler "
     "tetik fiyatına göre doğru tarafta olmalı.",
     ["sembol", "yon", "tetik_fiyat", "kaldirac", "marjin_usd"], _PROTECTION + ["gecerlilik_dk"]),
    ("emir_iptal", "Bekleyen bir emri iptal eder. numara, sembol ve yon emrin hesaptaki değerleridir.", _REF, []),
    ("pozisyon_ekle", "Açık bir pozisyona piyasa fiyatından ekleme yapar. numara, sembol ve yon pozisyonun "
     "hesaptaki değerleridir.", _REF + ["kaldirac", "marjin_usd"], []),
    ("pozisyon_kapat", "Bir pozisyonu piyasa fiyatından kapatır. yuzde verirsen o kadarını kapatır, vermezsen "
     "tamamını. numara, sembol ve yon pozisyonun hesaptaki değerleridir.", _REF, ["yuzde"]),
    ("koruma_guncelle", "Bir pozisyonun tp ya da tp_kademeleri, sl ve iz_yuzde değerlerini değiştirir. Vermediğin "
     "alan değişmez, 0 verirsen kaldırılır. Yeni kademelerin yüzdeleri pozisyonun kalan miktarına göredir. numara, "
     "sembol ve yon pozisyonun hesaptaki değerleridir.", _REF, _PROTECTION),
    ("alarm_kur", "sembol, fiyat ve not ile bir fiyat alarmı kurar.", ["sembol", "fiyat", "not"], []),
    ("alarm_iptal", "alarm_no ile bir alarmı iptal eder.", ["alarm_no"], []),
]

_BY_TYPE = {a[0]: a for a in ACTIONS}


def schema(symbols: list[str]) -> dict:
    """Eylem listesinin bir elemanının JSON şemasını döndürür."""
    fields = dict(_FIELDS, sembol={"type": "string", "enum": list(symbols)})
    options = []
    for tip, text, required, optional in ACTIONS:
        props = {"tip": {"type": "string", "const": tip}}
        props.update({f: fields[f] for f in required + optional})
        options.append({"type": "object", "additionalProperties": False, "description": text,
                        "required": ["tip"] + required, "properties": props})
    return {"anyOf": options}


def _ref(a: dict) -> Ref:
    return Ref(int(a["numara"]), a["sembol"], Side(a["yon"]))


# Tek TP yüzde yüzlük tek bir kademedir. İkisi birlikte verilirse hangisinin
# istendiği bilinemez ve eylem reddedilir. Koruma güncellemesinde tp 0
# bütün kademeleri kaldırır, hiçbiri verilmezse kademeler değişmez.
def _levels(a: dict, update: bool) -> tuple[TpLevel, ...] | None:
    tp, levels = a.get("tp"), a.get("tp_kademeleri")
    if tp and levels:
        raise ValueError("tp ve tp_kademeleri birlikte verilemez, birini seç.")
    if levels:
        return tuple(TpLevel(float(k["fiyat"]), float(k["yuzde"])) for k in levels)
    if tp:
        return (TpLevel(float(tp), 100),)
    if update:
        return () if tp == 0 else None
    return ()


def translate(a: dict) -> tuple[str, Command | dict]:
    """Bir eylemi ("command", komut) ya da ("alarm", alarm işlemi) olarak döndürür.

    Alarm işlemi op anahtarı add ya da cancel olan bir sözlüktür.

    Raises:
        ValueError: Eylem tipi bilinmiyorsa, zorunlu bir alan eksikse ya da alanlar çelişiyorsa.
    """
    tip = a.get("tip")
    if tip not in _BY_TYPE:
        raise ValueError(f"Bilinmeyen eylem tipi: {tip}.")
    missing = [f for f in _BY_TYPE[tip][2] if a.get(f) is None]
    if missing:
        raise ValueError(f"{', '.join(missing)} alanı eksik.")
    match tip:
        case "long_ac" | "short_ac":
            return "command", OpenPosition(a["sembol"], Side.LONG if tip == "long_ac" else Side.SHORT,
                                           a["kaldirac"], a["marjin_usd"], _levels(a, False), a.get("sl"),
                                           a.get("iz_yuzde"))
        case "emir_ver":
            return "command", PlaceOrder(a["sembol"], Side(a["yon"]), a["tetik_fiyat"], a["kaldirac"],
                                         a["marjin_usd"], _levels(a, False), a.get("sl"), a.get("iz_yuzde"),
                                         a.get("gecerlilik_dk"))
        case "emir_iptal":
            return "command", CancelOrder(_ref(a))
        case "pozisyon_ekle":
            return "command", AddToPosition(_ref(a), a["kaldirac"], a["marjin_usd"])
        case "pozisyon_kapat":
            return "command", ClosePosition(_ref(a), a.get("yuzde") or 100)
        case "koruma_guncelle":
            return "command", UpdateProtection(_ref(a), _levels(a, True), a.get("sl"), a.get("iz_yuzde"))
        case "alarm_kur":
            return "alarm", {"op": "add", "symbol": a["sembol"], "price": a["fiyat"], "note": a["not"]}
    return "alarm", {"op": "cancel", "number": a["alarm_no"]}
