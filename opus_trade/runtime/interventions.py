"""
file: interventions.py
importable: KINDS, command_from, limit_from, describe_command, Interventions

Yöneticinin profillere müdahaleleri. Interventions.add() bir müdahaleyi
doğrular, kesinse o anda uygular ve kaydeder. withdraw() stratejiye henüz
gösterilmemiş bir müdahaleyi geri çeker. restrictions() bir profilin
yürürlükteki kesin kısıtlarını kapıya, feed() stratejiye gidecek satırları
çalıştırıcıya verir. tick() zamanı gelen planlı çağrıları uyandırır.
command_from() ve limit_from() panelden gelen JSON'u komuta ve sınıra çevirir.

description: Yönetici müdahalelerinin doğrulanması, uygulanması, saklanması ve anlatımı.
"""

# NOTE: Ağırlık iki türlüdür. Teklif stratejiye değerlendirmesi için gider ve
# hiçbir şey uygulanmaz. Kesin bir hesap işlemi o anda admin olarak kapıdan
# geçer, kesin bir kısıtı kapı uygular. Kesin bir işlem reddedilirse müdahale
# kaydedilmez, hata yöneticiye döner.
#
# NOTE: Mesajlar, teklifler ve not defteri yalnızca yönetici mesajı okuyabilen
# bir stratejiye yapılabilir. Kural stratejisine yalnızca kesin işlem, alarm ve
# kesin kısıt uygulanır.
#
# NOTE: Bir müdahale stratejiye başarılı bir kararın bağlamında gösterilince
# teslim edilmiş sayılır. Karar hata verirse bir sonraki kararda yine
# gösterilir. Teslim edilmemiş bir müdahale geri çekilebilir, kesin bir hesap
# işlemi ise uygulandığı için geri çekilemez.

import json
import sqlite3
from datetime import datetime

from ..domain.clock import HOME_TZ, Clock
from ..domain.errors import RuleError
from ..domain.models import Side, TpLevel
from ..domain.trading import (Actor, AddToPosition, CancelOrder, CloseAll, ClosePosition, Command, OpenPosition,
                              PlaceOrder, Ref, UpdateProtection)
from ..storage.journal import JournalRepo
from ..storage.runtime_state import StateRepo
from .gate import Restriction, RestrictionSource
from .limits import Limit, MaxLeverage, MaxMargin, MaxNotional, NoOpen, SideOnly
from .runner import describe

# Tür ve ağırlık isteyip istemediği. Mesaj türleri yalnızca LLM stratejisinedir.
KINDS = {"look_now": False, "look_symbol": False, "call_at": False, "news": False, "research": False,
         "info": False, "question": False, "command": True, "alert": False, "restrict": True,
         "unrestrict": False, "notebook": True}
_MESSAGES = {"look_now", "look_symbol", "call_at", "news", "research", "info", "question", "notebook"}
_WEIGHTS = ("offer", "firm")


def _clock(t: int) -> str:
    return datetime.fromtimestamp(t, HOME_TZ).strftime("%d.%m %H:%M")


def _text(params: dict, key: str) -> str:
    value = str(params.get(key) or "").strip()
    if not value:
        raise ValueError(f"{key} alanı boş olamaz.")
    return value


def _levels(spec: dict) -> tuple[TpLevel, ...] | None:
    if spec.get("tp_levels") is None:
        return None
    return tuple(TpLevel(float(k["price"]), float(k["pct"])) for k in spec["tp_levels"])


def _ref(spec: dict) -> Ref:
    return Ref(int(spec["number"]), str(spec["symbol"]), Side(spec["side"]))


