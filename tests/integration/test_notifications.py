"""
file: test_notifications.py
importable: yok

Bildirimlerin testleri. Çekirdek gerçek parçalarıyla kurulur, olaylar gerçek
kâğıt hesaptan, çalıştırıcıdan ve denetçiden gelir. Burada bir hata ya
likidasyon gibi kritik bir olayı sessiz bırakır, ya aynı sorunu dakikada bir
çaldırıp yöneticiyi bildirimlere sağır eder, ya da okundu bilgisini cihazlar
arasında kaybeder. Telegram kanalı sahte bir sunucuya karşı sınanır.

description: runtime.notifications, storage.notifications ve bildirim uçları için testler.
"""

import asyncio
import json

import httpx
import pytest

from opus_trade.domain.models import Side
from opus_trade.domain.trading import ClosePosition, Ref
from opus_trade.market.models import Candle
from opus_trade.runtime.notifications import ChannelError, TelegramChannel
from opus_trade.strategy.base import Decision
from tests.integration.harness import FLAKY, SOL, T, Api, Flaky

def run(test):
    def wrapper(tmp_path):
        Flaky.errors, Flaky.script = [], []
        asyncio.run(test(Api(tmp_path)))
    wrapper.__name__, wrapper.__doc__ = test.__name__, test.__doc__
    return wrapper


