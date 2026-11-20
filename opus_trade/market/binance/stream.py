"""
file: stream.py
importable: STREAM_URL, BinanceStream

BinanceStream, MarketStream'in Binance vadeli birleşik akışıyla yazılmış
uygulamasıdır. Tek bağlantıdan bütün piyasanın likidasyonlarını ve watch()
ile verilen paritelerin dakikalık mumlarını dinler. Bağlantı koparsa ya da
uzun süre veri gelmezse yeniden bağlanır.

description: Binance vadeli piyasasının canlı mum ve likidasyon akışı.
"""

# NOTE: Eski /ws/ yolu bağlantıyı kabul ediyor ama hiç veri göndermiyor,
# Binance piyasa akışlarını /market/ yolu altına taşıdı. Bağlantı açıkken
# SUBSCRIBE ve UNSUBSCRIBE mesajlarıyla akış eklenip çıkarılabiliyor,
# 20.11.2026'da denendi. Bu mesajların cevabı data alanı taşımaz ve atlanır.
#
# NOTE: Bağlantı her koptuğunda bekleme ikiye katlanır ve bir dakikada
# sabitlenir, başarılı bir mesaj gelince başa döner. on_connect beklenirken
# gelen mesajlar bağlantının tamponunda bekler, kaybolmaz.
#
# XXX: Binance tek bağlantıda en fazla iki yüz akışa izin verir. Bugün on
# pariteden az dinleniyor, sınır yaklaşırsa bağlantı bölünmelidir.

import asyncio
import json
import logging

import websockets

from ...domain.models import Side
from ..models import Candle, Liquidation
from ..stream import MarketStream, StreamListener

STREAM_URL = "wss://fstream.binance.com/market/stream"

# Bu kadar saniye hiç mesaj gelmezse bağlantı ölü sayılır. Bağlantı açık
# görünüp veri gelmeyebiliyor, bu durumda hata oluşmadığı için akış sessizce
# donar. Likidasyonlar bütün piyasadan geldiği için normalde birkaç saniyede
# bir mesaj olur.
SILENCE_S = 30

_LIQUIDATIONS = "!forceOrder@arr"

log = logging.getLogger(__name__)


def _kline(d: dict) -> tuple[Candle, bool]:
    k = d["k"]
    candle = Candle(symbol=k["s"], time=k["t"] // 1000, open=float(k["o"]), high=float(k["h"]), low=float(k["l"]),
                    close=float(k["c"]), volume=float(k["v"]), quote_volume=float(k["q"]), trades=int(k["n"]),
                    taker_buy_volume=float(k["V"]))
    return candle, bool(k["x"])


# Akıştaki SELL emri bir long'un, BUY emri bir short'un zorla kapatılmasıdır.
# Fiyat gerçekleşen ortalama fiyat, miktar toplam gerçekleşen miktardır.
def _force_order(d: dict) -> Liquidation:
    o = d["o"]
    return Liquidation(time=o["T"] // 1000, symbol=o["s"], side=Side.LONG if o["S"] == "SELL" else Side.SHORT,
                       price=float(o["ap"]), qty=float(o["z"]))


def _kline_stream(symbol: str) -> str:
    return f"{symbol.lower()}@kline_1m"


# İstenen parite kümesi _symbols'ta, açık bağlantının dinlediği küme
# _subscribed'da durur. Bağlantı adresi açılırken o anki istenen kümeden
# kurulur. Bağlantı kurulurken watch() çağrılırsa küme değişir ama gönderecek
# bağlantı henüz yoktur, bu yüzden bağlantı açılınca _sync() farkı abonelik
# mesajıyla tamamlar. Yoksa yeni paritenin mumları hiç gelmezdi.
class BinanceStream(MarketStream):
    def __init__(self, url: str = STREAM_URL, silence_s: float = SILENCE_S) -> None:
        self._url = url
        self._silence = silence_s
        self._symbols: set[str] = set()
        self._subscribed: set[str] = set()
        self._ws = None
        self._next_id = 0

    async def watch(self, symbols: set[str]) -> None:
        self._symbols = set(symbols)
        await self._sync()

    async def _sync(self) -> None:
        if self._ws is None:
            return
        added, removed = self._symbols - self._subscribed, self._subscribed - self._symbols
        self._subscribed = set(self._symbols)
        for method, items in (("SUBSCRIBE", added), ("UNSUBSCRIBE", removed)):
            if items:
                self._next_id += 1
                await self._ws.send(json.dumps({"method": method, "params": sorted(map(_kline_stream, items)),
                                                "id": self._next_id}))

    def _address(self, symbols: set[str]) -> str:
        return f"{self._url}?streams=" + "/".join([_LIQUIDATIONS] + sorted(map(_kline_stream, symbols)))

    async def run(self, listener: StreamListener) -> None:
        wait = 1
        while True:
            try:
                asked = set(self._symbols)
                async with websockets.connect(self._address(asked), ping_interval=20) as ws:
                    self._ws, self._subscribed = ws, asked
                    await self._sync()
                    log.info("Binance canlı akışına bağlanıldı.")
                    await listener.on_connect()
                    while True:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), self._silence)
                        except asyncio.TimeoutError:
                            raise ConnectionError(f"{self._silence:g} saniyedir veri gelmiyor") from None
                        wait = 1
                        await self._dispatch(json.loads(msg), listener)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Canlı akış koptu: %s. %s saniye sonra yeniden bağlanılacak.", e, wait)
                await asyncio.sleep(wait)
                wait = min(wait * 2, 60)
            finally:
                self._ws = None

    # Abonelik cevapları ve tanınmayan mesajlar data taşımaz ya da başka bir
    # olay tipidir, sessizce atlanır.
    @staticmethod
    async def _dispatch(msg: dict, listener: StreamListener) -> None:
        data = msg.get("data")
        if not data:
            return
        if data.get("e") == "forceOrder":
            await listener.on_liquidation(_force_order(data))
        elif data.get("e") == "kline":
            await listener.on_candle(*_kline(data))
