"""
file: test_binance_rest.py
importable: yok

BinanceRest'in testleri. Ağa çıkmaz, httpx'in sahte taşıyıcısı Binance'in
cevap biçimini ve bilinen tuhaflıklarını taklit eder. Testler sayfalamanın
veri kaçırmamasını, alan çevirilerini ve hata dönüşümünü korur.

description: market.binance.rest için birim testleri.
"""

import asyncio

import httpx
import pytest

from opus_trade.market.binance.rest import BASE_URL, BinanceRest
from opus_trade.market.source import MarketDataError

T0 = 1_790_000_000 // 60 * 60


def run(handler, job):
    async def main():
        async with httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(handler)) as c:
            return await job(BinanceRest(c))
    return asyncio.run(main())


def _kline(t: int) -> list:
    return [t * 1000, "1", "2", "0.5", "1.5", "10", t * 1000 + 59999, "15", 7, "4", "6", "0"]


# Binance klines ucunu taklit eder: startTime'dan itibaren, endTime'ı
# geçmeden en fazla limit kadar mum verir.
def _klines_handler(available: list[int], calls: list):
    def handler(req: httpx.Request) -> httpx.Response:
        q = req.url.params
        calls.append(dict(q))
        lo, hi, limit = int(q["startTime"]) // 1000, int(q["endTime"]) // 1000, int(q["limit"])
        return httpx.Response(200, json=[_kline(t) for t in available if lo <= t <= hi][:limit])
    return handler


def test_candles_with_range_fetch_every_minute_across_pages():
    """Kapalı kalınan süre yeniden işlenirken tek bir dakika eksik kalırsa o dakikadaki SL ya da TP kaçar."""
    minutes = [T0 + i * 60 for i in range(3100)]
    calls = []
    out = run(_klines_handler(minutes, calls), lambda b: b.candles("BTCUSDT", "1m", start=T0, end=T0 + 3100 * 60))
    assert [c.time for c in out] == minutes
    assert len(calls) == 3


def test_mark_candles_come_from_the_mark_endpoint_across_pages():
    """Kapalı geçen dakikalarda SL mark mumlarıyla işlenir. Yanlış uçtan gelen son fiyat mumu SL'yi iğneyle
    çalıştırır, eksik sayfa ise bir dakikanın SL'sini kaçırır."""
    minutes = [T0 + i * 60 for i in range(1600)]
    calls, paths = [], set()
    inner = _klines_handler(minutes, calls)

    def handler(req: httpx.Request) -> httpx.Response:
        paths.add(req.url.path)
        return inner(req)
    out = run(handler, lambda b: b.mark_candles("BTCUSDT", start=T0, end=T0 + 1600 * 60))
    assert [c.time for c in out] == minutes
    assert paths == {"/fapi/v1/markPriceKlines"} and len(calls) == 2 and calls[0]["interval"] == "1m"


def test_candle_fields_keep_quote_volume_and_trade_count():
    """Toplayıcı ve araştırma dolar hacmini ve işlem sayısını kullanır. Sütunlar kayarsa veri sessizce bozulur."""
    out = run(_klines_handler([T0], []), lambda b: b.candles("BTCUSDT", "1m", start=T0, end=T0 + 60))
    c = out[0]
    assert (c.open, c.high, c.low, c.close, c.volume) == (1, 2, 0.5, 1.5, 10)
    assert (c.quote_volume, c.trades, c.taker_buy_volume) == (15, 7, 4)


def test_stats_history_sends_both_bounds_on_every_page():
    """Binance istatistik uçları endTime'sız startTime'ı yok sayıp en son kaydı verir. Sayfalama bunu bilmezse geriye dönük doldurma hep aynı sayfayı alır."""
    points = [T0 + i * 300 for i in range(1200)]

    def handler(req: httpx.Request) -> httpx.Response:
        q = req.url.params
        limit = int(q["limit"])
        if "endTime" not in q:
            chosen = points[-limit:]
        else:
            lo, hi = int(q["startTime"]) // 1000, int(q["endTime"]) // 1000
            chosen = [t for t in points if lo <= t <= hi][:limit]
        return httpx.Response(200, json=[{"symbol": "BTCUSDT", "sumOpenInterest": "1", "sumOpenInterestValue": "2",
                                          "timestamp": t * 1000} for t in chosen])

    out = run(handler, lambda b: b.open_interest_history("BTCUSDT", "5m", start=T0, end=T0 + 1200 * 300))
    assert [x.time for x in out] == points


def test_premium_reads_estimated_funding_for_one_or_all_symbols():
    """lastFundingRate tahmini orandır. Tek parite sözlük, bütün piyasa liste döner, ikisi de aynı biçime çevrilmeli."""
    row = {"symbol": "BTCUSDT", "markPrice": "100", "indexPrice": "99", "lastFundingRate": "0.0001",
           "nextFundingTime": 1_790_028_800_000, "time": 1_790_000_000_000}

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=row if "symbol" in req.url.params else [row, {**row, "symbol": "ETHUSDT"}])

    one = run(handler, lambda b: b.premium("BTCUSDT"))
    every = run(handler, lambda b: b.premium())
    assert one[0].est_funding == 0.0001 and one[0].next_funding == 1_790_028_800
    assert [p.symbol for p in every] == ["BTCUSDT", "ETHUSDT"]


def test_all_daily_stats_come_in_one_request_without_symbol():
    """İzleme listesi bütün piyasayı saat başı sıralar. Parite başına istek atılsaydı beş yüz istek Binance'in ağırlık
    sınırını doldururdu."""
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.url.path, dict(req.url.params)))
        row = {"priceChangePercent": "-12.5", "highPrice": "2", "lowPrice": "1", "quoteVolume": "3000000"}
        return httpx.Response(200, json=[{"symbol": "AUSDT", **row}, {"symbol": "BUSDT", **row}])

    out = run(handler, lambda b: b.all_daily_stats())
    assert calls == [("/fapi/v1/ticker/24hr", {})]
    assert [(d.symbol, d.change_pct, d.high, d.low, d.quote_volume) for d in out] == [
        ("AUSDT", -12.5, 2, 1, 3_000_000), ("BUSDT", -12.5, 2, 1, 3_000_000)]


def test_http_error_becomes_market_data_error_with_binance_reason():
    """Toplayıcı geçici hatayı programın kendi hatasından ayırabilmeli, nedeni de günlükte görünmeli."""
    handler = lambda req: httpx.Response(400, json={"code": -1130, "msg": "parameter 'startTime' is invalid."})
    with pytest.raises(MarketDataError, match="startTime"):
        run(handler, lambda b: b.open_interest("BTCUSDT"))


def test_range_without_end_is_refused():
    """Bitişsiz bir aralık sonsuz sayfalamaya dönebilir, bu yüzden açıkça reddedilir."""
    with pytest.raises(ValueError):
        run(lambda req: httpx.Response(200, json=[]), lambda b: b.candles("BTCUSDT", "1m", start=T0))
