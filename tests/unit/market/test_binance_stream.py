"""
file: test_binance_stream.py
importable: yok

BinanceStream'in testleri. Yerelde açılan sahte bir websocket sunucusu
Binance'in birleşik akışını taklit eder. Testler mesaj çevirisini, çalışırken
abonelik değişikliğini ve donan bağlantıdan sonra yeniden bağlanmayı korur.

description: market.binance.stream için birim testleri.
"""

import asyncio
import json

import websockets

from opus_trade.domain.models import Side
from opus_trade.market.binance.stream import BinanceStream
from opus_trade.market.stream import StreamListener

KLINE = {"stream": "btcusdt@kline_1m", "data": {"e": "kline", "s": "BTCUSDT", "k": {
    "t": 1_790_000_040_000, "s": "BTCUSDT", "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "10", "q": "15",
    "n": 7, "V": "4", "x": True}}}
MARK = {"stream": "btcusdt@markPrice@1s", "data": {"e": "markPriceUpdate", "E": 1_790_000_045_000, "s": "BTCUSDT",
                                                   "p": "1.4", "i": "1.39", "r": "0.0001", "T": 1_790_006_400_000}}
LIQ = {"stream": "!forceOrder@arr", "data": {"e": "forceOrder", "o": {
    "s": "ETHUSDT", "S": "SELL", "ap": "2000", "z": "3", "l": "1", "T": 1_790_000_001_000}}}


class Recorder(StreamListener):
    def __init__(self):
        self.connects, self.candles, self.liquidations, self.marks = 0, [], [], []

    async def on_connect(self):
        self.connects += 1

    async def on_candle(self, candle, closed):
        self.candles.append((candle, closed))

    async def on_mark(self, symbol, mark, time):
        self.marks.append((symbol, mark, time))

    async def on_liquidation(self, liq):
        self.liquidations.append(liq)


# Sunucu her bağlantıda adresi ve gelen mesajları kaydeder. send listesi
# bağlantı açılınca gönderilir, sonra sunucu susar.
async def _serve(send: list[dict], seen: dict):
    async def handler(ws):
        seen["paths"].append(ws.request.path)
        for m in send:
            await ws.send(json.dumps(m))
        async for msg in ws:
            seen["received"].append(json.loads(msg))
    return await websockets.serve(handler, "127.0.0.1", 0)


async def _until(cond, timeout=3.0):
    for _ in range(int(timeout / 0.02)):
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("koşul zamanında gerçekleşmedi")


def test_messages_are_translated_and_ack_is_ignored():
    """SELL likidasyonu bir long'un kapanmasıdır, miktar toplam gerçekleşendir. Ters çeviri dalganın yönünü tersine çevirirdi."""
    async def main():
        seen = {"paths": [], "received": []}
        server = await _serve([{"result": None, "id": 1}, KLINE, MARK, LIQ], seen)
        stream, rec = BinanceStream(url=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/stream"), Recorder()
        await stream.watch({"BTCUSDT"})
        task = asyncio.create_task(stream.run(rec))
        await _until(lambda: rec.candles and rec.marks and rec.liquidations)
        task.cancel()
        server.close()
        return seen, rec

    seen, rec = asyncio.run(main())
    assert seen["paths"][0] == "/stream?streams=!forceOrder@arr/btcusdt@kline_1m/btcusdt@markPrice@1s"
    assert rec.marks == [("BTCUSDT", 1.4, 1_790_000_045)]
    candle, closed = rec.candles[0]
    assert candle.symbol == "BTCUSDT" and candle.time == 1_790_000_040 and closed
    liq = rec.liquidations[0]
    assert liq.side is Side.LONG and liq.qty == 3 and liq.usd == 6000


def test_watch_while_connected_subscribes_the_difference():
    """Panelden yeni bir profil açıldığında paritesi programı yeniden başlatmadan dinlenmeye başlamalı."""
    async def main():
        seen = {"paths": [], "received": []}
        server = await _serve([], seen)
        stream, rec = BinanceStream(url=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/stream"), Recorder()
        await stream.watch({"BTCUSDT"})
        task = asyncio.create_task(stream.run(rec))
        await _until(lambda: rec.connects == 1)
        await stream.watch({"ETHUSDT"})
        await _until(lambda: len(seen["received"]) == 2)
        task.cancel()
        server.close()
        return seen

    received = asyncio.run(main())["received"]
    assert [(m["method"], m["params"]) for m in received] == [
        ("SUBSCRIBE", ["ethusdt@kline_1m", "ethusdt@markPrice@1s"]),
        ("UNSUBSCRIBE", ["btcusdt@kline_1m", "btcusdt@markPrice@1s"])]


def test_silent_connection_is_replaced():
    """Açık görünüp veri göndermeyen bağlantı hata vermez. Fark edilmezse fiyatlar sessizce donar."""
    async def main():
        seen = {"paths": [], "received": []}
        server = await _serve([], seen)
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/stream"
        stream, rec = BinanceStream(url=url, silence_s=0.2), Recorder()
        task = asyncio.create_task(stream.run(rec))
        await _until(lambda: rec.connects >= 2, timeout=5)
        task.cancel()
        server.close()
        return rec

    assert asyncio.run(main()).connects >= 2


def test_watch_during_connect_is_not_lost():
    """Bağlantı kurulurken eklenen parite kaybolmamalı. Kaybolursa yeni profilin TP, SL ve emirleri yeniden başlatmaya kadar işlemez."""
    async def main():
        seen = {"paths": [], "received": []}

        async def slow(connection, request):
            await asyncio.sleep(0.3)

        async def handler(ws):
            seen["paths"].append(ws.request.path)
            async for msg in ws:
                seen["received"].append(json.loads(msg))

        server = await websockets.serve(handler, "127.0.0.1", 0, process_request=slow)
        stream, rec = BinanceStream(url=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/stream"), Recorder()
        task = asyncio.create_task(stream.run(rec))
        await asyncio.sleep(0.1)
        await stream.watch({"ETHUSDT"})
        await _until(lambda: rec.connects == 1)
        await asyncio.sleep(0.2)
        task.cancel()
        server.close()
        return seen

    seen = asyncio.run(main())
    asked = "ethusdt@kline_1m" in seen["paths"][0] or any(
        m["method"] == "SUBSCRIBE" and "ethusdt@kline_1m" in m["params"] for m in seen["received"])
    assert asked, seen
