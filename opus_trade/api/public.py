"""
file: public.py
importable: Hub, create_public_app, public_entry, public_event

Herkese açık API. create_public_app() çekirdeğin kontrol API'sine giden bir
istemci alır ve yalnızca yayındaki profilin verisini veren FastAPI
uygulamasını kurar. opus-web bu uygulamayı /api altında sunar.

Hub çekirdeğin canlı akışına tek bir bağlantı açar ve gelen olayları süzüp
bağlı ziyaretçilere dağıtır. Mum güncellemeleri yalnızca yayındaki profilin
paritelerinden geçer. Ziyaretçi sayısı sınırlıdır, sınır doluysa yeni
bağlantı 503 alır. public_entry() bir günlük kaydını, public_event() bir canlı
olayı herkese açık hale getirir, gösterilmeyecekse public_event() None döner.

description: Yayındaki profilin süzülmüş verisini veren herkese açık uçlar.
"""

# NOTE: Süzme beyaz liste ile yapılır. Bir alan ya da olay türü burada adıyla
# izin almadıysa dışarı çıkmaz. Yeni bir alan eklenince kendiliğinden
# sızmasın diye böyle seçildi.
#
# NOTE: Yönetici izi sayılanlar: müdahaleler, yöneticiye cevap, Opus'a giden
# talimatın tamamı ve modelin ham çıktısı çünkü ikisi de yönetici bölümünü ve
# cevabı taşır, yönetici çağrısıyla gelen uyandırmalar, yöneticinin kurduğu
# alarmlar ve onların tetiklenmesi, kısıtlar, bildirimler. Karar hatasının
# metni de çıkmaz, claude komutunun hatası hesap bilgisi taşıyabilir.
#
# NOTE: Yöneticinin kendi hesap işlemleri, yani açtığı ya da kapattığı
# pozisyonlar, hesap olayı ve işlem olarak, yapanı admin olarak görünür kalır.
# Gizlenirse hesabın özkaynağı ile işlem listesi birbirini tutmaz.
#
# NOTE: Yayındaki profil her istekte çekirdekten okunur. Yayın değişince Hub
# bir sonraki olayda yeni profile göre süzer, çünkü profil birkaç saniyelik bir
# önbellekte tutulur.

import asyncio
import json
import logging
import time
from typing import AsyncIterator, Awaitable, Callable

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse

log = logging.getLogger(__name__)

_PROFILE_KEYS = ("id", "name", "short", "strategy", "state", "version", "equity", "start_balance", "positions",
                 "orders", "busy", "next_wake")
_RECORD_KEYS = ("summary", "decision", "tools", "model", "effort", "duration_s", "template_version", "waves")
_ADMIN_FIELDS = ("yoneticiye_cevap",)
_RESTRICTION = "yöneticinin #"


# --- Süzme ---

def _admin_alert(data: dict | None) -> bool:
    return bool(data) and data.get("inner") == "alert" and data.get("data", {}).get("actor") == "admin"


def _public_wake(w: dict) -> bool:
    if w["kind"] == "admin":
        return False
    return not _admin_alert(json.loads(w["data"])) if w.get("data") else True


# Kapının yönetici kısıtı reddi bu kalıpla başlar, Opus kısıtın numarasını ve
# içeriğini görür. Dışarıda yalnızca eylemin reddedildiği kalır.
def _public_result(r: dict) -> dict:
    if not r["ok"] and _RESTRICTION in r["message"]:
        return {**r, "message": f"{r['label']} reddedildi."}
    return r


def public_entry(e: dict) -> dict:
    """Bir günlük kaydından yönetici izlerini ve iç ayrıntıları çıkarır."""
    record = {k: e["record"][k] for k in _RECORD_KEYS if k in e["record"]}
    if isinstance(record.get("decision"), dict):
        record["decision"] = {k: v for k, v in record["decision"].items() if k not in _ADMIN_FIELDS}
    return {"id": e["id"], "time": e["time"], "version_id": e["version_id"], "next_wake": e["next_wake"],
            "wakes": [w for w in e["wakes"] if _public_wake(w)], "results": [_public_result(r) for r in e["results"]], "record": record,
            "error": "Karar alınamadı." if e["error"] else None}


def public_event(item: dict, pid: int, account_id: int | None, symbols: frozenset[str] = frozenset()) -> dict | None:
    """Kontrol akışından gelen bir olayı herkese açık hale getirir ya da gösterilmeyecekse None döndürür."""
    if "candle" in item:
        return item if item["candle"].get("symbol") in symbols else None
    if "account_event" in item:
        return item if account_id is not None and item["account_event"].get("account_id") == account_id else None
    if item.get("profile") != pid:
        return None
    if "journal" in item or "tool" in item or "version" in item:
        return item
    if "fire" in item:
        fire = item["fire"]
        if fire.get("kind") == "alert" and (fire.get("data") or {}).get("actor") == "admin":
            return None
        return item
    return None


# --- Canlı akış ---

