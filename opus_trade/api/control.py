"""
file: control.py
importable: Core, create_app

Çekirdeğin kontrol API'si. create_app() bir Core alır ve FastAPI uygulamasını
kurar. Admin paneli bütün işlerini bu uçlardan yapar: profiller ve sürümler,
kayıt defterleri, hesap, günlük, işlemler, özkaynak, müdahaleler, model
erişimi, sistem olayları ve canlı akış. Uygulama yalnızca çekirdeğin Unix
soketinde dinler, dışarıya hiç açılmaz.

description: Admin panelinin kullandığı kontrol uçları.
"""

# NOTE: Giriş kontrolü burada değildir. Soket dosyasına yalnızca web servisi
# ulaşır, web servisi de admin uçlarını yalnızca admin merkezinin yetki
# kontrolünden geçen isteğe açar.
#
# NOTE: Kural, doğrulama ya da durum hatası 400 ile ve Türkçe mesajla döner,
# panel mesajı olduğu gibi gösterir. Bulunamayan profil 404'tür.
#
# NOTE: Hesabı değiştiren her istek müdahale olarak kaydedilir, panelin elle
# işlemi de kesin bir komut müdahalesidir. Böylece kimin neyi ne zaman yaptığı
# tek bir yerde durur.

import asyncio
import json
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..domain.catalog import Catalog
from ..domain.clock import Clock
from ..domain.errors import RuleError
from ..domain.profile import ProfileConfig
from ..storage.journal import JournalRepo
from ..storage.ledger import Ledger
from ..storage.profiles import ProfileRepo
from ..storage.runtime_state import StateRepo
from ..strategy.base import STRATEGIES
from ..triggers.base import TRIGGERS
from ..runtime.bus import EventBus
from ..runtime.interventions import Interventions
from ..runtime.model_access import ModelAccess
from ..runtime.profiles import ProfileService
from ..runtime.supervisor import Supervisor


@dataclass
class Core:
    """Kontrol API'sinin ulaştığı çekirdek parçaları."""

    repo: ProfileRepo
    ledger: Ledger
    journal: JournalRepo
    state: StateRepo
    service: ProfileService
    supervisor: Supervisor
    interventions: Interventions
    access: ModelAccess
    bus: EventBus
    catalog: Catalog
    clock: Clock


