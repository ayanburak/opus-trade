"""
file: test_binance_specs.py
importable: yok

Parite kurallarının Binance'ten okunmasının testleri. Bir alan yanlış
çevrilirse kâğıt hesap canlının reddedeceği emri kabul eder ya da tersini
yapar ve bu hiçbir hata vermeden olur. Testler çeviriyi, eksik paritede
açılışın durmasını ve Binance'e ulaşılamayınca son kopyaya dönülmesini
korur. Ağa çıkmaz.

description: market.binance.specs ve çekirdeğin load_catalog'u için birim testleri.
"""

import asyncio
import json
from pathlib import Path

import pytest

from opus_trade.app.core import load_catalog
from opus_trade.market.binance.specs import load_brackets, specs_from
from opus_trade.market.source import MarketDataError

INFO = json.loads((Path(__file__).parents[2] / "fixtures" / "exchange_info.json").read_text())


class Rest:
    def __init__(self, info: dict | None) -> None:
        self.info = info

    async def exchange_info(self) -> dict:
        if self.info is None:
            raise MarketDataError("Binance'e ulaşılamadı.")
        return self.info


def test_binance_filters_become_the_spec():
    """Adımlar, sınırlar ve kademeler Binance'teki alanlarından gelir."""
    btc = next(s for s in specs_from(INFO, load_brackets()) if s.symbol == "BTCUSDT")
    assert (btc.tick, btc.qty_step, btc.min_notional, btc.max_market_qty) == (0.1, 0.001, 50, 120)
    assert (btc.price_up, btc.price_down, btc.max_orders, btc.name) == (1.03, 0.97, 200, "BTC/USDT")
    assert btc.brackets[0].max_leverage == 150 and btc.brackets[0].mm_rate == 0.004
    assert [b.cap for b in btc.brackets] == sorted(b.cap for b in btc.brackets)


def test_missing_symbol_or_bracket_stops_the_start():
    """Masadaki bir parite Binance'te yoksa ya da kademesi bilinmiyorsa çekirdek yanlış kurallarla açılmamalı."""
    info = {"symbols": [s for s in INFO["symbols"] if s["symbol"] != "ZECUSDT"]}
    with pytest.raises(ValueError, match="ZECUSDT Binance'te işleme açık değil"):
        specs_from(info, load_brackets())
    brackets = {k: v for k, v in load_brackets().items() if k != "XAUUSDT"}
    with pytest.raises(ValueError, match="XAUUSDT için kaldıraç kademesi yok"):
        specs_from(INFO, brackets)


def test_catalog_falls_back_to_the_last_copy(tmp_path):
    """Binance'e ulaşılamayan açılışta son okunan kopya kullanılır. Kopya da yoksa çekirdek açılmaz."""
    saved = tmp_path / "exchange_info.json"
    assert "SOLUSDT" in asyncio.run(load_catalog(Rest(INFO), saved))
    assert asyncio.run(load_catalog(Rest(None), saved)).get("SOLUSDT").tick == 0.01
    saved.unlink()
    with pytest.raises(MarketDataError):
        asyncio.run(load_catalog(Rest(None), saved))
