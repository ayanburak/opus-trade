"""
file: usage.py
importable: summarize, WINDOW_S

summarize() günlükten okunan çağrı satırlarını gün gün ve son beş saatlik
pencerede, profil başına toplar. Her toplamda çağrı ve hata sayısı, token
sayıları, API liste fiyatıyla dolar karşılığı ve çağrıların süresi vardır.
Alt görevlerin tüketimi çağrısının profiline eklenir, sayısı sub_calls ve
sub_errors'ta ayrıca durur. models model başına çağrı sayısı ve dolar
karşılığıdır, ana çağrılar ve alt görevler birlikte.

    rows = journal.usage_rows(since)
    out = summarize(rows, now, days=14)

description: Abonelik tüketiminin günlükten hesaplanması.
"""

# NOTE: Aboneliğin kalan limiti hiçbir yerden okunamaz. Bu hesap yalnızca bu
# programın çağrılarının ne harcadığını söyler. Limit beş saatlik pencereyle
# işlediği için son beş saat ayrıca toplanır.
#
# NOTE: Dolar karşılığı claude komutunun bildirdiği total_cost_usd'dir,
# aboneliğe ayrıca ödenen bir para değildir. Hata veren çağrının tüketimi
# bilinmez, yalnızca hata olarak sayılır.
#
# NOTE: Alt görev aynı abonelikten düşer. Ana çağrının satırı hatalı olsa da
# alt görevleri tamamlanmış olabilir, onlar yine sayılır. Hiç başlamayan
# atlanmış görev sayılmaz.
#
# NOTE: Kural stratejisinin günlük satırında usage yoktur, çağrı sayılmaz.
# Hata verirse yine hata olarak görünür.

import json
from datetime import datetime, timedelta

from ..domain.clock import HOME_TZ

WINDOW_S = 5 * 3600

# Toplanan alanlar ve claude komutunun usage sözlüğündeki karşılıkları.
_TOKENS = (("input", "input_tokens"), ("output", "output_tokens"),
           ("cache_read", "cache_read_input_tokens"), ("cache_write", "cache_creation_input_tokens"))


def _empty() -> dict:
    return {"calls": 0, "errors": 0, **{k: 0 for k, _ in _TOKENS}, "cost_usd": 0.0, "duration_s": 0.0,
            "sub_calls": 0, "sub_errors": 0, "models": {}}


def _tokens(into: dict, usage: dict | None, cost: float | None) -> None:
    for k, src in _TOKENS:
        into[k] += int((usage or {}).get(src) or 0)
    into["cost_usd"] += cost or 0.0


def _model(into: dict, model: str | None, cost: float | None) -> None:
    m = into["models"].setdefault(model or "bilinmiyor", {"calls": 0, "cost_usd": 0.0})
    m["calls"] += 1
    m["cost_usd"] += cost or 0.0


def _add(into: dict, error: bool, usage: dict | None, cost: float | None, duration: float | None,
         model: str | None = None) -> None:
    if error:
        into["errors"] += 1
        return
    if usage is None and cost is None:
        return
    into["calls"] += 1
    _tokens(into, usage, cost)
    into["duration_s"] += duration or 0.0
    _model(into, model, cost)


# Alt görevin usage alanı istemcinin sözlüğüdür, tokenlar onun usage'ındadır.
def _add_sub(into: dict, rec: dict) -> None:
    if rec.get("skipped"):
        return
    if rec.get("error"):
        into["sub_errors"] += 1
        return
    into["sub_calls"] += 1
    _tokens(into, (rec.get("usage") or {}).get("usage"), rec.get("cost_usd"))
    _model(into, rec.get("model"), rec.get("cost_usd"))


def _bucket(groups: dict, pid: int) -> dict:
    return groups.setdefault(str(pid), _empty())


def summarize(rows: list[tuple], now: int, days: int) -> dict:
    """Çağrı satırlarını gün gün ve son beş saatte toplar.

    Args:
        rows: (profile_id, time, error, usage, cost_usd, duration_s, model, team) satırları. usage JSON metni ya da
            None, error hata olup olmadığıdır. model ve team verilmeyebilir, team alt görevlerin JSON metnidir.
        now: Şimdiki an.
        days: Kaç gün geriye. Bugün dahildir, kaydı olmayan gün de boş toplamla listede durur.

    Returns:
        days ve window alanlı sözlük. days yeniden eskiye günlerdir, her biri day, start, total ve
        profil kimliğine göre profiles taşır. window son beş saatin since, total ve profiles toplamıdır.
    """
    today = datetime.fromtimestamp(now, HOME_TZ).date()
    out_days = []
    index = {}
    for i in range(days):
        d = today - timedelta(days=i)
        start = int(datetime(d.year, d.month, d.day, tzinfo=HOME_TZ).timestamp())
        entry = {"day": d.isoformat(), "start": start, "total": _empty(), "profiles": {}}
        index[d] = entry
        out_days.append(entry)
    window = {"since": now - WINDOW_S, "total": _empty(), "profiles": {}}
    for pid, t, error, usage, cost, duration, *rest in rows:
        model = rest[0] if rest else None
        team = rest[1] if len(rest) > 1 else None
        team = json.loads(team) if isinstance(team, str) else (team or [])
        u = json.loads(usage) if isinstance(usage, str) else usage
        day = index.get(datetime.fromtimestamp(t, HOME_TZ).date())
        targets = [day["total"], _bucket(day["profiles"], pid)] if day else []
        if t >= window["since"]:
            targets += [window["total"], _bucket(window["profiles"], pid)]
        for target in targets:
            _add(target, bool(error), u, cost, duration, model)
            for rec in team:
                _add_sub(target, rec)
    return {"days": out_days, "window": window}
