"""
file: actions.py
importable: schema, to_command

Opus'un karar şemasındaki eylemler burada tanımlanır. Her eylem tek bir
kayıttır: Türkçe tipi, Opus'a görünen açıklaması, zorunlu ve isteğe bağlı
alanları ve kapının hangi komutuna çevrildiği. schema() profilin
paritelerine göre eylem listesinin JSON şemasını kurar, to_command() Opus'un
bir eylemini engine/commands.py'deki Command'e çevirir.

description: Opus'un eylemlerinin şeması, açıklamaları ve kapıya çevirisi.
"""

# NOTE: Eylem açıklamaları talimatta değil şemada durur. Talimatın çekirdeği
# değişince dönem sıfırlanır, şemaya yeni bir eylem eklemek ise sıfırlatmaz.
# Yeni bir eylem için _ACTIONS listesine bir kayıt, kapıda yeni bir komut
# gerekiyorsa commands.py'ye bir dal eklenir.
#
# NOTE: Şemada her eylem ayrı bir seçenektir, tip alanı sabit değerdir. claude
# komutunun --json-schema seçeneği bu anyOf yapısını kabul ediyor, 17.11'de
# Haiku ile tek çağrıyla denendi. Böylece zorunlu bir alanın eksikliğini
# şema daha çağrıdayken yakalar.
#
# XXX: Alan adları ve eylem tipleri Opus'a görünür, Türkçe kalır.

from ..engine.account import AccountError
from ..engine.commands import Command

_LEVEL = {
    "type": "object", "additionalProperties": False, "required": ["fiyat", "yuzde"],
    "properties": {"fiyat": {"type": "number"}, "yuzde": {"type": "number"}},
}

# Alanların şeması. sembol profilin paritelerine göre schema() içinde kurulur.
_FIELDS = {
    "yon": {"type": "string", "enum": ["long", "short"]},
    "pozisyon_id": {"type": "integer"},
    "emir_id": {"type": "integer"},
    "alarm_id": {"type": "integer"},
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
}

# Türkçe alan adının komuttaki karşılığı.
_PARAMS = {
    "sembol": "symbol", "yon": "side", "pozisyon_id": "position_id", "emir_id": "order_id", "alarm_id": "alert_id",
    "tetik_fiyat": "trigger_price", "kaldirac": "leverage", "marjin_usd": "margin", "yuzde": "pct", "tp": "tp",
    "sl": "sl", "iz_yuzde": "trail_pct", "gecerlilik_dk": "valid_min", "fiyat": "price", "not": "note",
}

_PROTECTION = ["tp", "tp_kademeleri", "sl", "iz_yuzde"]

# tip, açıklama, zorunlu alanlar, isteğe bağlı alanlar, komut ve komuta
# eklenecek sabit parametreler.
_ACTIONS = [
    ("long_ac", "Piyasa fiyatından yeni bir long pozisyon açar. tp ya da tp_kademeleri, sl ve iz_yuzde isteğe bağlı.",
     ["sembol", "kaldirac", "marjin_usd"], _PROTECTION, "open", {"side": "long"}),
    ("short_ac", "Piyasa fiyatından yeni bir short pozisyon açar. tp ya da tp_kademeleri, sl ve iz_yuzde isteğe bağlı.",
     ["sembol", "kaldirac", "marjin_usd"], _PROTECTION, "open", {"side": "short"}),
    ("emir_ver", "Bekleyen emir verir. tp ya da tp_kademeleri, sl, iz_yuzde ve gecerlilik_dk isteğe bağlı. Seviyeler "
     "tetik fiyatına göre doğru tarafta olmalı.",
     ["sembol", "yon", "tetik_fiyat", "kaldirac", "marjin_usd"], _PROTECTION + ["gecerlilik_dk"], "place_order", {}),
    ("emir_iptal", "emir_id ile bekleyen bir emri iptal eder.", ["emir_id"], [], "cancel_order", {}),
    ("pozisyon_ekle", "pozisyon_id, kaldirac ve marjin_usd ile açık bir pozisyona piyasa fiyatından ekleme yapar.",
     ["pozisyon_id", "kaldirac", "marjin_usd"], [], "add", {}),
    ("pozisyon_kapat", "pozisyon_id ile bir pozisyonu piyasa fiyatından kapatır. yuzde verirsen o kadarını kapatır, "
     "vermezsen tamamını.", ["pozisyon_id"], ["yuzde"], "close", {}),
    ("koruma_guncelle", "pozisyon_id ile bir pozisyonun tp ya da tp_kademeleri, sl ve iz_yuzde değerlerini değiştirir. "
     "Vermediğin alan değişmez, 0 verirsen kaldırılır. Yeni kademelerin yüzdeleri pozisyonun kalan miktarına göredir.",
     ["pozisyon_id"], _PROTECTION, "protect", {}),
    ("alarm_kur", "sembol, fiyat ve not ile bir fiyat alarmı kurar.", ["sembol", "fiyat", "not"], [], "set_alert", {}),
    ("alarm_iptal", "alarm_id ile bir alarmı iptal eder.", ["alarm_id"], [], "cancel_alert", {}),
]

_BY_TYPE = {a[0]: a for a in _ACTIONS}


def schema(symbols: list[str]) -> dict:
    """Eylem listesinin bir elemanının JSON şemasını döndürür.

    Args:
        symbols: Profilin işlem yapılabilen pariteleri, sembol alanının seçenekleri.
    """
    fields = dict(_FIELDS, sembol={"type": "string", "enum": list(symbols)})
    options = []
    for tip, text, required, optional, _, _ in _ACTIONS:
        props = {"tip": {"type": "string", "const": tip}}
        props.update({f: fields[f] for f in required + optional})
        options.append({"type": "object", "additionalProperties": False, "description": text,
                        "required": ["tip"] + required, "properties": props})
    return {"anyOf": options}


# Eksik bir zorunlu alan KeyError'ı Türkçe adıyla fırlatır, zamanlayıcı bu
# adı Opus'a gösterir. Şema zorunlu alanları zaten istediği için bu pratikte
# yalnızca şemaya uymayan bir cevapta olur.
def to_command(a: dict) -> Command:
    """Opus'un bir eylemini opus kaynaklı bir Command'e çevirir.

    Raises:
        KeyError: Eylemin zorunlu bir alanı eksikse, Türkçe alan adıyla.
        AccountError: Eylem tipi bilinmiyorsa.
    """
    if a.get("tip") not in _BY_TYPE:
        raise AccountError(f"Bilinmeyen eylem tipi: {a.get('tip')}")
    _, _, required, optional, kind, fixed = _BY_TYPE[a["tip"]]
    params = dict(fixed)
    for f in required:
        params[_PARAMS.get(f, f)] = a[f]
    for f in optional:
        if f == "tp_kademeleri":
            if a.get(f) is not None:
                params["tp_levels"] = [{"price": k.get("fiyat"), "pct": k.get("yuzde")} for k in a[f]]
        elif a.get(f) is not None:
            params[_PARAMS[f]] = a[f]
    return Command(kind, params, "opus")