def command_from(spec: dict) -> Command:
    """Panelin JSON komutunu bir broker komutuna çevirir.

    Raises:
        ValueError: Tür bilinmiyorsa ya da bir alan eksik veya geçersizse.
    """
    try:
        match spec.get("type"):
            case "open":
                return OpenPosition(spec["symbol"], Side(spec["side"]), float(spec["leverage"]),
                                    float(spec["margin"]), _levels(spec) or (), spec.get("sl"), spec.get("trail_pct"))
            case "place_order":
                return PlaceOrder(spec["symbol"], Side(spec["side"]), float(spec["trigger"]), float(spec["leverage"]),
                                  float(spec["margin"]), _levels(spec) or (), spec.get("sl"), spec.get("trail_pct"),
                                  spec.get("valid_min"), bool(spec.get("taker", False)))
            case "cancel_order":
                return CancelOrder(_ref(spec))
            case "add":
                return AddToPosition(_ref(spec), float(spec["leverage"]), float(spec["margin"]))
            case "close":
                return ClosePosition(_ref(spec), float(spec.get("pct", 100)))
            case "protect":
                return UpdateProtection(_ref(spec), _levels(spec), spec.get("sl"), spec.get("trail_pct"))
            case "close_all":
                return CloseAll()
    except KeyError as k:
        raise ValueError(f"{k.args[0]} alanı eksik.") from None
    raise ValueError(f"Bilinmeyen komut: {spec.get('type')}")


def limit_from(spec: dict) -> Limit:
    """Panelin JSON kısıtını bir sınıra çevirir.

    Raises:
        ValueError: Tür bilinmiyorsa ya da değer geçersizse.
    """
    kind, symbol = spec.get("type"), spec.get("symbol") or None
    if kind == "no_open":
        return NoOpen(symbol)
    if kind == "side_only":
        if not symbol:
            raise ValueError("Yön kısıtı için parite seçilmeli.")
        return SideOnly(Side(spec["side"]), symbol)
    value = float(spec.get("value") or 0)
    if value <= 0:
        raise ValueError("Kısıtın değeri sıfırdan büyük olmalı.")
    if kind == "max_leverage":
        return MaxLeverage(value, symbol)
    if kind == "max_margin":
        return MaxMargin(value, bool(spec.get("total")), symbol)
    if kind == "max_notional":
        return MaxNotional(value, symbol)
    raise ValueError(f"Bilinmeyen kısıt: {kind}")


def _protection(cmd) -> str:
    parts = []
    if cmd.tp_levels:
        parts.append("TP " + ", ".join(f"{k.price:g} yüzde {k.pct:g}" for k in cmd.tp_levels))
    elif cmd.tp_levels == () and isinstance(cmd, UpdateProtection):
        parts.append("TP kaldırılsın")
    if cmd.sl is not None:
        parts.append(f"SL {cmd.sl:g}" if cmd.sl else "SL kaldırılsın")
    if cmd.trail_pct is not None:
        parts.append(f"iz süren stop yüzde {cmd.trail_pct:g}" if cmd.trail_pct else "iz süren stop kaldırılsın")
    return ", ".join(parts)


# Yöneticinin teklifleri stratejiye uygulanmadan, bu cümleyle gider. Anlatım
# eylem alanlarının adlarını değil işin kendisini söyler.
def describe_command(cmd: Command) -> str:
    match cmd:
        case OpenPosition():
            extra = _protection(cmd)
            return f"{cmd.symbol} {cmd.side} {cmd.leverage:g}x {cmd.margin:g} dolar marjinle pozisyon aç" + \
                (f", {extra}." if extra else ".")
        case PlaceOrder():
            extra = _protection(cmd)
            kind = ", taker" if cmd.taker else ""
            return (f"{cmd.symbol} {cmd.side} {cmd.trigger:g} tetiğiyle {cmd.leverage:g}x {cmd.margin:g} dolar "
                    f"marjinli emir ver{kind}" + (f", {extra}." if extra else "."))
        case CancelOrder():
            return f"#{cmd.ref.number} {cmd.ref.symbol} {cmd.ref.side} emrini iptal et."
        case AddToPosition():
            return f"#{cmd.ref.number} {cmd.ref.symbol} {cmd.ref.side} pozisyonuna {cmd.leverage:g}x {cmd.margin:g} " \
                   f"dolar marjinle ekleme yap."
        case ClosePosition():
            part = "tamamını" if cmd.pct >= 100 else f"yüzde {cmd.pct:g} kadarını"
            return f"#{cmd.ref.number} {cmd.ref.symbol} {cmd.ref.side} pozisyonunun {part} kapat."
        case UpdateProtection():
            return f"#{cmd.ref.number} {cmd.ref.symbol} {cmd.ref.side} pozisyonunda {_protection(cmd) or 'korumayı değiştir'}."
    return "Bütün pozisyonları kapat ve bütün bekleyen emirleri iptal et."


