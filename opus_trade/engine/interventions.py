"""
file: interventions.py
importable: add, pending, mark_delivered, due_calls, restrictions, lines, restriction_lines, KINDS, WEIGHTS

Yöneticinin müdahalelerini doğrular, uygular ve saklar. add() bir müdahaleyi
kaydeder. Kesin bir hesap işlemi ya da kesin bir not defteri eki o anda
uygulanır. pending() Opus'a henüz gösterilmemiş müdahaleleri, restrictions()
yürürlükteki kısıtları döndürür. lines() ve restriction_lines() bunları
Opus'un istemine girecek Türkçe satırlara çevirir. Başarılı bir çağrıdan sonra
mark_delivered() gösterilenleri işaretler. due_calls() zamanı gelen planlı
yönetici çağrılarını bir kez döndürür.

description: Yönetici müdahalelerinin kaydı, uygulanması ve Opus'a anlatımı.
"""

# NOTE: Ağırlık iki türlüdür. offer, yani teklif, Opus'a değerlendirmesi için
# gider ve hiçbir şey uygulanmaz. firm, yani kesin, hesap işleminde o anda
# admin kaynağıyla kapıdan geçer. Kısıtta ise kapı Opus'un aşan eylemlerini
# reddeder. Dikkat, haber ve bilgi müdahalelerinin ağırlığı yoktur.
#
# NOTE: Kesin bir hesap işlemi reddedilirse müdahale hiç kaydedilmez, hata
# yöneticiye döner. Opus'a yalnızca gerçekten uygulanmış işlemler bildirilir.
#
# NOTE: Bir müdahale Opus'a başarılı bir çağrının isteminde gösterildikten
# sonra teslim edilmiş sayılır. Çağrı hata verirse bir sonraki çağrıda yine
# gösterilir, böylece hiçbiri kaybolmaz.

import json
import sqlite3
import time

from ..db import get_state, rows, set_state
from .account import Account
from .commands import RESTRICTIONS, Command, describe, execute, restriction_text

# Müdahale türleri ve ağırlık isteyip istemedikleri.
KINDS = {
    "look_now": False, "look_symbol": False, "call_at": False,
    "news": False, "research": False, "info": False, "question": False,
    "command": True, "restrict": True, "unrestrict": False, "notebook": True,
}
WEIGHTS = ("offer", "firm")


def _clock(t: int) -> str:
    return time.strftime("%d.%m %H:%M", time.localtime(t))


def _text(params: dict, key: str) -> str:
    value = str(params.get(key) or "").strip()
    if not value:
        raise ValueError(f"{key} alanı boş olamaz.")
    return value


def _row(con: sqlite3.Connection, id_: int) -> dict:
    found = rows(con, "SELECT * FROM interventions WHERE id = ?", (id_,))
    if not found:
        raise ValueError(f"{id_} numaralı müdahale yok.")
    r = found[0]
    r["params"] = json.loads(r["params"])
    return r


# --- Kayıt ---

# Dışarıdan, admin panelinden gelen girdi burada doğrulanır. Hatalar
# ValueError ya da kesin işlemde AccountError olarak yöneticiye döner.
def _validate(con: sqlite3.Connection, kind: str, params: dict, weight: str | None, now: int) -> dict:
    if kind not in KINDS:
        raise ValueError(f"Bilinmeyen müdahale: {kind}")
    if KINDS[kind] and weight not in WEIGHTS:
        raise ValueError("Bu müdahale için teklif ya da kesin seçilmeli.")
    if not KINDS[kind] and weight is not None:
        raise ValueError("Bu müdahalenin ağırlığı olmaz.")
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
        cmd = Command(params.get("kind", ""), dict(params.get("params") or {}), "admin")
        try:
            describe(cmd)
        except KeyError as k:
            raise ValueError(f"{k} parametresi eksik.")
        return {"kind": cmd.kind, "params": cmd.params}
    if kind == "restrict":
        r = {"kind": params.get("kind"), "params": dict(params.get("params") or {})}
        if r["kind"] not in RESTRICTIONS:
            raise ValueError(f"Bilinmeyen kısıt: {r['kind']}")
        try:
            restriction_text(r)
        except KeyError as k:
            raise ValueError(f"{k} parametresi eksik.")
        if "value" in r["params"] and not r["params"]["value"] > 0:
            raise ValueError("Kısıtın değeri sıfırdan büyük olmalı.")
        until = params.get("until")
        if until is not None and int(until) <= now:
            raise ValueError("Kısıtın bitişi ileride olmalı.")
        return {**r, "until": int(until) if until is not None else None}
    if kind == "unrestrict":
        target = _row(con, int(params.get("id") or 0))
        if target["kind"] != "restrict" or target["removed_at"] or (target["ends_at"] or now + 1) <= now:
            raise ValueError("Bu numarada yürürlükte bir kısıt yok.")
        return {"id": target["id"]}
    return {}


