"""
file: test_control_api.py
importable: yok

Çekirdeğin kontrol API'si üzerinden uçtan uca testler. Çekirdek gerçek
parçalarıyla kurulur, yalnızca piyasa, akış ve model sahtedir. Testler admin
panelinin yapacağı işleri yapar: profil kurar, başlatır, kopyalar, sıfırlar,
müdahale eder, kısıt koyar ve model erişimini kapatır.

description: api.control ve runtime.interventions için entegrasyon testleri.
"""

import asyncio

import httpx

from opus_trade.app.core import assemble
from opus_trade.api.control import create_app
from opus_trade.config import AppConfig
from opus_trade.domain.models import Side
from opus_trade.domain.trading import OpenPosition
from opus_trade.market.models import Candle
from opus_trade.market.stream import MarketStream
from opus_trade.strategy.base import STRATEGIES, Decision, Strategy
from tests.fakes import FakeClock, FakeMarket

T = 1_795_000_020
SOL = "SOLUSDT"
RULE = {"strategy": "liquidation_reversal", "params": {}, "symbols": [SOL], "balance": 100,
        "triggers": [{"type": "liquidation_wave", "min_usd": {SOL: 100000}}]}
ECHO = {"strategy": "echo", "params": {}, "symbols": [SOL], "balance": 100}


# Yönetici mesajı alabilen bir strateji. Her bağlamı kaydeder, kendisine
# verilen kararları sırayla döndürür.
class Echo(Strategy):
    accepts_messages = True
    corrects = True
    seen: list = []
    script: list = []

    async def decide(self, ctx):
        Echo.seen.append(ctx)
        return Echo.script.pop(0) if Echo.script else Decision()


if "echo" not in STRATEGIES.entries:
    STRATEGIES.register("echo", "Test", "", (), lambda deps: Echo())


class NullStream(MarketStream):
    async def watch(self, symbols):
        pass

    async def run(self, listener):
        await asyncio.Event().wait()


