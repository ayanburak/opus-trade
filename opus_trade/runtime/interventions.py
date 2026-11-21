"""
file: interventions.py
importable: KINDS, command_from, limit_from, Interventions

Yöneticinin profillere müdahaleleri. Opus'a giden müdahale bir metindir,
teklif ya da kesin. Yanında Şimdi bak, grafikten gelen kesin hesap işlemleri,
alarm ve panelin ana kartında görünmeyen kısıtlar durur.

Interventions.add() bir müdahaleyi doğrular, kesinse o anda uygular ve
kaydeder. withdraw() stratejiye henüz gösterilmemiş bir müdahaleyi geri çeker.
restriction_source() bir profilin yürürlükteki kesin kısıtlarını kapıya,
feed() stratejiye gidecek satırları çalıştırıcıya verir. command_from() ve
limit_from() panelden gelen JSON'u komuta ve sınıra çevirir.

description: Yönetici müdahalelerinin doğrulanması, uygulanması, saklanması ve anlatımı.
"""

# NOTE: Ağırlık yalnızca mesajda ve kısıtta seçilir. Teklif stratejiye
# değerlendirmesi için gider, kesin mesaja uyması beklenir. Hesap işlemi her
# zaman kesindir, o anda admin olarak kapıdan geçer. Kesin bir kısıtı kapı
# uygular. Kesin bir işlem reddedilirse müdahale kaydedilmez, hata yöneticiye
# döner. Haber, soru, araştırma, planlı çağrı gibi ayrı türler yoktur,
# yönetici hepsini mesajın içinde söyler.
#
# NOTE: Mesaj ve teklif yalnızca yönetici mesajı okuyabilen bir stratejiye
# yapılabilir. Kural stratejisine yalnızca Şimdi bak, hesap işlemi, alarm ve
# kesin kısıt uygulanır.
#
# NOTE: Bir müdahale stratejiye başarılı bir kararın bağlamında gösterilince
# teslim edilmiş sayılır. Karar hata verirse bir sonraki kararda yine
# gösterilir. Teslim edilmemiş bir müdahale geri çekilebilir, kesin bir hesap
# işlemi ise uygulandığı için geri çekilemez.

import json
import sqlite3
from datetime import datetime

from ..domain import numbers
from ..domain.clock import HOME_TZ, Clock
from ..domain.errors import RuleError
from ..domain.models import Side, TpLevel
from ..domain.trading import (Actor, AddToPosition, CancelOrder, CloseAll, ClosePosition, Command, OpenPosition,
                              PlaceOrder, Ref, UpdateProtection)
from ..storage.runtime_state import StateRepo
from .gate import Restriction, RestrictionSource
from .limits import Limit, MaxLeverage, MaxMargin, MaxNotional, NoOpen, SideOnly
from .runner import describe

