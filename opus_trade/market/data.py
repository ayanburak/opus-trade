"""
file: data.py
importable: request, candles, minutes_between, fundings_between, daily_summary, funding_status,
            open_interest_change, positioning, last_funding, live_stream

Bütün fonksiyonlar Binance'in USDT vadeli piyasasından veri alır. candles()
istenen aralıkta geçmiş mumları, daily_summary() son yirmi dört saati,
funding_status() işaret fiyatını ve sıradaki fonlamayı, last_funding() en son
gerçekleşen fonlamayı getirir. live_stream() dakikalık mum ve likidasyon akışına
bağlanır, bağlantı koparsa kendi kendine yeniden bağlanır ve her bağlantıda
haber verir. minutes_between() ve
fundings_between() program kapalıyken geçen süreyi yeniden işlemek için
kullanılır. request() diğer uçlar için ortak giriş noktasıdır, piyasa aracı
da bunu kullanır.

description: Binance vadeli piyasasından katalogdaki paritelerin geçmiş ve canlı verisini alan modül.
"""

# NOTE: Binance'in bu uçları herkese açıktır, API anahtarı istemez. İstek
# sınırı dakikada iki bin dört yüz ağırlıktır, bu programın yaptığı istekler
# bunun çok altında kalır.
#
# NOTE: Mumlar t, o, h, l, c, v anahtarlı sözlüklerdir. Açılış anı, açılış,
# yüksek, düşük, kapanış ve hacim karşılıklarıdır. Canlı akıştan gelen mumda
# ayrıca paritenin sembolü s anahtarında durur.

import asyncio
import json
import logging
from typing import Awaitable, Callable

import httpx
import websockets

from .. import settings

log = logging.getLogger(__name__)

_client = httpx.AsyncClient(base_url=settings.BINANCE_REST, timeout=15)


async def request(path: str, **params) -> list | dict:
    r = await _client.get(path, params=params)
    r.raise_for_status()
    return r.json()


def _candle(m: list) -> dict:
    return {"t": m[0] // 1000, "o": float(m[1]), "h": float(m[2]), "l": float(m[3]),
            "c": float(m[4]), "v": float(m[5])}


# Binance mumları dizi olarak döndürür. Burada okunabilir kısa anahtarlı
# sözlüklere çevrilir ve zaman saniyeye indirilir, böylece grafik
# kütüphanesine doğrudan verilebilir.
async def candles(interval: str, limit: int, symbol: str) -> list[dict]:
    """Geçmiş mumları eskiden yeniye döndürür.

    Args:
        interval: Binance aralık kodu, örneğin 1m, 15m, 1h, 4h, 1d.
        limit: En fazla bin beş yüz.
        symbol: Vadeli piyasa sembolü.

    Returns:
        t, o, h, l, c, v anahtarlı mum listesi. Son mum henüz kapanmamış olabilir.
    """
    raw = await request("/fapi/v1/klines", symbol=symbol, interval=interval, limit=limit)
    return [_candle(m) for m in raw]


# Binance tek istekte en fazla bin beş yüz mum verir. Daha uzun bir aralık
# sayfa sayfa çekilir.
async def minutes_between(symbol: str, start: int, end: int) -> list[dict]:
    """İki an arasındaki bütün dakikalık mumları eskiden yeniye döndürür.

    Args:
        start: İlk mumun açılış anı, unix saniye.
        end: Bu andan önce açılan mumlar döner, unix saniye.

    Returns:
        candles() ile aynı biçimde liste.
    """
    result, t = [], start
    while t < end:
        raw = await request("/fapi/v1/klines", symbol=symbol, interval="1m",
                            startTime=t * 1000, endTime=end * 1000 - 1, limit=1500)
        if not raw:
            break
        result += [_candle(m) for m in raw]
        t = raw[-1][0] // 1000 + 60
    return result


async def fundings_between(symbol: str, start: int, end: int) -> list[dict]:
    d = await request("/fapi/v1/fundingRate", symbol=symbol, startTime=start * 1000,
                      endTime=end * 1000, limit=1000)
    return [{"time": x["fundingTime"] // 1000, "rate": float(x["fundingRate"]),
             "price": float(x.get("markPrice") or 0)} for x in d]


async def daily_summary(symbol: str) -> dict:
    d = await request("/fapi/v1/ticker/24hr", symbol=symbol)
    return {
        "change_pct": float(d["priceChangePercent"]),
        "high": float(d["highPrice"]),
        "low": float(d["lowPrice"]),
        "volume_usd": round(float(d["quoteVolume"])),
    }


# lastFundingRate adı yanıltıcıdır, Binance'te bu alan sıradaki fonlamada
# uygulanacak tahmini oranı gösterir.
async def funding_status(symbol: str) -> dict:
    d = await request("/fapi/v1/premiumIndex", symbol=symbol)
    oi = await request("/fapi/v1/openInterest", symbol=symbol)
    return {
        "mark_price": float(d["markPrice"]),
        "est_rate": float(d["lastFundingRate"]),
        "next_funding": d["nextFundingTime"] // 1000,
        "open_interest": float(oi["openInterest"]),
    }


