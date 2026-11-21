"""
file: rest.py
importable: BASE_URL, BinanceRest

BinanceRest, MarketData'nın Binance USDⓈ-M vadeli REST uçlarıyla yazılmış
uygulamasıdır. Yapıcısı httpx.AsyncClient alır, istemciyi kuran ve kapatan
programın giriş noktasıdır. İstemcinin base_url'i BASE_URL olmalıdır.

description: Binance vadeli piyasasının herkese açık REST uçlarından piyasa verisi.
"""

# NOTE: Bu uçlar herkese açıktır, API anahtarı istemez. İstek sınırı dakikada
# iki bin dört yüz ağırlıktır.
#
# NOTE: futures/data altındaki istatistik uçları yalnızca startTime verilince
# onu yok sayar ve en son kayıtları döndürür. startTime ile endTime birlikte
# verilince başlangıçtan ileriye doğru verir. Sayfalama bu yüzden her seferinde
# iki sınırı da gönderir. Otuz günden eski bir startTime 400 hatası verir,
# geriye dönük dolduran taraf başlangıcı buna göre seçmelidir.
#
# XXX: premiumIndex ucu symbol verilince tek bir sözlük, verilmeyince bütün
# paritelerin listesini döndürür. _premium iki biçimi de listeye çevirir.

import asyncio

import httpx

from ..models import Candle, DailyStats, Funding, LongShortRatio, OpenInterest, OrderBook, Premium, TakerVolume
from ..source import MarketData, MarketDataError, RatioKind

BASE_URL = "https://fapi.binance.com"

# Binance'in tek istekte verdiği en fazla kayıt sayıları.
_KLINE_LIMIT = 1500
_STATS_LIMIT = 500

_RATIO_PATHS = {
    RatioKind.ACCOUNTS: "/futures/data/globalLongShortAccountRatio",
    RatioKind.TOP_ACCOUNTS: "/futures/data/topLongShortAccountRatio",
    RatioKind.TOP_POSITIONS: "/futures/data/topLongShortPositionRatio",
}

_SECONDS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


# Aylık 1M aralığı sabit uzunlukta olmadığı için sayfalamada otuz bir gün
# sayılır. Sayfanın bir sonraki başlangıcı zaten son kaydın zamanından
# hesaplandığı için bu yalnızca sayfanın genişliğini etkiler.
def _interval_s(interval: str) -> int:
    if interval.endswith("M"):
        return 31 * 86400
    return int(interval[:-1]) * _SECONDS[interval[-1]]


def _ms(t: int | None) -> int | None:
    return None if t is None else t * 1000


def _required(end: int | None) -> int:
    if end is None:
        raise ValueError("start verilince end de verilmeli.")
    return end


def _candle(symbol: str, k: list) -> Candle:
    return Candle(symbol=symbol, time=k[0] // 1000, open=float(k[1]), high=float(k[2]), low=float(k[3]),
                  close=float(k[4]), volume=float(k[5]), quote_volume=float(k[7]), trades=int(k[8]),
                  taker_buy_volume=float(k[9]))