# Tür ve ağırlık isteyip istemediği. Hesap işleminin ağırlığı seçilmez, her
# zaman kesin kaydedilir.
KINDS = {"message": True, "look_now": False, "command": False, "alert": False, "restrict": True,
         "unrestrict": False}
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
    except TypeError:
        raise ValueError("Komutun alanlarından biri yanlış biçimde, örneğin tp_levels price ve pct taşıyan bir liste olmalı.") from None
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
        state: Alarmların deposu.
        clock: Saat.
        runner: Profil kimliğinden çalıştırıcıyı veren fonksiyon, çalıştırıcı yoksa None.
    """

    def __init__(self, con: sqlite3.Connection, state: StateRepo, clock: Clock, runner) -> None:
        self.con = con
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
        if (kind == "message" or weight == "offer") and not r.strategy.accepts_messages:
            raise ValueError("Bu profilin stratejisi yönetici mesajı almaz, yalnızca grafikten işlem yapılabilir.")
        if kind == "message":
            return {"text": _text(params, "text")}
        if kind == "command":
            command_from(params)
            return params
        # Alarmda cancel bir numaradır. Yalnız verilirse o alarm kalkar, fiyatla
        # birlikte verilirse alarm taşınır: eskisi kalkar, yenisi kurulur.
        if kind == "alert":
            cancel = int(params["cancel"]) if params.get("cancel") is not None else None
            price = float(params.get("price") or 0)
            if cancel is not None and not price:
                return {"cancel": cancel}
            return {"symbol": _text(params, "symbol"), "price": price, "note": str(params.get("note") or ""),
                    "cancel": cancel}
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
        result, ends, delivered = None, None, None
        if kind == "command":
            weight = "firm"
            result = describe(await r.gate.submit(command_from(params), Actor.ADMIN, now))
        elif kind == "alert":
            old = params.get("cancel")
            if old is not None and old not in {a.number for a in r.alerts.alerts}:
                raise ValueError(f"#{old} numaralı alarm yok.")
            if "price" in params and params["symbol"] not in r.prices:
                raise RuleError(f"{params['symbol']} fiyatı henüz gelmedi.")
            # Yeni seviye önce denenir, geçersizse eski alarm yerinde kalır.
            a = None
            if "price" in params:
                a = r.alerts.add(params["symbol"], params["price"], r.prices[params["symbol"]], params["note"],
                                 str(Actor.ADMIN), now)
            if old is not None:
                r.alerts.cancel(old)
                self.state.resolve_alert(pid, old, "cancelled", now)
            if a is not None:
                self.state.add_alert(pid, a)
            result = (f"alarm #{old} kaldırıldı." if a is None else f"alarm #{a.number} kuruldu."
                      if old is None else f"alarm #{old} kaldırıldı, yerine alarm #{a.number} kuruldu.")
        elif kind == "unrestrict":
            with self.con:
                self.con.execute("UPDATE interventions SET removed_at = ? WHERE id = ?", (now, params["id"]))
        elif kind == "restrict":
            ends = params["until"]
        # Mesaj alamayan stratejide müdahale anlatılmaz, kaydedildiği an teslim sayılır.
        if not r.strategy.accepts_messages:
            delivered = now
        with self.con:
            cur = self.con.execute(
                "INSERT INTO interventions (profile_id, created_at, kind, weight, params, note, call_now, ends_at, "
                "delivered_at, result) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (pid, now, kind, weight, json.dumps(params, ensure_ascii=False), note.strip(), int(call_now or
                 kind == "look_now"), ends, delivered, result))
        # Şimdi bak kendisi bir çağrıdır, call_now beklemeden uyandırır.
        if (call_now or kind == "look_now") and r.strategy.accepts_messages:
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

    # --- Stratejiye anlatım ---

    # Mesaja cevap gerekip gerekmediğini yönetici metnin içinde söyler, bu
    # yüzden cevap alanı her mesajda hatırlatılır.
    def _body(self, r: dict) -> str:
        k, c, w = r["kind"], r["params"], r["weight"]
        if k == "message":
            if w == "firm":
                return (f"Yöneticinin kesin talimatı: {c['text']} Buna uy. Cevap gerekiyorsa yoneticiye_cevap "
                        f"alanına yaz.")
            return (f"Yöneticinin teklifi: {c['text']} Değerlendir, mantıklı bulursan uygula. Cevap gerekiyorsa "
                    f"yoneticiye_cevap alanına yaz.")
        if k == "look_now":
            return "Yönetici duruma bakmanı istiyor."
        if k == "command":
            return f"Yöneticinin kesin kararı uygulandı: {r['result']}"
        if k == "alert":
            if "price" not in c:
                return f"Yönetici #{c['cancel']} numaralı alarmı kaldırdı."
            moved = f"#{c['cancel']} numaralı alarmı kaldırıp " if c.get("cancel") is not None else ""
            return (f"Yönetici {moved}{c['symbol']} {numbers.price(c['price'])} seviyesine alarm kurdu. "
                    f"Not: {c['note'] or 'yok'}")
        if k == "restrict":
            text = limit_from(c["limit"]).text()
            until = f", {_clock(c['until'])} saatine kadar" if c.get("until") else ", kaldırılana kadar"
            if w == "firm":
                return f"Yönetici #{r['id']} numaralı kesin bir kısıt koydu: {text}{until}. Bu kısıtı aşan eylemlerin reddedilir."
            return f"Yöneticinin #{r['id']} numaralı kısıt teklifi: {text}{until}. Değerlendir, mantıklı bulursan uy."
        return f"Yönetici #{c['id']} numaralı kısıtı kaldırdı."

    def feed(self, pid: int) -> tuple[dict[str, list[str]], list[int]]:
        """Stratejiye gidecek yönetici bölümünü ve gösterilecek müdahalelerin kimliklerini döndürür."""
        now = self.clock.time()
        rows = [_row(r) for r in self.con.execute(
            "SELECT * FROM interventions WHERE profile_id = ? AND delivered_at IS NULL AND withdrawn_at IS NULL "
            "ORDER BY id", (pid,))]
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
