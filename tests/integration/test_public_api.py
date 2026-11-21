"""
file: test_public_api.py
importable: yok

Herkese açık API'nin testleri. Çekirdek gerçek parçalarıyla kurulur, herkese
açık uygulama ona kontrol API'si üzerinden bağlanır, tıpkı opus-web'de olacağı
gibi. Burada bir hata yöneticinin Opus'a yazdıklarını, Opus'un yöneticiye
cevabını ya da yayında olmayan bir profilin verisini internete açar, ya da
sınırsız bağlantıyla sunucuyu yorar.

description: api.public için entegrasyon testleri.
"""

import asyncio
import json

import httpx

from opus_trade.api import public
from opus_trade.api.public import Hub, create_public_app
from opus_trade.domain.models import Side
from opus_trade.domain.trading import OrderType, PlaceOrder
from opus_trade.market.models import Candle
from opus_trade.strategy.base import Decision
from tests.integration.harness import ECHO, FLAKY, SOL, T, Api, Echo, Flaky

SECRETS = ("GIZLI-TALIMAT", "GIZLI-HAM", "GIZLI-CEVAP", "ornek@posta.com", "yöneticinin #", "FOMC notu")


class Public:
    def __init__(self, api: Api, max_clients: int = 50, source=None):
        self.api = api
        self.app = create_public_app(api.client, max_clients, source)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(self.app), base_url="http://web")

    async def get(self, path, status=200, **params):
        r = await self.client.get(path, params=params)
        assert r.status_code == status, r.text
        return r.json()


def run(test):
    def wrapper(tmp_path):
        Flaky.errors, Flaky.script = [], []
        asyncio.run(test(Api(tmp_path)))
    wrapper.__name__, wrapper.__doc__ = test.__name__, test.__doc__
    return wrapper


async def publish(api, pid):
    await api.call("POST", "/publish", json={"profile_id": pid})


@run
async def test_only_the_published_profile_is_visible(api):
    """Yayında profil yokken hiçbir şey dönmez. Yayındaki profilin özeti yalnızca izin verilen alanları taşır."""
    pub = Public(api)
    await pub.get("/profile", 404)
    pid, _ = await api.running(ECHO)
    await publish(api, pid)
    p = await pub.get("/profile")
    assert p["id"] == pid and p["symbols"] == [SOL]
    assert not {"account_id", "published", "area", "memory", "versions"} & set(p)


@run
async def test_publishing_one_profile_hides_the_previous(api):
    """Yayında aynı anda tek profil olur. Yeni profil seçilince eskisi dışarıdan kaybolur, yayın kaldırılınca hiçbiri görünmez."""
    pub = Public(api)
    first, _ = await api.running(ECHO)
    second, _ = await api.running(ECHO)
    await publish(api, first)
    await publish(api, second)
    assert (await pub.get("/profile"))["id"] == second
    assert [p["id"] for p in await api.call("GET", "/profiles") if p["published"]] == [second]
    await publish(api, None)
    await pub.get("/profile", 404)
    await pub.get("/journal", 404)


@run
async def test_other_profiles_tool_calls_stay_closed(api):
    """Araç çağrıları kayıt kimliğiyle istenir. Başka bir profilin kaydının kimliği verilse de açılmamalı."""
    pub = Public(api)
    shown, rs = await api.running(ECHO)
    hidden, rh = await api.running(ECHO)
    await publish(api, shown)
    await api.step(rh)
    jid = (await api.call("GET", f"/profiles/{hidden}/journal"))[0]["id"]
    await pub.get(f"/journal/{jid}/tools", 404)
    await api.call("GET", f"/profiles/{shown}/journal/{jid}/tools", ok=False)


@run
async def test_journal_carries_no_admin_traces(api):
    """Yöneticinin mesajı, Opus'un cevabı, talimat, ham çıktı, kısıt reddi ve hata metni dışarı çıkmaz. Özet ve araçlar kalır."""
    pub = Public(api)
    pid, r = await api.running(ECHO)
    await publish(api, pid)
    await api.step(r)
    await api.call("POST", f"/profiles/{pid}/interventions", json={
        "kind": "restrict", "weight": "firm", "params": {"limit": {"type": "max_leverage", "value": 5}}})
    await api.call("POST", f"/profiles/{pid}/interventions", json={
        "kind": "message", "weight": "firm", "params": {"text": "FOMC notu"}, "call_now": True})
    Echo.script = [Decision([PlaceOrder(SOL, Side.LONG, OrderType.MARKET, qty=1)], ["long_ac SOLUSDT"],
                            record={"prompt": "GIZLI-TALIMAT", "raw": "GIZLI-HAM", "reply": "GIZLI-CEVAP",
                                    "summary": "Bekliyorum.",
                                    "decision": {"ozet": "Bekliyorum.", "yoneticiye_cevap": "GIZLI-CEVAP"}})]
    await api.step(r, 1)
    entries = await pub.get("/journal")
    text = json.dumps(entries, ensure_ascii=False)
    assert not [s for s in SECRETS if s in text], text
    last = next(e for e in entries if e["record"].get("summary") == "Bekliyorum.")
    assert [w["kind"] for w in last["wakes"]] == []
    assert last["record"] == {"summary": "Bekliyorum.", "decision": {"ozet": "Bekliyorum."}, "tools": []}
    assert last["results"][0]["message"] == "long_ac SOLUSDT reddedildi."


