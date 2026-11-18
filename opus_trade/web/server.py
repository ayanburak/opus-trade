"""
file: server.py
importable: app

FastAPI uygulaması. Açılışta her profil için bir Engine kurar ve başlatır,
bütün profillerin paritelerini tek bir Binance akışından dinleyip her mumu
ilgili profillere dağıtır ve katalogdaki paritelerin likidasyonlarını
kaydeder. Arayüz dosyalarını sunar, her profilin verisini /api/<profil>/
altındaki uçlarla JSON olarak verir ve canlı olayları server sent events ile
tarayıcıya iletir. Kapanış isteğini yalnızca doğru anahtarla gelirse bütün
profillere iletir.

description: Bütün profilleri tek programda yürüten ve arayüzle veri uçlarını sunan web katmanı.
"""

# NOTE: Canlı olaylar için websocket yerine server sent events seçildi.
# Akış tek yönlü, tarayıcı sunucuya bir şey göndermiyor, ve tarayıcı
# bağlantı koparsa kendisi yeniden bağlanıyor.
#
# NOTE: Arayüzde kapatma düğmesi yoktur, sayfayı izleyen biri programı
# kapatamaz. Program her açılışta rastgele bir anahtar üretip yalnızca
# sahibinin okuyabildiği bir dosyaya yazar. /api/shutdown bu anahtarı
# Authorization başlığında ister. Anahtarı python -m opus_trade shutdown komutu
# dosyadan okuyup gönderir. Program kapanınca dosya silinir.
#
# NOTE: Kapanış bütün programı kapsar. Her profilin Opus'u aynı anda bilgilendirilir,
# hepsinin son kararı uygulanınca program kendine kesme sinyali gönderir.
# Uvicorn bunu Ctrl+C gibi karşılar ve düzgünce kapanır.
#
# NOTE: Arayüz bütün adresleri göreli ister, örneğin api/<profil>/state. Böylece
# sayfa bir vekil sunucunun arkasında bir alt yolda
# da değişiklik yapmadan çalışır. Bunun için alt yol sondaki eğik çizgiyle açılmalıdır.

import asyncio
import hmac
import json
import logging
import os
import secrets
import signal
import time
from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .. import settings
from ..db import get_state
from ..engine.scheduler import Engine, journal_list, journal_row, trade_lists
from ..market import data as market_data
from ..market import liquidations
from . import about

log = logging.getLogger(__name__)

_token = secrets.token_urlsafe(32)
_closed: set[str] = set()
_failing: set[str] = set()


# Her profilin Engine'i son kararını uygulayınca buraya haber verir. Bütün
# profiller kapanınca program kapanır.
def _engine_closed(pid: str) -> None:
    _closed.add(pid)
    if _closed >= engines.keys():
        asyncio.get_running_loop().call_later(0.5, os.kill, os.getpid(), signal.SIGINT)


# Likidasyon kayıtları bütün profillerde ortaktır. Program tek döngüde
# çalıştığı için akışın yazdığı ve motorların okuduğu bağlantı aynıdır.
market_con = liquidations.connect(settings.MARKET_DB)

engines = {pid: Engine(p, market_con, lambda pid=pid: _engine_closed(pid)) for pid, p in settings.PROFILES.items()}


