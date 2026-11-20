"""
file: test_usage.py
importable: yok

Abonelik tüketimi hesabının testleri. Hesap yanlışsa Sistem sayfası limitin
hangi profilde ve hangi gün tükendiğini yanlış gösterir. Gün sınırı
İstanbul saatine göredir, son beş saat ayrıca toplanır.

description: runtime.usage için birim testleri.
"""

import json
from datetime import datetime

from opus_trade.domain.clock import HOME_TZ
from opus_trade.runtime.usage import WINDOW_S, summarize

# 20 Kasım 2026 İstanbul saatiyle 10:00.
NOW = int(datetime(2026, 11, 20, 10, 0, tzinfo=HOME_TZ).timestamp())
MIDNIGHT = int(datetime(2026, 11, 20, 0, 0, tzinfo=HOME_TZ).timestamp())


def call(pid, t, tokens=100, cost=0.5, error=False):
    usage = None if error else json.dumps({"input_tokens": tokens, "output_tokens": tokens // 10,
                                           "cache_read_input_tokens": tokens * 5, "cache_creation_input_tokens": 7})
    return (pid, t, int(error), usage, None if error else cost, None if error else 30.0)


def test_days_follow_istanbul_midnight_and_profiles():
    """Gece yarısından bir dakika önceki çağrı önceki güne yazılır. Profiller ayrı toplanır, toplam ikisinin toplamıdır."""
    rows = [call(1, MIDNIGHT + 60), call(2, MIDNIGHT + 120, tokens=300, cost=1.5), call(1, MIDNIGHT - 60)]
    out = summarize(rows, NOW, days=3)
    today, yesterday, before = out["days"]
    assert today["day"] == "2026-11-20" and today["start"] == MIDNIGHT
    assert today["total"]["calls"] == 2 and today["total"]["input"] == 400 and today["total"]["cost_usd"] == 2.0
    assert today["profiles"]["1"]["cache_read"] == 500 and today["profiles"]["2"]["output"] == 30
    assert yesterday["profiles"]["1"]["calls"] == 1 and before["total"]["calls"] == 0


def test_window_counts_only_last_five_hours_and_errors_are_not_calls():
    """Son beş saatin penceresi daha eski çağrıyı saymaz. Hatalı çağrı tüketimi bilinmediği için çağrı değil hata sayılır."""
    rows = [call(1, NOW - WINDOW_S - 1), call(1, NOW - 60), call(1, NOW - 30, error=True)]
    w = summarize(rows, NOW, days=1)["window"]
    assert w["since"] == NOW - WINDOW_S
    assert (w["total"]["calls"], w["total"]["errors"], w["total"]["input"]) == (1, 1, 100)


def test_rule_strategy_rows_are_not_calls_and_old_rows_are_dropped():
    """Kural stratejisinin satırında usage yoktur, çağrı sayılmaz. İstenen günlerden eski satır hiçbir güne yazılmaz."""
    rows = [(3, NOW - 10, 0, None, None, None), call(1, NOW - 10 * 86400)]
    out = summarize(rows, NOW, days=2)
    assert [d["total"]["calls"] for d in out["days"]] == [0, 0]
    assert out["days"][0]["profiles"] == {"3": out["days"][0]["profiles"]["3"]}