@run
async def test_admin_alert_wake_is_hidden(api):
    """Yöneticinin alarmı tetiklenip Opus'u uyandırınca o uyandırma dışarıda görünmez. Stratejinin kendi alarmınınki görünür."""
    pub = Public(api)
    pid, r = await api.running(ECHO)
    await publish(api, pid)
    await api.step(r)
    await api.call("POST", f"/profiles/{pid}/interventions", json={"kind": "alert", "params": {"symbol": SOL, "price": 99}})
    Echo.script = [Decision(alarms=[{"op": "add", "symbol": SOL, "price": 98, "note": "", "label": "alarm"}])]
    r.wake("timer")
    await api.step(r, 1)
    r.on_candle(Candle(SOL, (T + 60) // 60 * 60, 100, 100, 97, 97, 1, 1, 1, 1), False)
    await api.step(r, 1)
    wakes = (await pub.get("/journal"))[0]["wakes"]
    alerts = [json.loads(w["data"])["data"]["actor"] for w in wakes if w["kind"] == "trigger"]
    assert alerts == ["strategy"]


@run
async def test_decide_error_text_is_hidden(api):
    """claude komutunun hata metni hesap bilgisi taşıyabilir. Dışarıda yalnızca kararın alınamadığı görünür."""
    pub = Public(api)
    pid, r = await api.running(FLAKY)
    await publish(api, pid)
    Flaky.errors = ["oturum ornek@posta.com için kapalı"]
    await api.step(r)
    [entry] = await pub.get("/journal")
    assert entry["error"] == "Karar alınamadı."


@run
async def test_account_hides_admin_alerts_and_restrictions(api):
    """Yöneticinin kurduğu alarm ve kısıtlar dışarıda görünmez. Stratejinin kendi alarmı görünür."""
    pub = Public(api)
    pid, r = await api.running(ECHO)
    await publish(api, pid)
    await api.call("POST", f"/profiles/{pid}/interventions", json={"kind": "alert", "params": {"symbol": SOL, "price": 120}})
    await api.call("POST", f"/profiles/{pid}/interventions", json={
        "kind": "restrict", "weight": "firm", "params": {"limit": {"type": "max_leverage", "value": 5}}})
    Echo.script = [Decision(alarms=[{"op": "add", "symbol": SOL, "price": 90, "note": "", "label": "alarm"}])]
    await api.step(r)
    acc = await pub.get("/account")
    assert [a["price"] for a in acc["alerts"]] == [90] and "restrictions" not in acc


@run
async def test_candles_only_for_profile_symbols(api):
    """Mum ucu yalnızca yayındaki profilin paritelerini verir, çekirdeği her pariteyi çekmeye açmaz."""
    pub = Public(api)
    pid, _ = await api.running(ECHO)
    await publish(api, pid)
    await pub.get("/candles", 404, symbol="BTCUSDT")


@run
async def test_hub_filters_events_and_limits_clients(api):
    """Akışta yalnızca yayındaki profilin olayları ve paritelerinin mumları gider. Bildirim, başka profil ve yöneticinin
    alarmı gitmez. Sınır doluysa 503 döner."""
    pid, _ = await api.running(ECHO)
    other, _ = await api.running(ECHO)
    await publish(api, pid)
    acc = (await api.call("GET", "/profiles"))
    accounts = {p["id"]: p["account_id"] for p in acc}
    upstream: asyncio.Queue = asyncio.Queue()

    async def source():
        while True:
            yield await upstream.get()

    pub = Public(api, max_clients=1, source=source)
    hub: Hub = pub.app.state.hub
    q = hub.join()
    await asyncio.wait_for(pub.get("/stream", 503), 2)
    for item in ({"account_event": {"account_id": accounts[other], "kind": "opened"}},
                 {"notification": {"title": "x"}},
                 {"profile": pid, "fire": {"kind": "alert", "data": {"actor": "admin"}}},
                 {"profile": pid, "decide_error": "ornek@posta.com", "limit": False},
                 {"profile": other, "journal": 1},
                 {"candle": {"symbol": "BTCUSDT", "close": 1}, "closed": False},
                 {"profile": pid, "journal": 7},
                 {"candle": {"symbol": SOL, "close": 2}, "closed": True},
                 {"account_event": {"account_id": accounts[pid], "kind": "opened"}}):
        upstream.put_nowait(item)
    got = [await asyncio.wait_for(q.get(), 1) for _ in range(3)]
    assert got == [{"profile": pid, "journal": 7}, {"candle": {"symbol": SOL, "close": 2}, "closed": True},
                   {"account_event": {"account_id": accounts[pid], "kind": "opened"}}]
    assert q.empty()
    hub.leave(q)
    assert hub._task is None


def test_restriction_rejection_is_hidden_in_old_and_new_wording():
    """Yayından önceki kayıtlar kısıt reddini cümle ortasında küçük harfle, yenileri başta büyük harfle yazar.
    Arama birini kaçırırsa yöneticinin kısıtı herkese açık sayfada görünür."""
    old = {"label": "long_ac SOLUSDT", "ok": False,
           "message": "long_ac SOLUSDT reddedildi: Reddedildi, yöneticinin #1 numaralı kesin kısıtı geçerli: x."}
    new = {"label": "piyasa_giris SOLUSDT", "ok": False,
           "message": "piyasa_giris SOLUSDT reddedildi: Yöneticinin #1 numaralı kesin kısıtı geçerli: x."}
    assert public._public_result(old)["message"] == "long_ac SOLUSDT reddedildi."
    assert public._public_result(new)["message"] == "piyasa_giris SOLUSDT reddedildi."
