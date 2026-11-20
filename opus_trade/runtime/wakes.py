"""
file: wakes.py
importable: encode, decode

Bir uyandırmanın verisini JSON'a çevirir ve geri kurar. encode() verinin
türünü de yazar, decode() o türe göre doğru tipi kurar. Bekleyen
uyandırmalar program yeniden başlayınca ve model erişimi açılınca bu yolla
geri gelir.

description: Uyandırma verisinin saklanabilir biçimi.
"""

# NOTE: Saklanan türler hesap olayı, likidasyon dalgası, alarm ve düz metindir.
# Düzeltme uyandırması saklanmaz, çünkü kararın hemen ardından aynı süreçte
# verilir. Yeni bir veri türü uyandırmaya girerse buraya da eklenir, yoksa
# encode() TypeError fırlatır.

import json
from dataclasses import asdict
from typing import Any

from ..domain.models import Side, TpLevel
from ..domain.trading import AccountEvent, Actor, EventKind, Trade
from ..triggers.base import Fire
from ..triggers.liquidation_wave import Wave
from ..triggers.price_alert import Alert


def encode(data: Any) -> str | None:
    if data is None:
        return None
    if isinstance(data, str):
        return json.dumps({"type": "text", "text": data}, ensure_ascii=False)
    if isinstance(data, AccountEvent):
        return json.dumps({"type": "event", **asdict(data)}, ensure_ascii=False)
    if isinstance(data, Fire) and isinstance(data.data, (Wave, Alert)):
        inner = "wave" if isinstance(data.data, Wave) else "alert"
        return json.dumps({"type": "fire", "kind": data.kind, "time": data.time, "symbol": data.symbol,
                           "inner": inner, "data": asdict(data.data)}, ensure_ascii=False)
    raise TypeError(f"Saklanamayan uyandırma verisi: {type(data).__name__}")


def _trade(d: dict | None) -> Trade | None:
    if d is None:
        return None
    return Trade(**{**d, "side": Side(d["side"]), "tp_levels": tuple(TpLevel(**k) for k in d["tp_levels"])})


def decode(text: str | None) -> Any:
    if text is None:
        return None
    d = json.loads(text)
    kind = d.pop("type")
    if kind == "text":
        return d["text"]
    if kind == "event":
        return AccountEvent(**{**d, "kind": EventKind(d["kind"]), "actor": Actor(d["actor"]), "side": Side(d["side"]),
                               "trade": _trade(d["trade"])})
    inner = Wave(**d["data"]) if d["inner"] == "wave" else Alert(**d["data"])
    return Fire(d["kind"], d["time"], d["symbol"], inner)
