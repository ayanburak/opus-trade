"""
file: specs.py
importable: specs_from, load_brackets, BRACKETS_FILE

specs_from() Binance'in exchangeInfo cevabını ve kaldıraç kademe tablosunu
masanın paritelerinin SymbolSpec'lerine çevirir. load_brackets() kademe
tablosunu paketin içindeki brackets.json'dan okur.

description: Binance'in parite kurallarını katalogun modeline çevirir.
"""

# NOTE: Kademe tablosu exchangeInfo'da yok, imzalı leverageBracket ucundan
# gelir ve anahtar ister. Anahtar 8. aşamaya kadar sunucuda olmayacağı için
# tablo bir kez alınıp brackets.json'a yazıldı, alındığı an dosyanın time
# alanındadır. Binance kademeleri seyrek değiştirir, bu hesabın büyüklükleri
# zaten hep ilk kademede kalır.
#
# TODO: Canlı broker anahtarla çalışmaya başlayınca kademe tablosu da açılışta
# leverageBracket ucundan okunmalı, brackets.json o zaman kalkar.

import json
from pathlib import Path

from ...domain.catalog import SLIPPAGE, SYMBOLS
from ...domain.models import Bracket, SymbolSpec

BRACKETS_FILE = Path(__file__).with_name("brackets.json")


def load_brackets(path: Path = BRACKETS_FILE) -> dict[str, list[dict]]:
    return json.loads(path.read_text())["brackets"]


def _bracket(b: dict) -> Bracket:
    return Bracket(float(b["notionalCap"]), int(b["initialLeverage"]), float(b["maintMarginRatio"]), float(b["cum"]))


def specs_from(info: dict, brackets: dict[str, list[dict]]) -> tuple[SymbolSpec, ...]:
    """Masanın paritelerinin kurallarını SYMBOLS sırasıyla döndürür.

    Args:
        info: exchangeInfo cevabı.
        brackets: Sembolden Binance'in kademe listesine.

    Returns:
        Her parite için bir SymbolSpec.

    Raises:
        ValueError: Bir parite Binance'te yoksa, işleme kapalıysa ya da kademesi bilinmiyorsa.
    """
    found = {s["symbol"]: s for s in info["symbols"]}
    out = []
    for symbol, note in SYMBOLS.items():
        s = found.get(symbol)
        if s is None or s["status"] != "TRADING":
            raise ValueError(f"{symbol} Binance'te işleme açık değil.")
        if symbol not in brackets:
            raise ValueError(f"{symbol} için kaldıraç kademesi yok, brackets.json yenilenmeli.")
        f = {x["filterType"]: x for x in s["filters"]}
        out.append(SymbolSpec(
            symbol=symbol, name=f"{s['baseAsset']}/{s['quoteAsset']}",
            tick=float(f["PRICE_FILTER"]["tickSize"]), qty_step=float(f["LOT_SIZE"]["stepSize"]),
            min_notional=float(f["MIN_NOTIONAL"]["notional"]), max_market_qty=float(f["MARKET_LOT_SIZE"]["maxQty"]),
            max_qty=float(f["LOT_SIZE"]["maxQty"]),
            price_up=float(f["PERCENT_PRICE"]["multiplierUp"]), price_down=float(f["PERCENT_PRICE"]["multiplierDown"]),
            max_orders=int(f["MAX_NUM_ORDERS"]["limit"]),
            brackets=tuple(sorted((_bracket(b) for b in brackets[symbol]), key=lambda b: b.cap)),
            slippage=SLIPPAGE, liq_fee=float(s["liquidationFee"]), note=note))
    return tuple(out)