def add(con: sqlite3.Connection, acc: Account, prices: dict[str, float], kind: str, params: dict,
        weight: str | None, note: str, call_now: bool, now: int) -> dict:
    """Bir müdahaleyi doğrular, kesinse uygular ve kaydeder.

    Args:
        con: Profilin veritabanı.
        acc: Profilin hesabı, kesin hesap işlemleri buna uygulanır.
        prices: Paritelerin güncel fiyatları.
        kind: KINDS içinden müdahalenin türü.
        params: Türe göre parametreler. command için kind ve params, restrict için
            kind, params ve isteğe bağlı until.
        weight: offer ya da firm. Ağırlığı olmayan türlerde None.
        note: Yöneticinin isteğe bağlı açıklaması.
        call_now: True ise Opus hemen çağrılır. Çağırmak zamanlayıcının işidir.
        now: Unix saniye.

    Returns:
        Kaydedilen müdahale.

    Raises:
        ValueError: Girdi geçersizse.
        AccountError: Kesin bir hesap işlemi hesap kurallarına takılırsa.
    """
    params = _validate(con, kind, params, weight, now)
    result, due, ends = None, None, None
    if kind == "command" and weight == "firm":
        result = execute(acc, prices, Command(params["kind"], params["params"], "admin"), now)
    elif kind == "notebook" and weight == "firm":
        current = get_state(con, "notebook", "")
        set_state(con, "notebook", (current + "\n\n" if current else "") + "Yöneticinin notu: " + params["text"])
    elif kind == "unrestrict":
        con.execute("UPDATE interventions SET removed_at = ? WHERE id = ?", (now, params["id"]))
    elif kind == "call_at":
        due = params["at"]
    elif kind == "restrict":
        ends = params["until"]
    cur = con.execute(
        "INSERT INTO interventions (created_at, kind, weight, params, note, call_now, due_at, ends_at, result) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (now, kind, weight, json.dumps(params, ensure_ascii=False), note.strip(), int(call_now), due, ends, result),
    )
    con.commit()
    return _row(con, cur.lastrowid)


def pending(con: sqlite3.Connection, now: int) -> list[dict]:
    """Opus'a henüz gösterilmemiş müdahaleleri eskiden yeniye döndürür.

    Planlı bir çağrı zamanı gelmeden gösterilmez.
    """
    found = rows(con, "SELECT * FROM interventions WHERE delivered_at IS NULL AND (due_at IS NULL OR due_at <= ?) "
                      "ORDER BY id", (now,))
    for r in found:
        r["params"] = json.loads(r["params"])
    return found


def mark_delivered(con: sqlite3.Connection, ids: list[int], now: int) -> None:
    con.executemany("UPDATE interventions SET delivered_at = ? WHERE id = ?", [(now, i) for i in ids])
    con.commit()


# Zamanlayıcı birkaç saniyede bir sorar. fired_at işareti aynı planlı çağrının
# ikinci kez tetiklenmesini önler, teslim ise çağrı başarılı olunca ayrıca işaretlenir.
def due_calls(con: sqlite3.Connection, now: int) -> list[int]:
    """Zamanı gelmiş ve henüz tetiklenmemiş planlı çağrıları işaretleyip numaralarını döndürür."""
    ids = [r["id"] for r in rows(con, "SELECT id FROM interventions WHERE kind = 'call_at' AND fired_at IS NULL "
                                       "AND due_at <= ?", (now,))]
    con.executemany("UPDATE interventions SET fired_at = ? WHERE id = ?", [(now, i) for i in ids])
    con.commit()
    return ids


