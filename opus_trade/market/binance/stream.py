"""
file: stream.py
importable: STREAM_URL, LIQUIDATIONS, ALL_MARKS, MAX_STREAMS, BinanceStream

BinanceStream, MarketStream'in Binance vadeli birleşik akışıyla yazılmış
uygulamasıdır. fixed ile verilen pariteden bağımsız akışları, örneğin bütün
piyasanın likidasyonlarını ve mark fiyatlarını, ve watch() ile istenen
paritelerin mum, mark ve tekil işlem akışlarını dinler. Akış sayısı bir
bağlantının sınırını aşarsa yeni bağlantı açar. Bir bağlantı koparsa ya da
uzun süre veri gelmezse yalnızca o bağlantı yeniden kurulur.

description: Binance vadeli piyasasının canlı mum, mark fiyatı, tekil işlem ve likidasyon akışı.
"""

# NOTE: Eski /ws/ yolu bağlantıyı kabul ediyor ama hiç veri göndermiyor,
# Binance piyasa akışlarını /market/ yolu altına taşıdı. aggTrade ve bütün
# piyasanın mark akışı da bu yoldan geliyor, /public/ ve kök yol veri
# göndermiyor, 24.11.2026'da denendi. Bağlantı açıkken SUBSCRIBE ve
# UNSUBSCRIBE mesajlarıyla akış eklenip çıkarılabiliyor. Bu mesajların cevabı
# data alanı taşımaz ve atlanır.
#
# NOTE: Bağlantı her koptuğunda bekleme ikiye katlanır ve bir dakikada
# sabitlenir, başarılı bir mesaj gelince başa döner. on_connect beklenirken
# gelen mesajlar bağlantının tamponunda bekler, kaybolmaz.
#
# NOTE: Binance tek bağlantıda en fazla iki yüz akışa izin verir. Akışlar
# bağlantılara yerleşirken var olanın yeri değişmez, yeni akış yeri olan ilk
# bağlantıya girer, hiçbirinde yer yoksa yeni bağlantı açılır. Böylece
# izleme listesi değişince yalnızca değişen akışlar için abonelik mesajı
# gider, hiçbir bağlantı yeniden kurulmaz. Akışı kalmayan ek bağlantı kapanır,
# yeri sonra yine kullanılır.
#
# XXX: Ek bağlantılarda yalnızca parite akışları vardır, sakin bir paritenin
# akışı uzun süre susabilir. Bu yüzden onların sessizlik süresi ilk
# bağlantınınkinin dört katıdır. İlk bağlantı likidasyonları ve mark
# fiyatlarını taşıdığı için orada sessizlik gerçekten kopukluktur.

import asyncio
import json
import logging
from collections.abc import Collection, Mapping

import websockets

from ...domain.models import Side
from ..models import Candle, Liquidation, Trade
from ..stream import Feed, MarketStream, StreamListener

STREAM_URL = "wss://fstream.binance.com/market/stream"

# Bu kadar saniye hiç mesaj gelmezse bağlantı ölü sayılır. Bağlantı açık
# görünüp veri gelmeyebiliyor, bu durumda hata oluşmadığı için akış sessizce
# donar. Likidasyonlar bütün piyasadan geldiği için normalde birkaç saniyede
# bir mesaj olur.
SILENCE_S = 30

MAX_STREAMS = 200

LIQUIDATIONS = "!forceOrder@arr"
ALL_MARKS = "!markPrice@arr@1s"

_SUFFIX = {Feed.KLINE: "@kline_1m", Feed.MARK: "@markPrice@1s", Feed.TRADE: "@aggTrade"}

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


# m alıcının defterdeki taraf olduğunu söyler. O zaman işlemi başlatan satıcıdır.
def _agg_trade(d: dict) -> Trade:
    return Trade(symbol=d["s"], ms=d["T"], price=float(d["p"]), qty=float(d["q"]), buy=not d["m"])