def _write_token() -> None:
    settings.TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(settings.TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(_token)


# Akış bütün profillerin paritelerini bir kez dinler. Aynı parite birden çok
# profilde varsa mum her birine ayrı ayrı verilir. Bir profilin hatası akışa
# sızmaz, yoksa akış kopup yeniden bağlanırdı ve hata kalıcıysa hiçbir profil
# fiyat alamazdı. Aynı profil her mumda hata verebileceği için hata yalnızca
# ilk seferinde yazılır, profil düzelince bu da yazılır.
async def _dispatch(candle: dict) -> None:
    for pid, e in engines.items():
        if candle["s"] not in e.p.symbols:
            continue
        try:
            await e.on_candle(candle)
        except Exception:
            if pid not in _failing:
                _failing.add(pid)
                log.exception("[%s] Mum işlenirken hata oluştu, akış diğer profiller için sürüyor.", e.p.short)
            continue
        if pid in _failing:
            _failing.discard(pid)
            log.info("[%s] Mumlar yeniden hatasız işleniyor.", e.p.short)


# Akış her bağlandığında her profil kopukken kaçan mumları işler. Bir
# profilin hatası, örneğin Binance'in REST ucuna o an ulaşılamaması, diğer
# profilleri ve akışı durdurmaz. O profilde kopuş süresi işlenmemiş kalır.
async def _catch_up() -> None:
    for e in engines.values():
        try:
            await e.catch_up()
        except Exception:
            log.exception("[%s] Akış kopukken geçen mumlar işlenemedi.", e.p.short)


# Likidasyon önce kaydedilir, sonra gözcüsü olan profillere verilir. Bir
# profilin gözcüsündeki hata kaydı ve diğer profilleri durdurmaz.
def _record_liquidation(event: dict) -> None:
    liquidations.record(market_con, event)
    for e in engines.values():
        try:
            e.on_liquidation(event)
        except Exception:
            log.exception("[%s] Likidasyon gözcülere verilirken hata oluştu.", e.p.short)


# Likidasyonlar en az bir dönem boyu saklanır, dönem bitince arşive
# kopyalanabilsinler diye. En uzun dönemden bir gün eski kayıtlar saatte bir silinir.
async def _purge_liquidations() -> None:
    keep = max(p.period_s for p in settings.PROFILES.values()) + 86400
    while True:
        liquidations.purge(market_con, int(time.time()), keep)
        await asyncio.sleep(3600)


@asynccontextmanager
async def _lifespan(_: FastAPI):
    _write_token()
    await asyncio.gather(*(e.start() for e in engines.values()))
    symbols = sorted({s for e in engines.values() for s in e.p.symbols})
    # Likidasyonlar katalogdaki bütün paritelerde kaydedilir, böylece bir
    # profile parite eklenince geçmişi hazır olur.
    stream = asyncio.create_task(market_data.live_stream(symbols, _dispatch, sorted(settings.SYMBOLS),
                                                         _record_liquidation, _catch_up))
    purge = asyncio.create_task(_purge_liquidations())
    yield
    stream.cancel()
    purge.cancel()
    for e in engines.values():
        await e.stop()
    market_con.close()
    settings.TOKEN_PATH.unlink(missing_ok=True)


app = FastAPI(lifespan=_lifespan)


# Tarayıcı arayüz dosyalarını her açılışta sunucuya sorar. Yoksa güncellemeden
# sonra eski app.js önbellekten çalışmaya devam ediyor. Dosyalar küçük ve
# sunucu yerel olduğu için bunun bir maliyeti yok.
@app.middleware("http")
async def _no_cache(req, call_next):
    resp = await call_next(req)
    if req.url.path == "/" or req.url.path.startswith(("/static/", "/details")):
        resp.headers["Cache-Control"] = "no-cache"
    return resp
app.mount("/static", StaticFiles(directory=settings.WEB_DIR), name="static")

_INTERVALS = {"1m", "5m", "15m", "1h", "4h", "1d"}


def _engine(pid: str) -> Engine:
    if pid not in engines:
        raise HTTPException(404, f"Böyle bir profil yok: {pid}")
    return engines[pid]


@app.get("/")
async def index():
    return FileResponse(settings.WEB_DIR / "index.html")


# Hakkında sayfası kendi bölümlerinin adresleriyle açılır, örneğin
# details/mimari. Bütün adresler aynı kabuğu döndürür, hangi bölümün
# gösterileceğine sayfa adresten karar verir. Sondaki eğik çizgi şarttır,
# sayfa da bütün adresleri ona göre göreli ister.
@app.get("/details")
async def details_root():
    return RedirectResponse("details/", status_code=301)


@app.get("/details/")
@app.get("/details/{section}")
async def details(section: str = ""):
    return FileResponse(settings.WEB_DIR / "docs" / "index.html")


@app.get("/api/about")
async def about_data():
    periods = {pid: (get_state(e.con, "start_time"), get_state(e.con, "end_time")) for pid, e in engines.items()}
    return await about.about(periods)


@app.get("/api/profiles")
async def profiles():
    return [{"id": p.id, "name": p.name, "short": p.short} for p in settings.PROFILES.values()]


# Grafik her açılışta ve her parite ya da aralık değişiminde bin mum ister.
# Sayfayı aynı anda birçok kişi izleyebileceği için cevap birkaç saniye
# saklanır, yoksa her ziyaretçi Binance'e ayrı istek atar ve sunucunun adresi
# Binance'in istek sınırına takılabilir. Bu da motorların veri almasını bozar.
_candle_cache: dict[tuple[str, str], tuple[float, list]] = {}
_CANDLE_CACHE_S = 5


@app.get("/api/candles")
async def candles(symbol: str, interval: str = "15m"):
    if interval not in _INTERVALS:
        raise HTTPException(400, f"Geçersiz aralık: {interval}")
    if symbol not in settings.SYMBOLS:
        raise HTTPException(400, f"Katalogda olmayan parite: {symbol}")
    hit = _candle_cache.get((symbol, interval))
    if hit and time.time() - hit[0] < _CANDLE_CACHE_S:
        return hit[1]
    data = await market_data.candles(interval, 1000, symbol)
    _candle_cache[(symbol, interval)] = (time.time(), data)
    return data


# Parite listesindeki 24 saatlik değişimler için kullanılır. Liste sık
# açılabileceği için sonuç yarım dakika saklanır.
_summary_cache: dict = {"time": 0, "data": None}


@app.get("/api/summary")
async def summary():
    if time.time() - _summary_cache["time"] > 30:
        found = await asyncio.gather(*(market_data.daily_summary(s) for s in settings.SYMBOLS))
        _summary_cache.update(time=time.time(), data=dict(zip(settings.SYMBOLS, found)))
    return _summary_cache["data"]


# return_at tahmini dönüş anıdır, unix saniye ya da boş. Cevap hemen döner,
# kapanış arka planda sürer.
@app.post("/api/shutdown")
async def shutdown(return_at: int | None = Body(default=None, embed=True), authorization: str = Header(default="")):
    if not hmac.compare_digest(authorization, f"Bearer {_token}"):
        raise HTTPException(403, "Geçersiz anahtar.")
    for e in engines.values():
        e.prepare_shutdown(return_at)
    return {"ok": True}


@app.get("/api/{pid}/state")
async def state(pid: str):
    return _engine(pid).state()


@app.get("/api/{pid}/trades")
async def trades(pid: str):
    return trade_lists(_engine(pid).con)


@app.get("/api/{pid}/journal")
async def journal(pid: str):
    return journal_list(_engine(pid).con)


@app.get("/api/{pid}/journal/{id_}")
async def journal_detail(pid: str, id_: int):
    try:
        return journal_row(_engine(pid).con, id_, full=True)
    except IndexError:
        raise HTTPException(404, "Kayıt bulunamadı.")


# On beş saniyede bir boş bir yorum satırı gönderilir. Böylece arada bir
# vekil sunucu ya da tarayıcı sessiz bağlantıyı kapatmaz.
@app.get("/api/{pid}/stream")
async def stream(pid: str):
    e = _engine(pid)
    q = e.listen()

    async def produce():
        try:
            while True:
                try:
                    m = await asyncio.wait_for(q.get(), 15)
                    yield f"event: {m['kind']}\ndata: {json.dumps(m['data'], ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": alive\n\n"
        finally:
            e.release(q)

    return StreamingResponse(produce(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
