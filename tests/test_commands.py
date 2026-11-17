"""
file: test_commands.py
importable: yok

commands kapısının, Opus eylemlerinin komuta çevrilmesinin ve yönetici
müdahalelerinin testleri. Proje klasöründe uv run pytest ile çalışır.

description: Kapının kaynağı taşıdığını, kesin kısıtları uyguladığını ve müdahalelerin doğru saklanıp anlatıldığını sınayan testler.
"""

# NOTE: Hesap rakamları test_account.py'de ayrıca sınanır. Burada yalnızca
# kapının ve müdahalelerin davranışı kontrol edilir, bu yüzden rakamlar
# yaklaşık değil yapısal olarak bakılır.

import pytest

from opus_trade.engine import interventions as iv
from opus_trade.engine.account import AccountError
from opus_trade.engine.commands import Command, describe, execute
from opus_trade.opus import actions

from conftest import NOW

PRICES = {"BTCUSDT": 50000.0, "SOLUSDT": 100.0}


def _open(account, source="opus", leverage=10, margin=20, side="long", restrictions=None) -> str:
    cmd = Command("open", {"symbol": "BTCUSDT", "side": side, "leverage": leverage, "margin": margin}, source)
    return execute(account, PRICES, cmd, NOW, restrictions)


def _firm(kind: str, value=None, **params) -> dict:
    if value is not None:
        params["value"] = value
    return {"id": 1, "kind": kind, "params": params}


# --- Eylemden komuta ---

def test_action_becomes_opus_command_with_english_params():
    cmd = actions.to_command({"tip": "long_ac", "sembol": "BTCUSDT", "kaldirac": 5, "marjin_usd": 30,
                              "tp_kademeleri": [{"fiyat": 51000, "yuzde": 50}], "sl": 49000})
    assert cmd.kind == "open" and cmd.source == "opus"
    assert cmd.params == {"side": "long", "symbol": "BTCUSDT", "leverage": 5, "margin": 30,
                          "tp_levels": [{"price": 51000, "pct": 50}], "sl": 49000}


def test_missing_field_keeps_turkish_name():
    with pytest.raises(KeyError, match="marjin_usd"):
        actions.to_command({"tip": "long_ac", "sembol": "BTCUSDT", "kaldirac": 5})


def test_unknown_action_is_rejected():
    with pytest.raises(AccountError, match="Bilinmeyen eylem tipi"):
        actions.to_command({"tip": "uc"})


# Şemadaki her eylem, zorunlu alanları doldurulunca kapının bildiği bir komuta çevrilebilmeli.
def test_every_schema_option_translates():
    sample = {"sembol": "BTCUSDT", "yon": "long", "pozisyon_id": 1, "emir_id": 1, "alarm_id": 1, "tetik_fiyat": 1.0,
              "kaldirac": 1, "marjin_usd": 1.0, "fiyat": 1.0, "not": "x"}
    for option in actions.schema(["BTCUSDT"])["anyOf"]:
        assert option["description"]
        a = {f: sample[f] for f in option["required"] if f != "tip"}
        a["tip"] = option["properties"]["tip"]["const"]
        describe(actions.to_command(a))


# --- Kaynak ---

def test_message_and_close_reason_follow_source(account):
    assert _open(account).startswith("#1 BTCUSDT long 10x açıldı")
    _open(account, source="admin")
    execute(account, PRICES, Command("close", {"position_id": 1}, "opus"), NOW)
    execute(account, PRICES, Command("close", {"position_id": 2, "pct": 100}, "admin"), NOW)
    assert account._trade(1)["exit_reason"] == "agent"
    assert account._trade(2)["exit_reason"] == "admin"


# BTC'de likidasyon 10.000'in üstündedir. Mesaj onu bilimsel gösterimle değil,
# istemin hesap bölümündeki gibi bir basamakla yazmalı.
def test_liquidation_price_is_written_plainly(account):
    opened = _open(account)
    liq = account.positions()[0]["liq_price"]
    assert f"likidasyon {liq:.1f}." in opened and "e+" not in opened
    added = execute(account, PRICES, Command("add", {"position_id": 1, "leverage": 10, "margin": 10}, "opus"), NOW)
    liq = account.positions()[0]["liq_price"]
    assert f"likidasyon {liq:.1f}." in added and "e+" not in added


def test_close_all_closes_positions_and_cancels_orders(account):
    _open(account)
    execute(account, PRICES, Command("place_order", {"symbol": "BTCUSDT", "side": "long", "trigger_price": 48000,
                                                     "leverage": 10, "margin": 10}, "opus"), NOW)
    execute(account, PRICES, Command("set_alert", {"symbol": "BTCUSDT", "price": 52000}, "opus"), NOW)
    msg = execute(account, PRICES, Command("close_all", {}, "admin"), NOW)
    assert "1 bekleyen emir iptal edildi" in msg
    assert not account.positions() and not account.pending_orders()
    assert len(account.alerts()) == 1
    note = account.con.execute("SELECT result_note FROM orders").fetchone()[0]
    assert note == "Yönetici iptal etti"


def test_unknown_command_or_source_is_rejected():
    with pytest.raises(AccountError):
        Command("fly")
    with pytest.raises(AccountError):
        Command("open", {}, "someone")


# --- Kesin kısıtlar ---

