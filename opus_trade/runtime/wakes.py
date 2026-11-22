"""
file: wakes.py
importable: encode, decode, event_from_dict

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
from dataclasses import asdict, fields
from typing import Any

from ..domain.models import Side
from ..domain.trading import AccountEvent, Actor, EventKind, OrderType, Trade
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


# Binance modeline göçten önce saklanan uyandırmalar eski alanları taşır:
# numara, kaldıraç ve yüzdeli TP kademeleri. Bilinmeyen alanlar düşer, TP
# kademelerinin fiyatları kalır.
def _trade(d: dict | None) -> Trade | None:
    if d is None:
        return None
    known = {f.name for f in fields(Trade)} - {"side", "tp_prices"}
    tps = d.get("tp_prices", [k["price"] for k in d.get("tp_levels", [])])
    return Trade(**{k: v for k, v in d.items() if k in known}, side=Side(d["side"]), tp_prices=tuple(tps))


def event_from_dict(d: dict) -> AccountEvent:
    """Sözlüğe çevrilmiş bir hesap olayını geri kurar. Defterdeki olayların detail alanı da bu biçimdedir."""
    known = {f.name for f in fields(AccountEvent)}
    return AccountEvent(**{**{k: v for k, v in d.items() if k in known}, "kind": EventKind(d["kind"]),
                           "actor": Actor(d["actor"]),
                           "side": Side(d["side"]) if d.get("side") else None, "trade": _trade(d.get("trade")),
                           "order_type": OrderType(d["order_type"]) if d.get("order_type") else None})


def decode(text: str | None) -> Any:
    if text is None:
        return None
    d = json.loads(text)
    kind = d.pop("type")
    if kind == "text":
        return d["text"]
    if kind == "event":
        return event_from_dict(d)
    inner = Wave(**d["data"]) if d["inner"] == "wave" else Alert(**d["data"])
    return Fire(d["kind"], d["time"], d["symbol"], inner)