def restrictions(con: sqlite3.Connection, now: int, firm_only: bool = False) -> list[dict]:
    """Yürürlükteki kısıtları döndürür.

    Returns:
        id, weight, kind, params ve ends_at anahtarlı kayıtlar. kind kısıtın kendi türüdür.
    """
    found = rows(con, "SELECT * FROM interventions WHERE kind = 'restrict' AND removed_at IS NULL "
                      "AND (ends_at IS NULL OR ends_at > ?) ORDER BY id", (now,))
    out = []
    for r in found:
        if firm_only and r["weight"] != "firm":
            continue
        p = json.loads(r["params"])
        out.append({"id": r["id"], "weight": r["weight"], "kind": p["kind"], "params": p["params"],
                    "ends_at": r["ends_at"]})
    return out


# --- Opus'a anlatım ---

def _body(r: dict) -> str:
    k, c, w = r["kind"], r["params"], r["weight"]
    if k == "look_now":
        return "Yönetici duruma bakmanı istiyor."
    if k == "look_symbol":
        return f"Yönetici {c['symbol']} paritesine bakmanı istiyor."
    if k == "call_at":
        return "Yönetici seni bu saate çağırmıştı."
    if k == "news":
        link = f" Kaynak: {c['url']}" if c.get("url") else ""
        return (f"Yönetici bir haber verdi: {c['text']}.{link} Habere bak, değerlendirmeni yoneticiye_cevap "
                f"alanına yaz.")
    if k == "research":
        return f"Yönetici şu konuyu araştırmanı istiyor: {c['text']}. Bulduklarını yoneticiye_cevap alanına yaz."
    if k == "info":
        return f"Yönetici bilgi veriyor: {c['text']}"
    if k == "question":
        return f"Yönetici soruyor: {c['text']} Cevabını yoneticiye_cevap alanına yaz."
    if k == "command":
        if w == "firm":
            return f"Yöneticinin kesin kararı uygulandı: {r['result']}"
        return (f"Yöneticinin teklifi: {describe(Command(c['kind'], c['params'], 'admin'))} Değerlendir, mantıklı "
                f"bulursan uygula.")
    if k == "restrict":
        until = f", {_clock(c['until'])} saatine kadar" if c.get("until") else ", dönem sonuna ya da kaldırılana kadar"
        text = restriction_text(c)
        if w == "firm":
            return f"Yönetici #{r['id']} numaralı kesin bir kısıt koydu: {text}{until}. Bu kısıtı aşan eylemlerin reddedilir."
        return f"Yöneticinin #{r['id']} numaralı kısıt teklifi: {text}{until}. Değerlendir, mantıklı bulursan uy."
    if k == "unrestrict":
        return f"Yönetici #{c['id']} numaralı kısıtı kaldırdı."
    if w == "firm":
        return f"Yönetici not defterine şunu ekledi: {c['text']}"
    return f"Yönetici not defterine şunu eklemeni öneriyor: {c['text']}"


def lines(items: list[dict]) -> list[str]:
    """Müdahaleleri Opus'un istemine girecek Türkçe satırlara çevirir."""
    out = []
    for r in items:
        note = f" Yöneticinin açıklaması: {r['note']}" if r["note"] else ""
        out.append(f"{_clock(r['created_at'])}: {_body(r)}{note}")
    return out


def restriction_lines(found: list[dict]) -> list[str]:
    """Yürürlükteki kısıtları istem için Türkçe satırlara çevirir."""
    out = []
    for r in found:
        until = f", {_clock(r['ends_at'])} saatine kadar" if r["ends_at"] else ""
        label = "kesin" if r["weight"] == "firm" else "teklif"
        out.append(f"#{r['id']}, {label}: {restriction_text(r)}{until}.")
    return out