def _plain(x: Any) -> Any:
    if is_dataclass(x) and not isinstance(x, type):
        return {k: _plain(v) for k, v in asdict(x).items()}
    if isinstance(x, dict):
        return {k: _plain(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_plain(v) for v in x]
    return x


def _registry(reg) -> list[dict]:
    return [{"key": e.key, "name": e.name, "description": e.description, "params": _plain(list(e.params))}
            for e in reg.entries.values()]


def create_app(core: Core) -> FastAPI:
    app = FastAPI()

    @app.exception_handler(ValueError)
    async def _bad(_: Request, e: ValueError):
        return JSONResponse({"detail": str(e)}, status_code=400)

    @app.exception_handler(RuleError)
    async def _rule(_: Request, e: RuleError):
        return JSONResponse({"detail": str(e)}, status_code=400)

    def profile(pid: int):
        try:
            return core.repo.get(pid)
        except KeyError:
            raise HTTPException(404, f"{pid} numaralı profil yok.") from None

    def runner(pid: int):
        profile(pid)
        r = core.supervisor.runners.get(pid)
        if r is None:
            raise HTTPException(400, "Profil taslak ya da arşivlenmiş, hesabı çalışmıyor.")
        return r

    def summary(p) -> dict:
        r = core.supervisor.runners.get(p.id)
        st = r.broker.state() if r else None
        start = core.ledger.start_balance(p.account_id) if p.account_id else None
        return {**_plain(p), "version": core.repo.version(p.version_id).number if p.version_id else None,
                "strategy": core.repo.version(p.version_id).config.strategy if p.version_id else None,
                "equity": st.equity if st else None, "start_balance": start,
                "positions": len(st.positions) if st else 0, "orders": len(st.orders) if st else 0,
                "busy": bool(r and r.busy), "next_wake": r.next_wake if r else None}

    # --- Kayıt defterleri ---

    @app.get("/registry")
    async def registry():
        return {"strategies": _registry(STRATEGIES), "triggers": _registry(TRIGGERS),
                "symbols": [_plain(core.catalog.get(s)) for s in core.catalog.symbols]}

    # --- Profiller ---

    @app.get("/profiles")
    async def profiles():
        return [summary(p) for p in core.repo.all()]

    @app.post("/profiles")
    async def create(name: str = Body(...), short: str = Body(...), config: dict = Body(...)):
        return _plain(core.service.create(name, short, ProfileConfig.from_dict(config)))

    @app.get("/profiles/{pid}")
    async def detail(pid: int):
        p = profile(pid)
        return {**summary(p), "versions": _plain(core.repo.versions(pid)), "memory": core.journal.memory(pid)}

    @app.post("/profiles/{pid}/config")
    async def config(pid: int, config: dict = Body(...), note: str = Body("")):
        profile(pid)
        return _plain(core.service.update_config(pid, ProfileConfig.from_dict(config), note))

    # Durdururken istenirse önce bütün pozisyonlar kapanır ve emirler iptal
    # olur, bu da kesin bir komut müdahalesi olarak kaydedilir.
    @app.post("/profiles/{pid}/stop")
    async def stop(pid: int, close_all: bool = Body(False, embed=True)):
        profile(pid)
        if close_all:
            await core.interventions.add(pid, "command", {"type": "close_all"}, "firm", "Durdururken kapatıldı.")
        return _plain(core.service.move(pid, "stop"))

    @app.post("/profiles/{pid}/clone")
    async def clone(pid: int, name: str = Body(...), short: str = Body(...)):
        profile(pid)
        return _plain(core.service.clone(pid, name, short))

    @app.post("/profiles/{pid}/reset")
    async def reset(pid: int):
        profile(pid)
        return _plain(core.service.reset_account(pid))

    @app.post("/publish")
    async def publish(profile_id: int | None = Body(None, embed=True)):
        core.service.publish(profile_id)
        return {"published": profile_id}

    # --- Hesap ve kayıtlar ---

    @app.get("/profiles/{pid}/account")
    async def account(pid: int):
        r = runner(pid)
        return {"state": _plain(r.broker.state()), "prices": r.prices, "alerts": _plain(r.alerts.alerts),
                "triggers": [t.snapshot() for t in r.triggers if t.snapshot() is not None],
                "restrictions": core.interventions.active(pid, core.clock.time())}

    @app.get("/profiles/{pid}/trades")
    async def trades(pid: int):
        p = profile(pid)
        return _plain(list(reversed(core.ledger.trades(p.account_id)))) if p.account_id else []

    @app.get("/profiles/{pid}/equity")
    async def equity(pid: int, since: int = 0):
        p = profile(pid)
        rows = core.ledger.con.execute("SELECT time, cash, equity, margin FROM equity WHERE account_id = ? AND "
                                       "time >= ? ORDER BY time", (p.account_id, since))
        return [dict(r) for r in rows]

    @app.get("/profiles/{pid}/journal")
    async def journal(pid: int, before: int | None = None, limit: int = 100):
        profile(pid)
        return core.journal.entries(pid, min(limit, 500), before)

    @app.get("/profiles/{pid}/journal/{jid}/tools")
    async def tools(pid: int, jid: int):
        profile(pid)
        return core.journal.tool_calls(jid)

    # --- Müdahaleler ---

    @app.get("/profiles/{pid}/interventions")
    async def interventions(pid: int):
        profile(pid)
        return core.interventions.list(pid)

    @app.post("/profiles/{pid}/interventions")
    async def intervene(pid: int, kind: str = Body(...), params: dict = Body({}), weight: str | None = Body(None),
                        note: str = Body(""), call_now: bool = Body(False)):
        runner(pid)
        return await core.interventions.add(pid, kind, params, weight, note, call_now)

    @app.post("/profiles/{pid}/interventions/{iid}/withdraw")
    async def withdraw(pid: int, iid: int):
        profile(pid)
        return core.interventions.withdraw(pid, iid)

    # --- Sistem ---

    @app.get("/system")
    async def system():
        return {"model_closed_until": core.access.until(), "model_warning": core.access.warning(),
                "dropped_events": core.bus.dropped, "events": core.state.system_events(limit=200)}

    @app.post("/system/model")
    async def model(until: int | None = Body(None, embed=True)):
        if until is None:
            core.access.open()
        elif until <= core.clock.time():
            raise ValueError("Limitin yenileneceği saat ileride olmalı.")
        else:
            core.access.close_until(until)
        return {"model_closed_until": core.access.until()}

    # Panel bu akışı dinler. Her mesaj bir olaydır, profile alanı hangi profile
    # ait olduğunu söyler. Bağlantı başına bir kuyruk açılır, kapanınca bırakılır.
    @app.get("/stream")
    async def stream(request: Request):
        q = core.bus.subscribe()

        async def produce():
            try:
                while not await request.is_disconnected():
                    try:
                        item = await asyncio.wait_for(q.get(), 15)
                    except asyncio.TimeoutError:
                        yield ": canlı\n\n"
                        continue
                    data = _plain(item)
                    if "account_id" in data and "kind" in data:
                        data = {"account_event": data}
                    yield f"data: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
            finally:
                core.bus.unsubscribe(q)

        return StreamingResponse(produce(), media_type="text/event-stream")

    # Genel işlem yolu bütün uçlardan sonra tanımlanır. FastAPI yolları
    # tanımlandıkları sırayla eşler, önce tanımlansa clone, reset ve
    # interventions da buraya düşerdi.
    @app.post("/profiles/{pid}/{action}")
    async def move(pid: int, action: str):
        profile(pid)
        if action not in ("start", "pause", "archive"):
            raise HTTPException(404, "Bilinmeyen işlem.")
        return _plain(core.service.move(pid, action))

    return app
