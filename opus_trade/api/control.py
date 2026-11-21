"""
file: control.py
importable: Core, create_app

Çekirdeğin kontrol API'si. create_app() bir Core alır ve FastAPI uygulamasını
kurar. Admin paneli bütün işlerini bu uçlardan yapar: profiller ve sürümler,
kayıt defterleri, hesap, günlük, not defteri, işlemler, özkaynak, müdahaleler,
model erişimi, bildirimler, sistemin durumu ve olayları, abonelik tüketimi ve
canlı akış. Uygulama yalnızca çekirdeğin Unix
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
from datetime import datetime, time
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..domain import rules
from ..domain.catalog import Catalog
from ..domain.models import Side
from ..domain.clock import Clock
from ..domain.errors import RuleError
from ..domain.profile import ProfileConfig
from ..domain.trading import Actor, ClosePosition, PlaceOrder, SetLeverage
from ..storage.journal import JournalRepo
from ..storage.ledger import Ledger
from ..storage.notifications import NotificationRepo
from ..storage.profiles import ProfileRepo
from ..storage.runtime_state import StateRepo
from ..strategy.base import STRATEGIES
from ..triggers.base import TRIGGERS
from ..runtime.bus import EventBus
from ..runtime.interventions import Interventions, commands_from
from ..runtime.health import CollectorWatch
from ..runtime.model_access import ModelAccess
from ..runtime.risk import DAY_TZ
from ..runtime.notifications import Notifier, settings_from, DEFAULT_SETTINGS
from ..runtime.profiles import ProfileService
from ..runtime.supervisor import Supervisor
from ..runtime.usage import summarize


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
    notifications: NotificationRepo
    notifier: Notifier
    collector: CollectorWatch


# Genel bakıştaki küçük özkaynak eğrisinin en fazla nokta sayısı.
CURVE_POINTS = 60

# Tüketim raporunun en fazla gün sayısı.
USAGE_DAYS = 60

# Grafiğin seçebildiği mum aralıkları.
INTERVALS = ("1m", "5m", "15m", "1h", "4h", "1d")


def _plain(x: Any) -> Any:
    if is_dataclass(x) and not isinstance(x, type):
        return {k: _plain(v) for k, v in asdict(x).items()}
    if isinstance(x, dict):
        return {k: _plain(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_plain(v) for v in x]
    return x


# Strateji kaydında accepts_messages ve uses_model da gelir. Panel Opus'a yaz kartını
# yalnızca mesaj okuyabilen, not defteri sekmesini yalnızca model kullanan stratejide
# gösterir. Tetikleyicilerde bu bayraklar yoktur, False döner.
def _registry(reg) -> list[dict]:
    return [{"key": e.key, "name": e.name, "description": e.description, "params": _plain(list(e.params)),
             "accepts_messages": bool(getattr(e.cls, "accepts_messages", False)),
             "uses_model": bool(getattr(e.cls, "uses_model", False))}
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

    # --- Genel bakış ---

    # Eğri son yedi günün özkaynağından eşit aralıklı noktalarla seyreltilir,
    # son nokta her zaman girer. Bugün Türkiye saatiyle gece yarısından beri
    # demektir, sonuç kapanan işlemlerin neti ile açık pozisyonların kâr ve
    # zararıdır. Düzeltme çağrısı günlükteki uyandırmalardan sayılır.
    def day_start() -> int:
        now = datetime.fromtimestamp(core.clock.time(), DAY_TZ)
        return int(datetime.combine(now.date(), time(0), DAY_TZ).timestamp())

    def curve(account_id: int) -> list[dict]:
        rows = core.ledger.con.execute("SELECT time, equity FROM equity WHERE account_id = ? AND time >= ? ORDER BY time",
                                       (account_id, core.clock.time() - 7 * 86400)).fetchall()
        step = max(1, len(rows) // CURVE_POINTS)
        points = rows[::step]
        if rows and points[-1] is not rows[-1]:
            points.append(rows[-1])
        return [{"time": t, "equity": e} for t, e in points]

    def today(p, since: int) -> dict:
        calls, errors, corrections = core.journal.con.execute(
            "SELECT COUNT(*), COUNT(error), SUM(wakes LIKE '%\"correction\"%') FROM journal "
            "WHERE profile_id = ? AND time >= ?", (p.id, since)).fetchone()
        r = core.supervisor.runners.get(p.id)
        realized = sum(t.net for t in core.ledger.trades(p.account_id, since=since)) if p.account_id else 0.0
        st = r.broker.state() if r else None
        open_pnl = st.equity - st.cash - sum(x.margin for x in st.positions) if st else 0.0
        risk = sum(x.margin for x in st.positions) + sum(o.margin for o in st.orders) if st else 0.0
        return {"pnl": realized + open_pnl, "calls": calls, "errors": errors, "corrections": corrections or 0,
                "risk": risk}

    @app.get("/overview")
    async def overview():
        since = day_start()
        out = []
        for p in core.repo.all():
            last = core.journal.entries(p.id, limit=1)
            out.append({**summary(p), "curve": curve(p.account_id) if p.account_id else [], "today": today(p, since),
                        "last": {"time": last[0]["time"], "summary": last[0]["record"].get("summary"),
                                 "error": last[0]["error"] is not None} if last else None})
        return {"profiles": out, "model_closed_until": core.access.until(), "model_warning": core.access.warning(),
                "collector": {"last_seen": core.collector.last_seen(), "down": core.collector.down},
                "stream_down": core.supervisor.stream_down}

    # --- Kayıt defterleri ---

    @app.get("/registry")
    async def registry():
        return {"strategies": _registry(STRATEGIES), "triggers": _registry(TRIGGERS),
                "symbols": [_plain(core.catalog.get(s)) for s in core.catalog.symbols]}

    # LLM stratejisinin Opus'a açtığı araçlar ve karar şemasındaki eylemler.
    # Piyasa araçları araç sunucusunun kendi listesinden, araştırmacı istemcinin
    # ayarından, eylemler ACTIONS'tan okunur, panel hiçbir listeyi elle tutmaz.
    # Araç sunucusu ağır bir modüldür, yalnızca bu uç çağrılınca yüklenir.
    @app.get("/registry/llm")
    async def llm_registry():
        from ..strategy.llm.actions import ACTIONS
        from ..strategy.llm.client import BUILTIN_TOOLS, RESEARCHER
        from ..tools.market import server

        tools = []
        for t in await server.list_tools():
            props, required = t.input_schema.get("properties", {}), set(t.input_schema.get("required", []))
            tools.append({"name": t.name, "description": t.description or "",
                          "params": [{"name": k, "type": v.get("type") or "any", "default": v.get("default"),
                                      "required": k in required} for k, v in props.items()]})
        return {"server": server.name, "tools": tools, "builtin": list(BUILTIN_TOOLS),
                "agents": [{"name": k, **v} for k, v in RESEARCHER.items()],
                "actions": [{"type": t, "description": d, "required": r, "optional": o} for t, d, r, o in ACTIONS]}

    # --- Profiller ---

    @app.get("/profiles")
    async def profiles():
        return [summary(p) for p in core.repo.all()]

    @app.post("/profiles")
    async def create(name: str = Body(...), short: str = Body(...), config: dict = Body(...)):
        return _plain(core.service.create(name, short, ProfileConfig.from_dict(config)))

    # Sürümün sonucu, yürürlükte kaldığı aralıktaki karar ve işlem sayısıyla
    # net kâr ve zarardır. Aralık sürümün created_at'inden bir sonrakinin
    # created_at'ine kadardır, işlem çıkış zamanıyla aralığa atanır. Yalnızca
    # şimdiki hesap sayılır, sıfırlamadan önceki kararlar ve işlemler girmez.
    def results(p, versions) -> dict[int, dict]:
        since = core.ledger.con.execute("SELECT created_at FROM accounts WHERE id = ?",
                                        (p.account_id,)).fetchone()[0] if p.account_id else 0
        decisions = dict(core.journal.con.execute(
            "SELECT version_id, COUNT(*) FROM journal WHERE profile_id = ? AND time >= ? GROUP BY version_id",
            (p.id, since)).fetchall())
        trades = core.ledger.trades(p.account_id) if p.account_id else ()
        out = {}
        for i, v in enumerate(versions):
            end = versions[i + 1].created_at if i + 1 < len(versions) else None
            mine = [t for t in trades if t.exit_time >= v.created_at and (end is None or t.exit_time < end)]
            out[v.id] = {"decisions": decisions.get(v.id, 0), "trades": len(mine),
                         "wins": sum(t.net > 0 for t in mine), "net": sum(t.net for t in mine)}
        return out

    @app.get("/profiles/{pid}")
    async def detail(pid: int):
        p = profile(pid)
        versions = core.repo.versions(pid)
        res = results(p, versions)
        return {**summary(p), "versions": [{**_plain(v), "result": res[v.id]} for v in versions],
                "memory": core.journal.memory(pid)}

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
            await core.interventions.add(pid, "command", {"type": "close_all"}, note="Durdururken kapatıldı.")
        return _plain(core.service.move(pid, "stop"))

    @app.post("/profiles/{pid}/clone")
    async def clone(pid: int, name: str = Body(...), short: str = Body(...)):
        profile(pid)
        return _plain(core.service.clone(pid, name, short))

    @app.post("/profiles/{pid}/reset")
    async def reset(pid: int):
        profile(pid)
        return _plain(core.service.reset_account(pid))

    @app.post("/profiles/{pid}/test")
    async def set_test(pid: int, test: bool = Body(..., embed=True)):
        profile(pid)
        return _plain(core.service.set_test(pid, test))

    @app.post("/publish")
    async def publish(profile_id: int | None = Body(None, embed=True)):
        core.service.publish(profile_id)
        return {"published": profile_id}

    # --- Hesap ve kayıtlar ---

    @app.get("/profiles/{pid}/account")
    async def account(pid: int):
        r = runner(pid)
        state = r.broker.state()
        # available bir property'dir, asdict onu almaz. leverages profilin
        # bütün paritelerini taşır, ayarlanmamış paritenin varsayılanını panel
        # bilmek zorunda kalmaz.
        st = {**_plain(state), "available": state.available,
              "leverages": {s: state.leverage(s) for s in sorted({*r.symbols, *state.leverages})}}
        # Pozisyonun açık kârı ve büyüklüğü son fiyatla burada hesaplanır,
        # arayüzler hesap kuralı taşımaz. Fiyat henüz gelmediyse None'dır.
        # Kapanan parçalar pozisyonun kimliğini taşır, realized onların
        # netidir. outlook koruma emirleri tetiklenirse ne olacağıdır, emri
        # olmayan adım likidasyondur.
        closed = [t for t in core.ledger.trades(state.account_id)
                  if t.position_id in {p.id for p in state.positions}]
        outlook = {p.id: _plain(r.broker.outlook(p)) for p in state.positions}
        for x in st["positions"]:
            price = r.prices.get(x["symbol"])
            x["open_pnl"] = (rules.unrealized_pnl(Side(x["side"]), x["entry_price"], x["qty"], price)
                             if price is not None else None)
            x["notional"] = x["qty"] * price if price is not None else None
            mine = [t for t in closed if t.position_id == x["id"]]
            x["realized"], x["realized_count"] = sum(t.net for t in mine), len(mine)
            x["outlook"] = outlook[x["id"]]
        return {"state": st, "prices": r.prices, "alerts": _plain(r.alerts.alerts),
                "triggers": [t.snapshot() for t in r.triggers if t.snapshot() is not None],
                "restrictions": core.interventions.active(pid, core.clock.time())}

    @app.get("/profiles/{pid}/trades")
    async def trades(pid: int):
        p = profile(pid)
        # net ve leverage property'dir, asdict onları almaz. Arayüzler kendisi hesaplamasın diye eklenir.
        return [{**_plain(t), "net": t.net, "leverage": t.leverage} for t in reversed(core.ledger.trades(p.account_id))] if p.account_id else []

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

    # Kayıt başka bir profilinse bulunamamış sayılır. Herkese açık API bu uca
    # yayındaki profilin kimliğiyle gelir, başka profilin araçlarını açmamalı.
    @app.get("/profiles/{pid}/journal/{jid}/tools")
    async def tools(pid: int, jid: int):
        profile(pid)
        if core.journal.profile_of(jid) != pid:
            raise HTTPException(404, "Kayıt bulunamadı.")
        return core.journal.tool_calls(jid)

    # --- Piyasa ---

    @app.get("/candles")
    async def candles(symbol: str, interval: str = "15m", limit: int = 300):
        if symbol not in core.catalog.symbols:
            raise ValueError(f"Bilinmeyen parite: {symbol}")
        if interval not in INTERVALS:
            raise ValueError(f"Bilinmeyen aralık: {interval}")
        return _plain(await core.supervisor.market.candles(symbol, interval, min(limit, 1000)))

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

    # Onay kartı göndermeden önce girişin tahminini buradan alır. Komut bir
    # yönetici komutu gibi kapıdan geçer, reddedilecekse nedeni 400 ile döner.
    # Not defterini yönetici düzeltir. Opus karar verirken yazma reddedilir:
    # süren çağrı eski defteri okudu, bitince kendi defterini yazıp düzeltmeyi
    # silerdi. before panelin düzenlemeye başladığı metindir. Defter o arada
    # yeni bir kararla değiştiyse yazma reddedilir, yoksa Opus'un yeni defteri
    # ezilirdi. Eski metin sistem olayının ayrıntısında kalır, geri alınabilir.
    @app.post("/profiles/{pid}/memory")
    async def edit_memory(pid: int, notebook: str = Body(...), before: str = Body(...)):
        p = profile(pid)
        key = core.repo.version(p.version_id).config.strategy if p.version_id else None
        entry = STRATEGIES.entries.get(key)
        if entry is None or not getattr(entry.cls, "uses_model", False):
            raise ValueError("Bu profilin stratejisi not defteri tutmaz.")
        r = core.supervisor.runners.get(pid)
        if r is not None and r.busy:
            raise ValueError("Opus şu an karar veriyor, karar bitince yeniden dene.")
        old = core.journal.memory(pid)
        if old.get("notebook", "") != before:
            raise ValueError("Defter sen düzenlerken yeni bir kararla değişti, düzeltme kaydedilmedi.")
        core.journal.set_memory(pid, {**old, "notebook": notebook})
        core.state.system_event(pid, core.clock.time(), "memory_edit", "Not defteri yönetici tarafından değiştirildi.",
                                {"before": old.get("notebook", "")})
        return {"memory": core.journal.memory(pid)}

    @app.post("/profiles/{pid}/preview")
    async def preview(pid: int, params: dict = Body(..., embed=True)):
        cmds, gate, now = commands_from(params), runner(pid).gate, core.clock.time()
        # Önizlenen zincir yalnızca kaldıraç ve ardından aynı paritenin
        # girişi olabilir, kaldıraç girişin tahminine katılır.
        leverage = None
        if len(cmds) == 2 and isinstance(cmds[0], SetLeverage) and getattr(cmds[1], "symbol", None) == cmds[0].symbol:
            leverage, cmds = cmds[0].leverage, cmds[1:]
        cmd = cmds[0]
        if len(cmds) == 1 and isinstance(cmd, ClosePosition) and leverage is None:
            return _plain(gate.preview_close(cmd, Actor.ADMIN, now))
        if len(cmds) > 1 or not isinstance(cmd, PlaceOrder) or cmd.reduce:
            raise ValueError("Önizleme yalnızca giriş emri, kaldıraçla birlikte giriş emri ve kapatma için yapılır.")
        return _plain(gate.preview(cmd, Actor.ADMIN, now, leverage))

    @app.post("/profiles/{pid}/interventions/{iid}/withdraw")
    async def withdraw(pid: int, iid: int):
        profile(pid)
        return core.interventions.withdraw(pid, iid)

    # --- Bildirimler ---

    # opus-web çekirdeğe ulaşamadığı süreyi çekirdek geri gelince bu uçla
    # kalıcı bildirim olarak yazar.
    @app.post("/notifications")
    async def notify(level: str = Body(...), kind: str = Body(...), title: str = Body(...), body: str = Body(""),
                     profile_id: int | None = Body(None)):
        if profile_id is not None:
            profile(profile_id)
        return await core.notifier.push(level, kind, profile_id, title, body)

    @app.get("/notifications")
    async def notifications(profile_id: int | None = None, unread: bool = False, before: int | None = None,
                            limit: int = 100):
        return core.notifications.list(profile_id, unread, before, min(limit, 500))

    @app.get("/notifications/counts")
    async def notification_counts():
        return core.notifications.counts()

    # Okundu yapılınca sayılar akışla bütün açık panellere gider, böylece
    # başka bir cihazdaki baloncuk da hemen söner.
    @app.post("/notifications/read")
    async def read(ids: list[int] | None = Body(None), profile_id: int | None = Body(None),
                   all: bool = Body(False)):
        changed = core.notifications.mark_read(core.clock.time(), ids, profile_id, all)
        counts = core.notifications.counts()
        core.bus.publish({"notification_counts": counts})
        return {"changed": changed, "counts": counts}

    @app.get("/notifications/settings")
    async def notification_settings():
        return core.state.setting("notification_settings", DEFAULT_SETTINGS)

    @app.post("/notifications/settings")
    async def save_notification_settings(settings: dict = Body(..., embed=True)):
        clean = settings_from(settings)
        core.state.set_setting("notification_settings", clean)
        return clean

    # --- Sistem ---

    @app.get("/health")
    async def health():
        return {"ok": True, "time": core.clock.time()}

    def _detail(e: dict) -> dict:
        return {**e, "detail": json.loads(e["detail"]) if e["detail"] else None}

    # Sistem olayları iki kaynaktan gelir. system_events profillerin karar
    # hatalarını tutar, onlar errors'ta ayrı durur. Akış, toplayıcı ve çekirdek
    # kesintileri profilsiz bildirim olarak yazılır. events ikisini, karar
    # hataları dışında, zamana göre tek listede verir.
    @app.get("/system")
    async def system():
        rows = [_detail(e) for e in core.state.system_events(limit=300)]
        errors = [e for e in rows if e["kind"] == "decide_error"][:100]
        events = [{"time": e["time"], "kind": e["kind"], "level": "info", "title": e["message"],
                   "body": json.dumps(e["detail"], ensure_ascii=False) if e["detail"] else "",
                   "profile_id": e["profile_id"]} for e in rows if e["kind"] != "decide_error"]
        events += [{k: n[k] for k in ("time", "kind", "level", "title", "body", "profile_id")}
                   for n in core.notifications.system(limit=200)]
        events.sort(key=lambda e: e["time"], reverse=True)
        sup = core.supervisor
        return {"time": core.clock.time(), "model_closed_until": core.access.until(),
                "model_warning": core.access.warning(),
                "stream": {"active": bool(sup.runners), "down": sup.stream_down, "last_candle": sup.last_candle},
                "collector": {"last_seen": core.collector.last_seen(), "down": core.collector.down},
                "listeners": core.bus.listeners, "dropped_events": core.bus.dropped,
                "errors": errors, "events": events[:200]}

    @app.get("/usage")
    async def usage(days: int = 14):
        days = max(1, min(days, USAGE_DAYS))
        now = core.clock.time()
        rows = core.journal.usage_rows(now - days * 86400 - 86400)
        return summarize(rows, now, days)

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