# lastFundingRate adı yanıltıcıdır, bu alan sıradaki fonlamada uygulanacak
# tahmini oranı gösterir.
def _premium(d: dict) -> Premium:
    return Premium(time=d["time"] // 1000, symbol=d["symbol"], mark=float(d["markPrice"]),
                   index=float(d["indexPrice"]), est_funding=float(d["lastFundingRate"]),
                   next_funding=d["nextFundingTime"] // 1000)


# İstemci dışarıdan verilir, böylece testler httpx'in sahte taşıyıcısıyla ağa
# çıkmadan gerçek cevap biçimlerini deneyebilir ve program tek istemciyi bütün
# parçalar arasında paylaşır.
#
# min_interval_s iki istek arasında en az geçmesi gereken süredir. Binance'in
# istek sınırı IP başınadır ve sunucuda eski sistemle yeni toplayıcı aynı
# adresten istek atar. Geriye dönük doldurma gibi yoğun işler bu süreyle
# yavaşlatılır.
class BinanceRest(MarketData):
    def __init__(self, client: httpx.AsyncClient, min_interval_s: float = 0) -> None:
        self._client = client
        self._gap = min_interval_s
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def _wait_turn(self) -> None:
        if not self._gap:
            return
        async with self._lock:
            loop = asyncio.get_running_loop()
            delay = self._next - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next = loop.time() + self._gap

    # Ağ hatası ve HTTP hata kodu aynı MarketDataError'a çevrilir. Binance'in
    # hata gövdesi kısa bir JSON'dur ve hatanın nedenini söyler, mesaja eklenir.
    async def _get(self, path: str, **params) -> list | dict:
        params = {k: v for k, v in params.items() if v is not None}
        await self._wait_turn()
        try:
            r = await self._client.get(path, params=params)
        except httpx.HTTPError as e:
            raise MarketDataError(f"Binance {path} isteği başarısız: {e!r}") from e
        if r.status_code != 200:
            raise MarketDataError(f"Binance {path} {r.status_code} döndürdü: {r.text[:200]}")
        return r.json()

    # Sayfanın genişliği Binance'in bir istekte verdiği kayıt sayısı kadar
    # aralıktır. Kayıtlı sayfada bir sonraki sayfa son kaydın bir aralık
    # sonrasından başlar. Boş sayfa o aralıkta veri olmadığı anlamına gelir,
    # örneğin parite henüz listelenmemiştir, ve bir sonraki pencereye geçilir.
    async def _pages(self, path: str, start: int, end: int, step_s: int, limit: int, **params) -> list:
        out, t = [], start
        while t < end:
            hi = min(end, t + step_s * limit)
            page = await self._get(path, startTime=t * 1000, endTime=hi * 1000 - 1, limit=limit, **params)
            if not page:
                t = hi
                continue
            out += page
            last = page[-1][0] if isinstance(page[-1], list) else page[-1]["timestamp"]
            t = last // 1000 + step_s
        return out

    async def candles(self, symbol: str, interval: str, limit: int = 500,
                      start: int | None = None, end: int | None = None) -> list[Candle]:
        if start is None:
            raw = await self._get("/fapi/v1/klines", symbol=symbol, interval=interval, limit=limit,
                                  endTime=None if end is None else end * 1000 - 1)
        else:
            raw = await self._pages("/fapi/v1/klines", start, _required(end), _interval_s(interval), _KLINE_LIMIT,
                                    symbol=symbol, interval=interval)
        return [_candle(symbol, k) for k in raw]

    # Mark mumu Binance'te kline biçimindedir, hacim alanları sıfır gelir.
    async def mark_candles(self, symbol: str, start: int, end: int) -> list[Candle]:
        raw = await self._pages("/fapi/v1/markPriceKlines", start, end, 60, _KLINE_LIMIT, symbol=symbol, interval="1m")
        return [_candle(symbol, k) for k in raw]

    async def exchange_info(self) -> dict:
        return await self._get("/fapi/v1/exchangeInfo")

    async def daily_stats(self, symbol: str) -> DailyStats:
        d = await self._get("/fapi/v1/ticker/24hr", symbol=symbol)
        return DailyStats(symbol=symbol, change_pct=float(d["priceChangePercent"]), high=float(d["highPrice"]),
                          low=float(d["lowPrice"]), quote_volume=float(d["quoteVolume"]))

    async def premium(self, symbol: str | None = None) -> list[Premium]:
        d = await self._get("/fapi/v1/premiumIndex", symbol=symbol)
        return [_premium(x) for x in (d if isinstance(d, list) else [d])]

    async def funding_history(self, symbol: str, start: int | None = None, end: int | None = None,
                              limit: int = 1000) -> list[Funding]:
        d = await self._get("/fapi/v1/fundingRate", symbol=symbol, startTime=_ms(start), endTime=_ms(end),
                            limit=limit)
        return [Funding(time=x["fundingTime"] // 1000, symbol=symbol, rate=float(x["fundingRate"]),
                        mark=float(x.get("markPrice") or 0)) for x in d]

    async def open_interest(self, symbol: str) -> OpenInterest:
        d = await self._get("/fapi/v1/openInterest", symbol=symbol)
        return OpenInterest(time=d["time"] // 1000, symbol=symbol, qty=float(d["openInterest"]), usd=None)

    async def _stats(self, path: str, symbol: str, period: str, limit: int, start: int | None,
                     end: int | None) -> list[dict]:
        if start is None:
            return await self._get(path, symbol=symbol, period=period, limit=limit, endTime=_ms(end))
        return await self._pages(path, start, _required(end), _interval_s(period), _STATS_LIMIT,
                                 symbol=symbol, period=period)

    async def open_interest_history(self, symbol: str, period: str, limit: int = 500,
                                    start: int | None = None, end: int | None = None) -> list[OpenInterest]:
        d = await self._stats("/futures/data/openInterestHist", symbol, period, limit, start, end)
        return [OpenInterest(time=x["timestamp"] // 1000, symbol=symbol, qty=float(x["sumOpenInterest"]),
                             usd=float(x["sumOpenInterestValue"])) for x in d]

    async def long_short(self, kind: RatioKind, symbol: str, period: str, limit: int = 500,
                         start: int | None = None, end: int | None = None) -> list[LongShortRatio]:
        d = await self._stats(_RATIO_PATHS[kind], symbol, period, limit, start, end)
        return [LongShortRatio(time=x["timestamp"] // 1000, symbol=symbol, long_pct=float(x["longAccount"]) * 100,
                               ratio=float(x["longShortRatio"])) for x in d]

    async def taker_volume(self, symbol: str, period: str, limit: int = 500,
                           start: int | None = None, end: int | None = None) -> list[TakerVolume]:
        d = await self._stats("/futures/data/takerlongshortRatio", symbol, period, limit, start, end)
        return [TakerVolume(time=x["timestamp"] // 1000, symbol=symbol, buy_sell_ratio=float(x["buySellRatio"]),
                            buy_volume=float(x["buyVol"]), sell_volume=float(x["sellVol"])) for x in d]

    async def order_book(self, symbol: str, depth: int = 20) -> OrderBook:
        d = await self._get("/fapi/v1/depth", symbol=symbol, limit=depth)
        side = lambda rows: tuple((float(p), float(q)) for p, q in rows)
        return OrderBook(time=d["T"] // 1000, symbol=symbol, bids=side(d["bids"]), asks=side(d["asks"]))