class Api:
    def __init__(self, path):
        self.clock = FakeClock(T)
        self.core = assemble(AppConfig(path), self.clock, FakeMarket(), NullStream(), None)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(create_app(self.core)), base_url="http://core")
        Echo.seen, Echo.script = [], []

    async def call(self, method, url, ok=True, **kw):
        r = await self.client.request(method, url, **kw)
        assert (r.status_code == 200) == ok, r.text
        return r.json()

    async def running(self, config):
        p = await self.call("POST", "/profiles", json={"name": "Test", "short": "T", "config": config})
        await self.call("POST", f"/profiles/{p['id']}/start")
        r = self.core.supervisor.runners[p["id"]]
        r.on_candle(Candle(SOL, T // 60 * 60, 100, 100, 100, 100, 1, 1, 1, 1), False)
        return p["id"], r

    async def step(self, r, seconds=0):
        self.clock.t += seconds
        r.tick(self.clock.t)
        await r.wait_idle()


def run(test):
    def wrapper(tmp_path):
        asyncio.run(test(Api(tmp_path)))
    wrapper.__name__, wrapper.__doc__ = test.__name__, test.__doc__
    return wrapper


@run
async def test_profile_lifecycle_routes(api):
    """Panelin profil düğmeleri doğru uçlara gider. Kopyalama ve sıfırlama genel işlem yoluna düşmemeli."""
    reg = await api.call("GET", "/registry")
    assert {"llm", "liquidation_reversal"} <= {s["key"] for s in reg["strategies"]}
    pid, _ = await api.running(RULE)
    copy = await api.call("POST", f"/profiles/{pid}/clone", json={"name": "Kopya", "short": "K"})
    assert copy["state"] == "draft"
    await api.call("POST", f"/profiles/{pid}/reset", ok=False)
    await api.call("POST", f"/profiles/{pid}/stop", json={"close_all": False})
    reset = await api.call("POST", f"/profiles/{pid}/reset")
    assert reset["account_id"] != copy["account_id"]
    await api.call("POST", f"/profiles/{pid}/explode", ok=False)


@run
async def test_rule_strategy_takes_only_firm_actions(api):
    """Kural stratejisi yönetici mesajı okuyamaz, ona teklif ya da soru gitmez. Kesin işlem ise uygulanır."""
    pid, r = await api.running(RULE)
    await api.call("POST", f"/profiles/{pid}/interventions", ok=False,
                   json={"kind": "question", "params": {"text": "Neden?"}})
    iv = await api.call("POST", f"/profiles/{pid}/interventions",
                        json={"kind": "command", "weight": "firm",
                              "params": {"type": "open", "symbol": SOL, "side": "long", "leverage": 10, "margin": 10}})
    assert "#1 SOLUSDT long açıldı" in iv["result"]
    acc = await api.call("GET", f"/profiles/{pid}/account")
    assert len(acc["state"]["positions"]) == 1


@run
async def test_firm_restriction_rejects_strategy_and_is_told(api):
    """Kesin kısıt stratejinin aşan komutunu reddeder ve stratejiye yürürlükte olduğu söylenir."""
    pid, r = await api.running(ECHO)
    await api.step(r)
    iv = await api.call("POST", f"/profiles/{pid}/interventions", json={
        "kind": "restrict", "weight": "firm", "params": {"limit": {"type": "max_leverage", "value": 5}},
        "call_now": True})
    Echo.script = [Decision([OpenPosition(SOL, Side.LONG, 10, 10)], ["long_ac SOLUSDT"])]
    await api.step(r, 1)
    ctx = Echo.seen[1]
    assert f"#{iv['id']}, kesin: kaldıraç en fazla 5x." in ctx.admin["restrictions"]
    entry = (await api.call("GET", f"/profiles/{pid}/journal"))[1]
    assert not entry["results"][0]["ok"] and f"#{iv['id']} numaralı kesin kısıtı" in entry["results"][0]["message"]


@run
async def test_withdrawn_message_is_never_shown(api):
    """Gösterilmeden geri çekilen müdahale stratejiye hiç gitmez. Gösterilmiş olan ise artık geri çekilemez."""
    pid, r = await api.running(ECHO)
    await api.step(r)
    first = await api.call("POST", f"/profiles/{pid}/interventions", json={"kind": "info", "params": {"text": "A"}})
    second = await api.call("POST", f"/profiles/{pid}/interventions", json={"kind": "info", "params": {"text": "B"}})
    await api.call("POST", f"/profiles/{pid}/interventions/{first['id']}/withdraw")
    r.wake("admin")
    await api.step(r, 1)
    items = Echo.seen[-1].admin["items"]
    assert len(items) == 1 and "bilgi veriyor: B" in items[0]
    await api.call("POST", f"/profiles/{pid}/interventions/{second['id']}/withdraw", ok=False)


@run
async def test_stop_with_close_all_closes_positions_and_records_it(api):
    """Durdururken kapatma istenirse pozisyonlar kapanır ve bu bir kesin müdahale olarak kayda geçer."""
    pid, r = await api.running(RULE)
    await api.call("POST", f"/profiles/{pid}/interventions", json={
        "kind": "command", "weight": "firm",
        "params": {"type": "open", "symbol": SOL, "side": "long", "leverage": 10, "margin": 10}})
    await api.call("POST", f"/profiles/{pid}/stop", json={"close_all": True})
    acc = await api.call("GET", f"/profiles/{pid}/account")
    assert acc["state"]["positions"] == []
    ivs = await api.call("GET", f"/profiles/{pid}/interventions")
    assert ivs[0]["params"] == {"type": "close_all"} and ivs[0]["note"] == "Durdururken kapatıldı."


@run
async def test_model_access_time_must_be_in_the_future(api):
    """Geçmiş bir saat girilirse erişim hiç kapanmaz ve kullanıcı bunu fark etmez, bu yüzden reddedilir."""
    await api.call("POST", "/system/model", ok=False, json={"until": T - 1})
    out = await api.call("POST", "/system/model", json={"until": T + 3600})
    assert out["model_closed_until"] == T + 3600
    assert (await api.call("GET", "/system"))["model_closed_until"] == T + 3600