def test_leverage_cap_blocks_only_opus(account):
    cap = [_firm("max_leverage", 5)]
    with pytest.raises(AccountError, match="#1 numaralı kesin kısıtı"):
        _open(account, leverage=10, restrictions=cap)
    _open(account, leverage=5, restrictions=cap)
    _open(account, source="admin", leverage=10, restrictions=cap)
    assert len(account.positions()) == 2


def test_symbol_scoped_restriction_ignores_other_symbols(account):
    ban = [_firm("symbol_ban", symbol="SOLUSDT")]
    _open(account, restrictions=ban)
    with pytest.raises(AccountError):
        execute(account, PRICES, Command("open", {"symbol": "SOLUSDT", "side": "long", "leverage": 2, "margin": 10}),
                NOW, ban)


def test_side_only_also_checks_additions(account):
    _open(account, side="short")
    only_long = [_firm("side_only", symbol="BTCUSDT", side="long")]
    with pytest.raises(AccountError):
        execute(account, PRICES, Command("add", {"position_id": 1, "leverage": 10, "margin": 5}), NOW, only_long)
    _open(account, side="long", restrictions=only_long)


def test_total_margin_counts_positions_and_orders(account):
    _open(account, margin=20)
    execute(account, PRICES, Command("place_order", {"symbol": "BTCUSDT", "side": "long", "trigger_price": 48000,
                                                     "leverage": 10, "margin": 10}), NOW)
    cap = [_firm("max_margin", 45, scope="total")]
    with pytest.raises(AccountError):
        _open(account, margin=20, restrictions=cap)
    _open(account, margin=10, restrictions=cap)


def test_closing_is_never_blocked(account):
    _open(account)
    execute(account, PRICES, Command("close", {"position_id": 1}), NOW, [_firm("no_open")])
    assert not account.positions()


# --- Müdahaleler ---

def _add(account, kind, params, weight=None, note="", now=NOW):
    return iv.add(account.con, account, PRICES, kind, params, weight, note, False, now)


def test_firm_command_runs_now_and_is_reported_as_done(account):
    r = _add(account, "command", {"kind": "open", "params": {"symbol": "BTCUSDT", "side": "long", "leverage": 3,
                                                             "margin": 20}}, "firm")
    assert len(account.positions()) == 1
    assert account.positions()[0]["leverage"] == 3
    assert "kesin kararı uygulandı" in iv.lines([r])[0]


def test_offer_command_changes_nothing(account):
    r = _add(account, "command", {"kind": "close", "params": {"position_id": 7, "pct": 50}}, "offer",
             note="Riski azaltalım.")
    line = iv.lines([r])[0]
    assert "teklifi: #7 pozisyonunun yüzde 50 kadarını kapat." in line
    assert line.endswith("Yöneticinin açıklaması: Riski azaltalım.")


def test_rejected_firm_command_is_not_saved(account):
    with pytest.raises(AccountError):
        _add(account, "command", {"kind": "close", "params": {"position_id": 9}}, "firm")
    assert iv.pending(account.con, NOW) == []


@pytest.mark.parametrize("kind, params, weight", [
    ("teleport", {}, None),
    ("command", {"kind": "open", "params": {}}, None),
    ("question", {"text": "Neden?"}, "firm"),
    ("question", {"text": " "}, None),
    ("command", {"kind": "open", "params": {"symbol": "BTCUSDT"}}, "offer"),
    ("restrict", {"kind": "max_leverage", "params": {"value": 0}}, "firm"),
    ("call_at", {"at": NOW - 60}, None),
])
def test_invalid_intervention_is_rejected(account, kind, params, weight):
    with pytest.raises(ValueError):
        _add(account, kind, params, weight)


def test_restriction_lifecycle(account):
    r = _add(account, "restrict", {"kind": "max_leverage", "params": {"value": 5}, "until": NOW + 3600}, "firm")
    _add(account, "restrict", {"kind": "symbol_ban", "params": {"symbol": "SOLUSDT"}}, "offer")
    assert [x["id"] for x in iv.restrictions(account.con, NOW, firm_only=True)] == [r["id"]]
    assert iv.restriction_lines(iv.restrictions(account.con, NOW))[1].startswith("#2, teklif: SOLUSDT")
    assert iv.restrictions(account.con, NOW + 3600, firm_only=True) == []
    _add(account, "unrestrict", {"id": 2})
    assert [x["id"] for x in iv.restrictions(account.con, NOW)] == [1]
    with pytest.raises(ValueError):
        _add(account, "unrestrict", {"id": 2})


def test_pending_until_delivered_and_planned_call_fires_once(account):
    _add(account, "question", {"text": "Neden long?"})
    _add(account, "call_at", {"at": NOW + 600})
    assert len(iv.pending(account.con, NOW)) == 1
    assert iv.due_calls(account.con, NOW) == []
    assert len(iv.due_calls(account.con, NOW + 600)) == 1
    assert iv.due_calls(account.con, NOW + 605) == []
    shown = iv.pending(account.con, NOW + 600)
    assert len(shown) == 2
    iv.mark_delivered(account.con, [r["id"] for r in shown], NOW + 610)
    assert iv.pending(account.con, NOW + 620) == []


def test_firm_notebook_note_is_appended(account):
    from opus_trade.db import get_state, set_state
    set_state(account.con, "notebook", "Eski not.")
    _add(account, "notebook", {"text": "ETH'yi izle."}, "firm")
    assert get_state(account.con, "notebook") == "Eski not.\n\nYöneticinin notu: ETH'yi izle."
