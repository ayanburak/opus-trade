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
import hashlib
import hmac

import httpx

from opus_trade.market.binance import refresh_brackets
from opus_trade.market.binance.specs import load_brackets, specs_from
from opus_trade.market.models import DailyStats
from opus_trade.market.source import MarketDataError

INFO = json.loads((Path(__file__).parents[2] / "fixtures" / "exchange_info.json").read_text())


class Rest:
    def __init__(self, info: dict | None) -> None:
        self.info = info

    async def exchange_info(self) -> dict:
        if self.info is None:
            raise MarketDataError("Binance'e ulaşılamadı.")
        return self.info

    async def all_daily_stats(self) -> list:
        return [DailyStats("AAAUSDT", 1, 2, 1, 60e6)]


def test_binance_filters_become_the_spec():
    """Adımlar, sınırlar ve kademeler Binance'teki alanlarından gelir."""
    btc = next(s for s in specs_from(INFO, load_brackets()) if s.symbol == "BTCUSDT")
    assert (btc.tick, btc.qty_step, btc.min_notional, btc.max_market_qty) == (0.1, 0.001, 50, 120)
    assert (btc.price_up, btc.price_down, btc.max_orders, btc.name) == (1.03, 0.97, 200, "BTC/USDT")
    assert (btc.max_qty, btc.liq_fee) == (1000, 0.0125)
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


def _like(base: str, symbol: str, **extra) -> dict:
    s = next(x for x in INFO["symbols"] if x["symbol"] == base)
    return {**s, "symbol": symbol, "baseAsset": symbol.removesuffix("USDT"), **extra}


def test_other_usdt_perpetuals_join_when_their_bracket_is_known():
    """İzleme listesi bütün piyasadan seçer, liste ancak katalogdaki paritelerden kurulabilir. Kademesi bilinmeyen
    parite katalogda olmamalı, yanlış kademe canlının reddedeceği kaldıracı kabul ettirirdi. Teslim tarihli ve
    USDT dışındaki pariteler hiç girmemeli."""
    extra = [_like("SOLUSDT", "ZZZUSDT", contractType="PERPETUAL"), _like("SOLUSDT", "AAAUSDT"),
             _like("SOLUSDT", "NOBRUSDT"), _like("SOLUSDT", "BTCUSDT_261225", contractType="CURRENT_QUARTER"),
             _like("SOLUSDT", "ETHUSDC", quoteAsset="USDC"), _like("SOLUSDT", "OFFUSDT", status="SETTLING")]
    brackets = load_brackets()
    for k in ("ZZZUSDT", "AAAUSDT", "BTCUSDT_261225", "ETHUSDC", "OFFUSDT"):
        brackets[k] = brackets["SOLUSDT"]
    specs = specs_from({"symbols": INFO["symbols"] + extra}, brackets)
    names = [s.symbol for s in specs]
    assert names[:6] == ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "XAUUSDT", "ZECUSDT"]
    assert names[6:] == ["AAAUSDT", "ZZZUSDT"]
    assert specs[6].name == "AAA/USDT" and specs[6].note == ""


def test_slippage_follows_volume_outside_the_desk():
    """İğnede küçük coin'in kayması büyüktür. Sabit yüzde 0,02 küçük hesabı olduğundan iyi gösterirdi. Masanın
    paritelerinin kayması değişirse bugünkü profillerin sonuçları kırılır, hacmi bilinmeyen parite en kötü kademeyi
    almalı."""
    extra = [_like("SOLUSDT", s) for s in ("AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT", "EEEUSDT")]
    brackets = load_brackets()
    for e in extra:
        brackets[e["symbol"]] = brackets["SOLUSDT"]
    volumes = {"AAAUSDT": 600e6, "BBBUSDT": 50e6, "CCCUSDT": 6e6, "DDDUSDT": 4e6, "XAUUSDT": 1e3}
    specs = {s.symbol: s.slippage for s in specs_from({"symbols": INFO["symbols"] + extra}, brackets, volumes)}
    assert [specs[k] for k in ("AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT", "EEEUSDT")] == [
        0.0002, 0.0005, 0.001, 0.002, 0.002]
    assert specs["XAUUSDT"] == 0.0002


def test_desk_symbol_of_another_contract_type_survives_the_saved_copy(tmp_path):
    """Altın Binance'te hisse ve emtia perp'lerinin türündendir. Kayıtlı kopyanın süzgeci onu düşürseydi çekirdek
    masanın paritesi eksik diye hiç açılmazdı. 25.11'de yerel denemede görüldü."""
    info = {"symbols": [{**s, "contractType": "TRADIFI_PERPETUAL"} if s["symbol"] == "XAUUSDT" else
                        {**s, "contractType": "PERPETUAL"} for s in INFO["symbols"]]
            + [_like("SOLUSDT", "AAPLUSDT", contractType="TRADIFI_PERPETUAL")]}
    catalog = asyncio.run(load_catalog(Rest(info), tmp_path / "exchange_info.json"))
    assert "XAUUSDT" in catalog and "AAPLUSDT" not in catalog


def test_catalog_falls_back_to_the_last_copy(tmp_path):
    """Binance'e ulaşılamayan açılışta son okunan kopya kullanılır. Kopya da yoksa çekirdek açılmaz."""
    saved = tmp_path / "exchange_info.json"
    assert "SOLUSDT" in asyncio.run(load_catalog(Rest(INFO), saved))
    assert json.loads(saved.read_text())["volumes"] == {"AAAUSDT": 60e6}
    assert asyncio.run(load_catalog(Rest(None), saved)).get("SOLUSDT").tick == 0.01
    saved.unlink()
    with pytest.raises(MarketDataError):
        asyncio.run(load_catalog(Rest(None), saved))


def test_bracket_refresh_signs_the_request_and_writes_every_symbol(tmp_path):
    """leverageBracket imzalı uçtur. İmza yanlışsa tablo hiç alınamaz, alınan tablo katalogun okuduğu biçimde
    yazılmazsa açılışta yeni pariteler yine görünmez."""
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["key"] = req.headers["X-MBX-APIKEY"]
        seen["query"] = req.url.query.decode()
        return httpx.Response(200, json=[{"symbol": "ZZZUSDT", "brackets": [{"bracket": 1}]},
                                         {"symbol": "AAAUSDT", "brackets": [{"bracket": 1}]}])

    with httpx.Client(base_url="https://x", transport=httpx.MockTransport(handler)) as c:
        found = refresh_brackets.fetch(c, "anahtar", "gizli", 1_790_000_000_000)
    signature = hmac.new(b"gizli", b"timestamp=1790000000000", hashlib.sha256).hexdigest()
    assert seen == {"key": "anahtar", "query": f"timestamp=1790000000000&signature={signature}"}
    path = tmp_path / "brackets.json"
    refresh_brackets.write(found, path, 1_790_000_000)
    assert list(load_brackets(path)) == ["AAAUSDT", "ZZZUSDT"]