async def _dispatch(msg: dict, listener: StreamListener) -> None:
    data = msg.get("data")
    if not data:
        return
    # Bütün piyasanın mark akışı her saniye paritelerin listesini gönderir.
    for d in data if isinstance(data, list) else (data,):
        e = d.get("e")
        if e == "aggTrade":
            await listener.on_trade(_agg_trade(d))
        elif e == "markPriceUpdate":
            await listener.on_mark(d["s"], float(d["p"]), d["E"] // 1000)
        elif e == "kline":
            await listener.on_candle(*_kline(d))
        elif e == "forceOrder":
            await listener.on_liquidation(_force_order(d))


# Tek bir websocket bağlantısı. asked bağlantının dinlemesi istenen akışlar,
# subscribed açık bağlantının o an dinledikleridir. Bağlantı adresi açılırken
# o anki istenen kümeden kurulur. Bağlantı kurulurken istek değişirse
# gönderecek bağlantı henüz yoktur, bu yüzden bağlantı açılınca sync() farkı
# abonelik mesajıyla tamamlar. Yoksa yeni paritenin akışı hiç gelmezdi.
class _Connection:
    def __init__(self, fixed: tuple[str, ...]) -> None:
        self.fixed = fixed
        self.asked: set[str] = set()
        self.subscribed: set[str] = set()
        self.ws = None
        self.task: asyncio.Task | None = None
        self._next_id = 0

    @property
    def streams(self) -> list[str]:
        return list(self.fixed) + sorted(self.asked)

    async def sync(self) -> None:
        if self.ws is None:
            return
        added, removed = self.asked - self.subscribed, self.subscribed - self.asked
        self.subscribed = set(self.asked)
        for method, items in (("SUBSCRIBE", added), ("UNSUBSCRIBE", removed)):
            if items:
                self._next_id += 1
                await self.ws.send(json.dumps({"method": method, "params": sorted(items), "id": self._next_id}))

    async def run(self, url: str, silence: float, listener: StreamListener) -> None:
        wait = 1
        while True:
            try:
                asked = set(self.asked)
                address = f"{url}?streams=" + "/".join(list(self.fixed) + sorted(asked))
                async with websockets.connect(address, ping_interval=20) as ws:
                    self.ws, self.subscribed = ws, asked
                    await self.sync()
                    log.info("Binance canlı akışına bağlanıldı, %d akış.", len(self.fixed) + len(self.asked))
                    await listener.on_connect()
                    while True:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), silence)
                        except asyncio.TimeoutError:
                            raise ConnectionError(f"{silence:g} saniyedir veri gelmiyor") from None
                        wait = 1
                        await _dispatch(json.loads(msg), listener)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Canlı akış koptu: %s. %s saniye sonra yeniden bağlanılacak.", e, wait)
                await asyncio.sleep(wait)
                wait = min(wait * 2, 60)
            finally:
                self.ws = None


class BinanceStream(MarketStream):
    """Binance vadeli piyasasının birleşik akışı.

    Args:
        url: Akışın adresi.
        silence_s: Bu kadar saniye mesaj gelmeyen ilk bağlantı yeniden kurulur.
        fixed: Pariteden bağımsız, her zaman dinlenen akışlar. İlk bağlantıda durur.
        max_streams: Bir bağlantının taşıyabileceği en fazla akış.
    """

    def __init__(self, url: str = STREAM_URL, silence_s: float = SILENCE_S,
                 fixed: tuple[str, ...] = (LIQUIDATIONS,), max_streams: int = MAX_STREAMS) -> None:
        self._url = url
        self._silence = silence_s
        self._max = max_streams
        self._conns = [_Connection(tuple(fixed))]
        self._listener: StreamListener | None = None

    async def watch(self, wants: Mapping[str, Collection[Feed]]) -> None:
        names = {f"{s.lower()}{_SUFFIX[Feed(f)]}" for s, feeds in wants.items() for f in feeds}
        for c in self._conns:
            c.asked &= names
        placed = set().union(*(c.asked for c in self._conns))
        for name in sorted(names - placed):
            conn = next((c for c in self._conns if len(c.fixed) + len(c.asked) < self._max), None)
            if conn is None:
                conn = _Connection(())
                self._conns.append(conn)
            conn.asked.add(name)
        for c in self._conns:
            await self._settle(c)

    # Akışı kalmayan ek bağlantı kapanır, akışı olan ama çalışmayan bağlantı
    # akış dinlenmeye başladıysa açılır. Çalışan bağlantıya yalnızca fark gider.
    async def _settle(self, c: _Connection) -> None:
        if self._listener is None:
            return
        if not c.streams:
            if c.task:
                c.task.cancel()
                c.task, c.ws, c.subscribed = None, None, set()
            return
        if c.task is None:
            silence = self._silence if c is self._conns[0] else self._silence * 4
            c.task = asyncio.create_task(c.run(self._url, silence, self._listener))
            return
        await c.sync()

    async def run(self, listener: StreamListener) -> None:
        self._listener = listener
        try:
            for c in self._conns:
                await self._settle(c)
            await asyncio.Event().wait()
        finally:
            for c in self._conns:
                if c.task:
                    c.task.cancel()
                c.task, c.ws = None, None
            self._listener = None