def _row(r: sqlite3.Row) -> dict:
    return {**dict(r), "params": json.loads(r["params"])}


class _Restrictions(RestrictionSource):
    def __init__(self, owner: "Interventions", pid: int) -> None:
        self.owner, self.pid = owner, pid

    def restrictions(self, now: int) -> list[Restriction]:
        return [Restriction(r["id"], limit_from(r["params"]["limit"])) for r in self.owner.active(self.pid, now)
                if r["weight"] == "firm"]


class Interventions:
    """Bütün profillerin müdahaleleri.

    Args:
        con: Çekirdek veritabanı.
        journal: Not defterine kesin ek için hafıza deposu.
        state: Alarmların deposu.
        clock: Saat.
        runner: Profil kimliğinden çalıştırıcıyı veren fonksiyon, çalıştırıcı yoksa None.
    """

    def __init__(self, con: sqlite3.Connection, journal: JournalRepo, state: StateRepo, clock: Clock,
                 runner) -> None:
        self.con = con
        self.journal = journal
        self.state = state
        self.clock = clock
        self.runner = runner

    def get(self, iid: int) -> dict:
        r = self.con.execute("SELECT * FROM interventions WHERE id = ?", (iid,)).fetchone()
        if r is None:
            raise ValueError(f"{iid} numaralı müdahale yok.")
        return _row(r)

    def list(self, pid: int, limit: int = 300) -> list[dict]:
        return [_row(r) for r in self.con.execute(
            "SELECT * FROM interventions WHERE profile_id = ? ORDER BY id DESC LIMIT ?", (pid, limit))]

    def active(self, pid: int, now: int) -> list[dict]:
        return [_row(r) for r in self.con.execute(
            "SELECT * FROM interventions WHERE profile_id = ? AND kind = 'restrict' AND removed_at IS NULL "
            "AND withdrawn_at IS NULL AND (ends_at IS NULL OR ends_at > ?) ORDER BY id", (pid, now))]

    def restriction_source(self, pid: int) -> RestrictionSource:
        return _Restrictions(self, pid)

    # --- Kayıt ---

    def _validate(self, pid: int, kind: str, params: dict, weight: str | None, now: int) -> dict:
        if kind not in KINDS:
            raise ValueError(f"Bilinmeyen müdahale: {kind}")
        if KINDS[kind] and weight not in _WEIGHTS:
            raise ValueError("Bu müdahale için teklif ya da kesin seçilmeli.")
        if not KINDS[kind] and weight is not None:
            raise ValueError("Bu müdahalenin ağırlığı olmaz.")
        r = self.runner(pid)
        if r is None:
            raise ValueError("Profil taslak ya da arşivlenmiş, müdahale yapılamaz.")
        if (kind in _MESSAGES or weight == "offer") and not r.strategy.accepts_messages:
            raise ValueError("Bu profilin stratejisi yönetici mesajı almaz, yalnızca kesin işlem yapılabilir.")
        if kind == "look_symbol":
            return {"symbol": _text(params, "symbol")}
        if kind == "call_at":
            at = int(params.get("at") or 0)
            if at <= now:
                raise ValueError("Çağrı zamanı ileride olmalı.")
            return {"at": at}
        if kind == "news":
            return {"text": _text(params, "text"), "url": str(params.get("url") or "").strip()}
        if kind in ("research", "info", "question", "notebook"):
            return {"text": _text(params, "text")}
        if kind == "command":
            command_from(params)
            return params
        if kind == "alert":
            return {"symbol": _text(params, "symbol"), "price": float(params.get("price") or 0),
                    "note": str(params.get("note") or "")}
        if kind == "restrict":
            limit_from(params.get("limit") or {})
            until = params.get("until")
            if until is not None and int(until) <= now:
                raise ValueError("Kısıtın bitişi ileride olmalı.")
            return {"limit": params["limit"], "until": int(until) if until is not None else None}
        if kind == "unrestrict":
            target = int(params.get("id") or 0)
            if not any(x["id"] == target for x in self.active(pid, now)):
                raise ValueError("Bu numarada yürürlükte bir kısıt yok.")
            return {"id": target}
        return {}

    async def add(self, pid: int, kind: str, params: dict, weight: str | None = None, note: str = "",
                  call_now: bool = False) -> dict:
        """Bir müdahaleyi doğrular, kesinse uygular ve kaydeder.

        Raises:
            ValueError: Girdi geçersizse ya da profilin stratejisi bu müdahaleyi almıyorsa.
            RuleError: Kesin bir işlem hesap kurallarına ya da risk sınırlarına takılırsa.
        """
        now = self.clock.time()
        params = self._validate(pid, kind, params, weight, now)
        r = self.runner(pid)
        result, due, ends, delivered = None, None, None, None
        if kind == "command" and weight == "firm":
            result = describe(await r.gate.submit(command_from(params), Actor.ADMIN, now))
        elif kind == "alert":
            if params["symbol"] not in r.prices:
                raise RuleError(f"{params['symbol']} fiyatı henüz gelmedi.")
            a = r.alerts.add(params["symbol"], params["price"], r.prices[params["symbol"]], params["note"],
                             str(Actor.ADMIN), now)
            self.state.add_alert(pid, a)
            result = f"alarm #{a.number} kuruldu."
        elif kind == "notebook" and weight == "firm":
            mem = self.journal.memory(pid)
            current = mem.get("notebook", "")
            mem["notebook"] = (current + "\n\n" if current else "") + "Yöneticinin notu: " + params["text"]
            self.journal.set_memory(pid, mem)
        elif kind == "unrestrict":
            with self.con:
                self.con.execute("UPDATE interventions SET removed_at = ? WHERE id = ?", (now, params["id"]))
        elif kind == "call_at":
            due = params["at"]
        elif kind == "restrict":
            ends = params["until"]
        # Mesaj alamayan stratejide müdahale anlatılmaz, kaydedildiği an teslim sayılır.
        if not r.strategy.accepts_messages:
            delivered = now
        with self.con:
            cur = self.con.execute(
                "INSERT INTO interventions (profile_id, created_at, kind, weight, params, note, call_now, due_at, "
                "ends_at, delivered_at, result) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (pid, now, kind, weight, json.dumps(params, ensure_ascii=False), note.strip(), int(call_now), due,
                 ends, delivered, result))
        if call_now and r.strategy.accepts_messages:
            r.wake("admin")
        return self.get(cur.lastrowid)

    def withdraw(self, pid: int, iid: int) -> dict:
        """Stratejiye henüz gösterilmemiş bir müdahaleyi geri çeker.

        Raises:
            ValueError: Müdahale bu profilin değilse, gösterildiyse ya da uygulanmış kesin bir işlemse.
        """
        r = self.get(iid)
        if r["profile_id"] != pid:
            raise ValueError(f"{iid} numaralı müdahale bu profilin değil.")
        if r["delivered_at"] or r["withdrawn_at"]:
            raise ValueError("Bu müdahale zaten gösterildi ya da geri çekildi.")
        if r["result"] is not None:
            raise ValueError("Uygulanmış bir işlem geri çekilemez, ters işlemle düzeltilir.")
        with self.con:
            self.con.execute("UPDATE interventions SET withdrawn_at = ? WHERE id = ?", (self.clock.time(), iid))
        return self.get(iid)

    # Zamanı gelen planlı çağrılar bir kez uyandırır, fired_at ikinci kez
    # uyandırmayı önler. Teslim ayrıca karar başarılı olunca işaretlenir.
    def tick(self, now: int) -> None:
        due = self.con.execute("SELECT id, profile_id FROM interventions WHERE kind = 'call_at' AND fired_at IS NULL "
                               "AND withdrawn_at IS NULL AND due_at <= ?", (now,)).fetchall()
        for iid, pid in due:
            with self.con:
                self.con.execute("UPDATE interventions SET fired_at = ? WHERE id = ?", (now, iid))
            r = self.runner(pid)
            if r:
                r.wake("admin")

    # --- Stratejiye anlatım ---

    def _body(self, r: dict) -> str:
        k, c, w = r["kind"], r["params"], r["weight"]
        if k == "look_now":
            return "Yönetici duruma bakmanı istiyor."
        if k == "look_symbol":
            return f"Yönetici {c['symbol']} paritesine bakmanı istiyor."
        if k == "call_at":
            return "Yönetici seni bu saate çağırmıştı."
        if k == "news":
            link = f" Kaynak: {c['url']}" if c.get("url") else ""
            return f"Yönetici bir haber verdi: {c['text']}.{link} Habere bak, değerlendirmeni yoneticiye_cevap alanına yaz."
        if k == "research":
            return f"Yönetici şu konuyu araştırmanı istiyor: {c['text']}. Bulduklarını yoneticiye_cevap alanına yaz."
        if k == "info":
            return f"Yönetici bilgi veriyor: {c['text']}"
        if k == "question":
            return f"Yönetici soruyor: {c['text']} Cevabını yoneticiye_cevap alanına yaz."
        if k == "command":
            if w == "firm":
                return f"Yöneticinin kesin kararı uygulandı: {r['result']}"
            return f"Yöneticinin teklifi: {describe_command(command_from(c))} Değerlendir, mantıklı bulursan uygula."
        if k == "alert":
            return f"Yönetici {c['symbol']} {c['price']:g} seviyesine alarm kurdu. Not: {c['note'] or 'yok'}"
        if k == "restrict":
            text = limit_from(c["limit"]).text()
            until = f", {_clock(c['until'])} saatine kadar" if c.get("until") else ", kaldırılana kadar"
            if w == "firm":
                return f"Yönetici #{r['id']} numaralı kesin bir kısıt koydu: {text}{until}. Bu kısıtı aşan eylemlerin reddedilir."
            return f"Yöneticinin #{r['id']} numaralı kısıt teklifi: {text}{until}. Değerlendir, mantıklı bulursan uy."
        if k == "unrestrict":
            return f"Yönetici #{c['id']} numaralı kısıtı kaldırdı."
        if w == "firm":
            return f"Yönetici not defterine şunu ekledi: {c['text']}"
        return f"Yönetici not defterine şunu eklemeni öneriyor: {c['text']}"

    def feed(self, pid: int) -> tuple[dict[str, list[str]], list[int]]:
        """Stratejiye gidecek yönetici bölümünü ve gösterilecek müdahalelerin kimliklerini döndürür."""
        now = self.clock.time()
        rows = [_row(r) for r in self.con.execute(
            "SELECT * FROM interventions WHERE profile_id = ? AND delivered_at IS NULL AND withdrawn_at IS NULL "
            "AND (due_at IS NULL OR due_at <= ?) ORDER BY id", (pid, now))]
        items = []
        for r in rows:
            note = f" Yöneticinin açıklaması: {r['note']}" if r["note"] else ""
            items.append(f"{_clock(r['created_at'])}: {self._body(r)}{note}")
        restrictions = []
        for r in self.active(pid, now):
            until = f", {_clock(r['ends_at'])} saatine kadar" if r["ends_at"] else ""
            label = "kesin" if r["weight"] == "firm" else "teklif"
            restrictions.append(f"#{r['id']}, {label}: {limit_from(r['params']['limit']).text()}{until}.")
        return {"items": items, "restrictions": restrictions}, [r["id"] for r in rows]

    def delivered(self, ids: list[int]) -> None:
        with self.con:
            self.con.executemany("UPDATE interventions SET delivered_at = ? WHERE id = ?",
                                 [(self.clock.time(), i) for i in ids])