class Hub:
    """Çekirdeğin akışını ziyaretçilere dağıtan tek bağlantı.

    Args:
        source: Çekirdeğin olaylarını sırayla veren akış fonksiyonu, koparsa Hub yeniden açar.
        published: Yayındaki profilin kimliğini, hesabını ve paritelerini veren fonksiyon, yayında profil yoksa None.
        max_clients: Aynı anda en fazla ziyaretçi.
        retry_s: Akış koparsa yeniden bağlanmadan önce beklenen süre.
    """

    def __init__(self, source: Callable[[], AsyncIterator[dict]],
                 published: Callable[[], Awaitable[tuple[int, int | None, frozenset[str]] | None]], max_clients: int = 50,
                 retry_s: float = 3) -> None:
        self.source = source
        self.published = published
        self.max_clients = max_clients
        self.retry_s = retry_s
        self.clients: set[asyncio.Queue] = set()
        self._task: asyncio.Task | None = None

    def join(self) -> asyncio.Queue | None:
        """Bir ziyaretçi kuyruğu açar, sınır doluysa None döndürür. Upstream bağlantısı ilk ziyaretçiyle açılır."""
        if len(self.clients) >= self.max_clients:
            return None
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self.clients.add(q)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._pump())
        return q

    # Son ziyaretçi ayrılınca çekirdeğe açık bağlantı da kapanır, boşuna
    # olay taşınmaz.
    def leave(self, q: asyncio.Queue) -> None:
        self.clients.discard(q)
        if not self.clients and self._task:
            self._task.cancel()
            self._task = None

    async def dispatch(self, item: dict) -> None:
        pub = await self.published()
        if pub is None:
            return
        out = public_event(item, *pub)
        if out is None:
            return
        for q in list(self.clients):
            if not q.full():
                q.put_nowait(out)

    async def _pump(self) -> None:
        while self.clients:
            try:
                async for item in self.source():
                    await self.dispatch(item)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Çekirdeğin akışı koptu: %s", e)
            await asyncio.sleep(self.retry_s)


# --- Uygulama ---

def create_public_app(core: httpx.AsyncClient, max_clients: int = 50,
                      source: Callable[[], AsyncIterator[dict]] | None = None) -> FastAPI:
    """Herkese açık uçları kurar.

    Args:
        core: Çekirdeğin kontrol API'sine giden istemci.
        max_clients: Canlı akışa aynı anda bağlanabilecek en fazla ziyaretçi.
        source: Çekirdeğin olay akışı. Verilmezse core'un /stream ucu okunur, testte sahtesi verilir.
    """
    app = FastAPI()
    cache: dict = {"at": 0.0, "value": None}

    async def call(path: str, **params):
        r = await core.get(path, params=params)
        if r.status_code == 404:
            raise HTTPException(404, "Bulunamadı.")
        if r.status_code == 400:
            raise HTTPException(400, r.json().get("detail", "Geçersiz istek."))
        r.raise_for_status()
        return r.json()

    async def published() -> dict:
        found = next((p for p in await call("/profiles") if p.get("published")), None)
        if found is None:
            raise HTTPException(404, "Yayında bir profil yok.")
        return found

    async def profile_view() -> dict:
        p = await published()
        detail = await call(f"/profiles/{p['id']}")
        versions = {v["id"]: v for v in detail["versions"]}
        current = versions.get(p.get("version_id"))
        return {**{k: p.get(k) for k in _PROFILE_KEYS},
                "symbols": current["config"]["symbols"] if current else [], "account_id": p.get("account_id")}

    async def cached() -> tuple[int, int | None, frozenset[str]] | None:
        if time.monotonic() - cache["at"] > 5:
            try:
                p = await profile_view()
                cache["value"] = (p["id"], p["account_id"], frozenset(p["symbols"]))
            except HTTPException:
                cache["value"] = None
            cache["at"] = time.monotonic()
        return cache["value"]

    async def core_stream() -> AsyncIterator[dict]:
        async with core.stream("GET", "/stream", timeout=None) as r:
            async for line in r.aiter_lines():
                if line.startswith("data: "):
                    yield json.loads(line[6:])

    hub = Hub(source or core_stream, cached, max_clients)
    app.state.hub = hub

    @app.get("/profile")
    async def profile():
        view = await profile_view()
        view.pop("account_id")
        return view


    @app.get("/account")
    async def account():
        p = await published()
        acc = await call(f"/profiles/{p['id']}/account")
        return {"state": acc["state"], "prices": acc["prices"], "triggers": acc["triggers"],
                "alerts": [a for a in acc["alerts"] if a.get("actor") != "admin"]}

    @app.get("/trades")
    async def trades():
        return await call(f"/profiles/{(await published())['id']}/trades")

    @app.get("/equity")
    async def equity(since: int = 0):
        return await call(f"/profiles/{(await published())['id']}/equity", since=since)

    @app.get("/journal")
    async def journal(before: int | None = None, limit: int = 50):
        p = await published()
        params = {"limit": min(limit, 100)} | ({"before": before} if before is not None else {})
        return [public_entry(e) for e in await call(f"/profiles/{p['id']}/journal", **params)]

    @app.get("/journal/{jid}/tools")
    async def tools(jid: int):
        return await call(f"/profiles/{(await published())['id']}/journal/{jid}/tools")

    @app.get("/candles")
    async def candles(symbol: str, interval: str = "15m", limit: int = 300):
        if symbol not in (await profile())["symbols"]:
            raise HTTPException(404, "Bu parite yayındaki profilde yok.")
        return await call("/candles", symbol=symbol, interval=interval, limit=min(limit, 1000))

    # On beş saniyede bir boş bir yorum satırı gider, böylece arada bir vekil
    # sunucu sessiz bağlantıyı kapatmaz.
    @app.get("/stream")
    async def stream():
        q = hub.join()
        if q is None:
            raise HTTPException(503, "Canlı akış şu an dolu, biraz sonra yeniden deneyin.")

        async def produce():
            try:
                while True:
                    try:
                        item = await asyncio.wait_for(q.get(), 15)
                    except asyncio.TimeoutError:
                        yield ": canlı\n\n"
                        continue
                    yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
            finally:
                hub.leave(q)

        return StreamingResponse(produce(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    return app