# Açık pozisyonun son yirmi dört saatteki değişimi. Binance bu veriyi
# saatlik dilimlerle verir, yirmi beş dilimin ilki ile sonuncusu karşılaştırılır.
async def open_interest_change(symbol: str) -> dict | None:
    """Açık pozisyonun güncel dolar karşılığını ve yirmi dört saatlik yüzde değişimini döndürür.

    Returns:
        usd ve change_pct anahtarlı sözlük. Binance bu paritede veri vermiyorsa None.
    """
    d = await request("/futures/data/openInterestHist", symbol=symbol, period="1h", limit=25)
    if len(d) < 2:
        return None
    first, last = float(d[0]["sumOpenInterest"]), float(d[-1]["sumOpenInterest"])
    return {"usd": float(d[-1]["sumOpenInterestValue"]), "change_pct": (last / first - 1) * 100 if first else 0.0}


# Altın gibi geleneksel varlıklarda Binance bu verileri vermeyebilir. O
# durumda boş liste ya da hata döner, ikisi de veri yok sayılır.
async def positioning(symbol: str) -> dict | None:
    """Hesapların ve büyük hesapların long yüzdesini, agresif alış satış oranını döndürür.

    Returns:
        accounts_long, top_long, taker_last ve taker_24h anahtarlı sözlük.
        accounts_long ve top_long yüzde, taker değerleri alışın satışa oranıdır.
        Veri yoksa None.
    """
    p = {"symbol": symbol, "period": "1h"}
    try:
        everyone, top, taker = await asyncio.gather(
            request("/futures/data/globalLongShortAccountRatio", limit=1, **p),
            request("/futures/data/topLongShortPositionRatio", limit=1, **p),
            request("/futures/data/takerlongshortRatio", limit=24, **p),
        )
    except httpx.HTTPStatusError:
        return None
    if not everyone or not top or not taker:
        return None
    ratios = [float(x["buySellRatio"]) for x in taker]
    return {"accounts_long": float(everyone[-1]["longAccount"]) * 100, "top_long": float(top[-1]["longAccount"]) * 100,
            "taker_last": ratios[-1], "taker_24h": sum(ratios) / len(ratios)}


async def last_funding(symbol: str) -> dict:
    d = (await request("/fapi/v1/fundingRate", symbol=symbol, limit=1))[-1]
    return {"time": d["fundingTime"] // 1000, "rate": float(d["fundingRate"]),
            "price": float(d.get("markPrice") or 0)}


def _kline(d: dict) -> dict:
    k = d["k"]
    return {"s": k["s"], "t": k["t"] // 1000, "o": float(k["o"]), "h": float(k["h"]),
            "l": float(k["l"]), "c": float(k["c"]), "v": float(k["v"])}


# Akıştaki SELL emri bir long'un, BUY emri bir short'un zorla kapatılmasıdır.
# Dolar karşılığı gerçekleşen ortalama fiyat çarpı toplam miktardır.
def _force_order(d: dict) -> dict:
    o = d["o"]
    price, qty = float(o["ap"]), float(o["z"])
    return {"time": o["T"] // 1000, "symbol": o["s"], "side": "long" if o["S"] == "SELL" else "short",
            "price": price, "qty": qty, "usd": price * qty}


# Bağlantı her koptuğunda bekleme süresi ikiye katlanır, bir dakikada
# sabitlenir. Başarılı bir mesaj gelince tekrar başa döner. on_connect
# beklenirken gelen mesajlar bağlantının tamponunda bekler, kaybolmaz.
async def live_stream(
    symbols: list[str], on_candle: Callable[[dict], Awaitable[None]],
    liquidation_symbols: list[str], on_liquidation: Callable[[dict], None],
    on_connect: Callable[[], Awaitable[None]],
) -> None:
    """Verilen paritelerin dakikalık mumlarını ve likidasyonlarını tek bağlantıdan sonsuza kadar dinler.

    Args:
        symbols: Mumları dinlenecek pariteler.
        on_candle: Her mum güncellemesinde s, t, o, h, l, c, v anahtarlı mumla çağrılan async fonksiyon.
            s paritenin sembolüdür.
        liquidation_symbols: Likidasyonları dinlenecek pariteler.
        on_liquidation: Her likidasyonda time, symbol, side, price, qty ve usd anahtarlı kayıtla çağrılır.
        on_connect: Her bağlantı kurulduğunda, ilk mesaj okunmadan önce çağrılan async fonksiyon.
            Kopukken kaçan mumlar bununla işlenir.
    """
    streams = [f"{s.lower()}@kline_1m" for s in symbols] + [f"{s.lower()}@forceOrder" for s in liquidation_symbols]
    url = settings.BINANCE_WS + "/".join(streams)
    wait = 1
    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                log.info("Binance canlı akışına bağlanıldı.")
                await on_connect()
                while True:
                    # Bağlantı açık görünüp veri gelmeyebiliyor. Bu durumda
                    # hata oluşmadığı için akış sessizce donar. Süre sınırı
                    # bunu yakalayıp yeniden bağlanmayı sağlar. Likidasyonlar
                    # seyrek olduğu için sessizliği mumlar belirler.
                    try:
                        msg = await asyncio.wait_for(ws.recv(), settings.STREAM_SILENCE_S)
                    except asyncio.TimeoutError:
                        raise ConnectionError(f"{settings.STREAM_SILENCE_S} saniyedir veri gelmiyor")
                    wait = 1
                    data = json.loads(msg)["data"]
                    if data.get("e") == "forceOrder":
                        on_liquidation(_force_order(data))
                    else:
                        await on_candle(_kline(data))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("Canlı akış koptu: %s. %s saniye sonra yeniden bağlanılacak.", e, wait)
            await asyncio.sleep(wait)
            wait = min(wait * 2, 60)
