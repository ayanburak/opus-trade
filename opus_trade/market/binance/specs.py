"""
file: specs.py
importable: specs_from, tradable, load_brackets, BRACKETS_FILE

specs_from() Binance'in exchangeInfo cevabını ve kaldıraç kademe tablosunu
katalogun SymbolSpec'lerine çevirir. Katalog masanın sabit paritelerini ve
işleme açık bütün USDT perp'lerini taşır. tradable() bir paritenin bu
ikinci gruba girip girmediğini söyler. load_brackets() kademe tablosunu
paketin içindeki brackets.json'dan okur.

description: Binance'in parite kurallarını katalogun modeline çevirir.
"""

# NOTE: Kademe tablosu exchangeInfo'da yok, imzalı leverageBracket ucundan
# gelir ve anahtar ister. Tablo bir kez alınıp brackets.json'a yazıldı, alındığı
# an dosyanın time alanındadır. Yenilemek için refresh_brackets komutu vardır.
# Binance kademeleri seyrek değiştirir, bu hesabın büyüklükleri zaten hep ilk
# kademede kalır.
#
# NOTE: Masanın sabit paritelerinden biri eksikse açılış durur, çünkü o
# paritelerde çalışan profiller vardır. Diğer pariteler kademesi bilinmiyorsa
# katalogda yer almaz. Yanlış kademeyle kâğıt hesap canlının reddedeceği
# kaldıracı kabul ederdi, paritenin hiç olmaması daha güvenlidir.
#
# TODO: Canlı broker anahtarla çalışmaya başlayınca kademe tablosu da açılışta
# leverageBracket ucundan okunmalı, brackets.json o zaman kalkar.

import json
import logging
from pathlib import Path

from ...domain.catalog import SLIPPAGE, SYMBOLS, slippage_for
from ...domain.models import Bracket, SymbolSpec

BRACKETS_FILE = Path(__file__).with_name("brackets.json")

log = logging.getLogger(__name__)


def load_brackets(path: Path = BRACKETS_FILE) -> dict[str, list[dict]]:
    return json.loads(path.read_text())["brackets"]


def _bracket(b: dict) -> Bracket:
    return Bracket(float(b["notionalCap"]), int(b["initialLeverage"]), float(b["maintMarginRatio"]), float(b["cum"]))


# Sürekli vadeli ve USDT ile ödenen, işleme açık kripto paritesi. Teslim
# tarihli vadeliler, başka para birimli pariteler ve Binance'in hisse ve emtia
# perp'leri, yani TRADIFI_PERPETUAL türü, katalogun dışındadır. Masanın sabit
# pariteleri türünden bağımsız kataloga girer, altın bu türdendir.
def tradable(s: dict) -> bool:
    return (s["status"] == "TRADING" and s.get("quoteAsset") == "USDT"
            and s.get("contractType", "PERPETUAL") == "PERPETUAL")


def _spec(s: dict, brackets: list[dict], note: str, slippage: float) -> SymbolSpec:
    f = {x["filterType"]: x for x in s["filters"]}
    return SymbolSpec(
        symbol=s["symbol"], name=f"{s['baseAsset']}/{s['quoteAsset']}",
        tick=float(f["PRICE_FILTER"]["tickSize"]), qty_step=float(f["LOT_SIZE"]["stepSize"]),
        min_notional=float(f["MIN_NOTIONAL"]["notional"]), max_market_qty=float(f["MARKET_LOT_SIZE"]["maxQty"]),
        max_qty=float(f["LOT_SIZE"]["maxQty"]),
        price_up=float(f["PERCENT_PRICE"]["multiplierUp"]), price_down=float(f["PERCENT_PRICE"]["multiplierDown"]),
        max_orders=int(f["MAX_NUM_ORDERS"]["limit"]),
        brackets=tuple(sorted((_bracket(b) for b in brackets), key=lambda b: b.cap)),
        slippage=slippage, liq_fee=float(s["liquidationFee"]), note=note)


def specs_from(info: dict, brackets: dict[str, list[dict]],
               volumes: dict[str, float] | None = None) -> tuple[SymbolSpec, ...]:
    """Katalogun kurallarını döndürür: önce masanın pariteleri SYMBOLS sırasıyla, sonra diğerleri adlarına göre.

    Args:
        info: exchangeInfo cevabı.
        brackets: Sembolden Binance'in kademe listesine.
        volumes: Sembolden yirmi dört saatlik dolar hacmine, masanın dışındaki paritelerin kayması için.

    Returns:
        Her parite için bir SymbolSpec.

    Raises:
        ValueError: Masanın bir paritesi Binance'te yoksa, işleme kapalıysa ya da kademesi bilinmiyorsa.
    """
    found = {s["symbol"]: s for s in info["symbols"]}
    out = []
    for symbol, note in SYMBOLS.items():
        s = found.get(symbol)
        if s is None or s["status"] != "TRADING":
            raise ValueError(f"{symbol} Binance'te işleme açık değil.")
        if symbol not in brackets:
            raise ValueError(f"{symbol} için kaldıraç kademesi yok, brackets.json yenilenmeli.")
        out.append(_spec(s, brackets[symbol], note, SLIPPAGE))
    others = sorted(k for k, s in found.items() if k not in SYMBOLS and tradable(s))
    unknown = [k for k in others if k not in brackets]
    if unknown:
        log.warning("%d paritenin kaldıraç kademesi bilinmiyor, katalogda yer almıyorlar. brackets.json "
                    "yenilenmeli.", len(unknown))
    volumes = volumes or {}
    out += [_spec(found[k], brackets[k], "", slippage_for(volumes.get(k))) for k in others if k in brackets]
    return tuple(out)