def candle(t, low, high=100, close=None):
    return Candle(SOL, t // 60 * 60, 100, high, low, close if close is not None else low, 1, 1, 1, 1)


async def notes(api):
    await api.core.notifier.drain()
    return list(reversed(await api.call("GET", "/notifications")))


async def open_long(api, pid, sl=None):
    spec = {"type": "open", "symbol": SOL, "side": "long", "leverage": 10, "margin": 10}
    await api.call("POST", f"/profiles/{pid}/interventions", json={"kind": "command", "params": {**spec, "sl": sl}})


@run
async def test_account_events_get_the_right_level(api):
    """SL ile kapanış önemli, likidasyon kritiktir. Likidasyon önemli sayılırsa sessiz saatlerde hiç çalmaz."""
    pid, r = await api.running(FLAKY)
    await open_long(api, pid, sl=95)
    r.on_candle(candle(T + 60, 94), False)
    await open_long(api, pid)
    r.on_candle(candle(T + 120, 80), False)
    got = [(n["level"], n["kind"]) for n in await notes(api)]
    assert got == [("important", "opened"), ("important", "sl"), ("important", "opened"),
                   ("critical", "liquidation")]
    counts = await api.call("GET", "/notifications/counts")
    assert counts["total"] == {"critical": 1, "important": 3}
    assert counts["profiles"][str(pid)] == {"critical": 1, "important": 3}


@run
async def test_error_streak_and_model_limit_are_told_once(api):
    """Üçüncü art arda hata bir kez kritik bildirilir. Limit hatası her denemede değil bir kez söylenir, başarılı karar sayacı sıfırlar."""
    pid, r = await api.running(FLAKY)
    Flaky.errors = ["zaman aşımı"] * 4 + ["You've hit your limit · resets 6:40am"] * 2
    for _ in range(6):
        await api.step(r, 601)
    got = [n["kind"] for n in await notes(api)]
    assert got == ["decide_errors", "model_limit"]
    await api.step(r, 601)
    Flaky.errors = ["zaman aşımı"] * 3
    r.wake("admin")
    for _ in range(3):
        await api.step(r, 601)
    await api.step(r, 601)
    Flaky.errors = ["You've hit your limit · resets 6:40am"]
    r.wake("admin")
    await api.step(r, 601)
    assert [n["kind"] for n in await notes(api)] == ["decide_errors", "model_limit", "decide_errors", "model_limit"]


@run
async def test_reply_is_important_and_read_state_is_shared(api):
    """Opus'un cevabı önemli bildirimdir. Okundu yapılınca sayılar akışa gider, başka cihazdaki baloncuk da söner."""
    pid, r = await api.running(FLAKY)
    Flaky.script = [Decision(record={"reply": "FOMC'ye kadar beklerim."})]
    await api.step(r)
    [n] = await notes(api)
    assert (n["level"], n["kind"], n["body"]) == ("important", "reply", "FOMC'ye kadar beklerim.")
    q = api.core.bus.subscribe()
    out = await api.call("POST", "/notifications/read", json={"profile_id": pid})
    assert out["changed"] == 1 and out["counts"]["total"] == {"critical": 0, "important": 0}
    assert q.get_nowait() == {"notification_counts": out["counts"]}
    assert (await api.call("GET", "/notifications", params={"unread": True})) == []
    await api.call("POST", "/notifications/read", ok=False, json={})


@run
async def test_correction_is_info_only(api):
    """Düzeltme çağrısı bilgi seviyesindedir, baloncuğa sayılmaz. Sayılsa her reddedilen eylemde ses çalardı."""
    pid, r = await api.running(FLAKY)
    bad = ClosePosition(Ref(9, SOL, Side.LONG))
    Flaky.script = [Decision([bad], ["kapat 9"])]
    await api.step(r)
    kinds = [(n["level"], n["kind"]) for n in await notes(api)]
    assert ("info", "correction") in kinds
    assert (await api.call("GET", "/notifications/counts"))["total"] == {"critical": 0, "important": 0}


@run
async def test_daily_loss_is_told_once_a_day_even_after_restart(api):
    """Günlük zarar sınırı her dakika değil günde bir kez bildirilir. Profil yeniden kurulsa da aynı gün tekrar etmez."""
    cfg = {**FLAKY, "risk": {"daily_loss": 1}}
    pid, r = await api.running(cfg)
    await open_long(api, pid)
    r.on_candle(candle(T + 60, 95, close=95), False)
    for s in (60, 60):
        await api.step(r, s)
    api.core.supervisor.refresh(pid)
    r = api.core.supervisor.runners[pid]
    r.on_candle(candle(T + 180, 95, close=95), False)
    await api.step(r, 60)
    got = [n["kind"] for n in await notes(api) if n["level"] == "critical"]
    assert got == ["daily_loss"]


@run
async def test_stream_silence_is_critical_once_and_recovery_is_info(api):
    """İki dakika mum gelmezse bir kez kritik bildirim gider, akış dönünce bilgi düşer. Her saniye tekrar etmemeli."""
    await api.running(FLAKY)
    sup = api.core.supervisor
    sup.last_candle = T
    sup.check_stream(T + 120)
    sup.check_stream(T + 121)
    sup.check_stream(T + 300)
    api.clock.t = T + 400
    await sup.on_candle(candle(T + 400, 100), False)
    got = [(n["level"], n["kind"], n["profile_id"]) for n in await notes(api)]
    assert got == [("critical", "stream_down", None), ("info", "stream_up", None)]
    assert (await api.call("GET", "/notifications/counts"))["profiles"] == {"system": {"critical": 1, "important": 0}}


@run
async def test_new_version_is_info(api):
    """Çalışan profil yeni sürüme geçince bilgi bildirimi düşer, sürüm numarasıyla."""
    pid, _ = await api.running(FLAKY)
    await api.call("POST", f"/profiles/{pid}/config", json={"config": {**FLAKY, "balance": 100}, "note": "x"})
    assert [(n["level"], n["title"]) for n in await notes(api)] == [("info", "Sürüm 2 yürürlükte")]


@run
async def test_sound_settings_are_validated_and_kept(api):
    """Ses ayarları sunucuda tutulur, böylece her cihazda aynıdır. Bozuk saat kaydedilmez."""
    assert (await api.call("GET", "/notifications/settings"))["quiet"]["enabled"] is False
    await api.call("POST", "/notifications/settings", ok=False, json={"settings": {"quiet": {"start": "25:00"}}})
    await api.call("POST", "/notifications/settings", ok=False, json={"settings": {"volume": 2}})
    saved = await api.call("POST", "/notifications/settings",
                           json={"settings": {"volume": 0.3, "quiet": {"enabled": True, "start": "00:30"}}})
    assert saved["quiet"] == {"enabled": True, "start": "00:30", "end": "08:00"}
    assert await api.call("GET", "/notifications/settings") == saved


# --- Telegram ---

def telegram(handler, **kw):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TelegramChannel("123:GIZLI", 42, client, base_url="https://tg", **kw)


def note(level, name="Trend"):
    return {"level": level, "title": "#1 SOLUSDT long likide oldu", "body": "net -10.00 dolar.", "profile_name": name}


def test_telegram_sends_only_chosen_levels_and_rings_only_for_critical():
    """Bilgi seviyesi Telegram'a gitmez, yalnızca kritik telefonda sesli gelir. Yoksa telefon her emirde öter."""
    sent = []

    def handler(req):
        assert req.url.path == "/bot123:GIZLI/sendMessage"
        sent.append(json.loads(req.content))
        return httpx.Response(200, json={"ok": True, "result": {}})

    async def go():
        ch = telegram(handler)
        for level in ("critical", "important", "info"):
            await ch.send(note(level))

    asyncio.run(go())
    assert [m["disable_notification"] for m in sent] == [False, True]
    assert sent[0]["chat_id"] == 42 and sent[0]["text"].startswith("🔴 Trend: #1 SOLUSDT long likide oldu\nnet")


def test_telegram_errors_never_carry_the_token():
    """Hata metni günlüğe yazılır. Anahtar adresin içinde olduğu için hata metnine asla girmemeli."""
    def refuse(req):
        return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})

    def broken(req):
        raise httpx.ConnectError(f"bağlanamadı: {req.url}")

    async def go():
        for handler in (refuse, broken):
            with pytest.raises(ChannelError) as e:
                await telegram(handler).send(note("critical"))
            assert "GIZLI" not in str(e.value)

    asyncio.run(go())


def test_telegram_check_reads_bot_name():
    """Anahtar kurulurken getMe ile doğrulanır, yanlış anahtar sessizce kabul edilmez."""
    def handler(req):
        assert req.url.path.endswith("/getMe")
        return httpx.Response(200, json={"ok": True, "result": {"username": "opus_bot"}})

    assert asyncio.run(telegram(handler).check()) == "opus_bot"


@run
async def test_failing_channel_does_not_stop_the_others(api):
    """Telegram çökse bile bildirim kaydedilir ve panele gider. Bir kanal ötekini susturmamalı."""
    class Broken:
        async def send(self, n):
            raise ChannelError("kapalı")

    api.core.notifier.channels.insert(0, Broken())
    q = api.core.bus.subscribe()
    pid, _ = await api.running(FLAKY)
    await open_long(api, pid)
    await api.core.notifier.drain()
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    panel = [i["notification"] for i in items if isinstance(i, dict) and "notification" in i]
    assert [n["kind"] for n in panel] == ["opened"] and panel[0]["profile_name"] == "Test"
