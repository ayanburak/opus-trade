"""
file: harness.py
importable: T, SOL, RULE, ECHO, FLAKY, Echo, Flaky, NullStream, Api

Entegrasyon testlerinin ortak düzeneği. Api çekirdeği gerçek parçalarıyla
kurar, yalnızca piyasa, akış ve model sahtedir, kontrol API'sine ağa çıkmadan
bağlanır. Echo ve Flaky iki sahte stratejidir.

description: Entegrasyon testleri için ortak çekirdek kurulumu ve sahte stratejiler.
"""

# NOTE: Bu dosya test dosyası değildir, adı test_ ile başlamaz. Test dosyaları
# birbirini içe aktarırsa pytest aynı dosyayı iki modül adıyla yükler, sahte
# strateji sınıfı ikiye bölünür ve kayıt defterindeki sınıf testin
# değiştirdiğiyle aynı olmaz.

import asyncio

import httpx

from opus_trade.app.core import assemble
from opus_trade.api.control import create_app
from opus_trade.config import AppConfig
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



FLAKY = {"strategy": "flaky", "params": {}, "symbols": [SOL], "balance": 100}


# Hata listesi doluysa sıradaki hatayı fırlatır, boşsa verilen kararı döndürür.
class Flaky(Strategy):
    accepts_messages = True
    corrects = True
    errors: list = []
    script: list = []

    async def decide(self, ctx):
        if Flaky.errors:
            raise RuntimeError(Flaky.errors.pop(0))
        return Flaky.script.pop(0) if Flaky.script else Decision()


if "flaky" not in STRATEGIES.entries:
    STRATEGIES.register("flaky", "Test", "", (), lambda deps: Flaky())


