"""
file: world.py
importable: T, SOL, Scripted, NullStream, World, opening, go

Çalıştırıcı testlerinin ortak düzeneği. World profil servisini, Supervisor'ı
ve defteri sahte saat ve sahte piyasayla kurar. Scripted kararlarını
sırayla bir listeden veren sahte stratejidir.

description: Çalıştırıcı testleri için ortak kurulum ve sahte strateji.
"""

# NOTE: Bu dosya test dosyası değildir, adı test_ ile başlamaz. Test dosyaları
# birbirini içe aktarırsa pytest aynı dosyayı iki modül adıyla yükler, sahte
# strateji sınıfı ikiye bölünür ve kayıt defterindeki sınıf testin
# değiştirdiğiyle aynı olmaz.

import asyncio

from opus_trade import plugins
from opus_trade.domain.fees import BINANCE_STANDARD
from opus_trade.domain.models import Side
from opus_trade.domain.profile import ProfileConfig
from opus_trade.domain.trading import OrderType, PlaceOrder, SetLeverage
from opus_trade.market.models import Candle
from opus_trade.market.stream import MarketStream
from opus_trade.registry import Param
from opus_trade.runtime.bus import EventBus
from opus_trade.runtime.model_access import ModelAccess
from opus_trade.runtime.profiles import ProfileService
from opus_trade.runtime.supervisor import Supervisor
from opus_trade.storage.database import connect
from opus_trade.storage.journal import JournalRepo
from opus_trade.storage.ledger import MIGRATIONS, Ledger
from opus_trade.storage.migrate import migrate
from opus_trade.storage.profiles import ProfileRepo
from opus_trade.storage.runtime_state import StateRepo
from opus_trade.strategy.base import STRATEGIES, Decision, Deps, Strategy
from tests.fakes import FakeClock, FakeMarket, catalog

T = 1_795_000_020
SOL = "SOLUSDT"


# Kararları sırayla verilen listeden döndürür, her bağlamı kaydeder. fail
# doluysa decide hata fırlatır. hold verilmişse karar o olay kurulana kadar
# bekler, düşünme süren bir çağrıyı taklit eder. Düzeltme ve model
# yetenekleri ayarlanabilir.
class Scripted(Strategy):
    script: list = []
    seen: list = []
    fail: list = []
    hold: asyncio.Event | None = None

    def __init__(self, corrects: bool = True, uses_model: bool = False):
        self.corrects = corrects
        self.uses_model = uses_model

    async def decide(self, ctx):
        Scripted.seen.append(ctx)
        if Scripted.hold:
            await Scripted.hold.wait()
        if Scripted.fail:
            raise RuntimeError(Scripted.fail.pop(0))
        return Scripted.script.pop(0) if Scripted.script else Decision()


if "scripted" not in STRATEGIES.entries:
    STRATEGIES.register("scripted", "Test", "", (Param("corrects", "int", "", default=1),
                                                 Param("uses_model", "int", "", default=0)),
                        lambda deps, corrects, uses_model: Scripted(bool(corrects), bool(uses_model)))


class NullStream(MarketStream):
    async def watch(self, symbols):
        self.symbols = symbols

    async def run(self, listener):
        await asyncio.Event().wait()


class World:
    def __init__(self, path):
        plugins.load()
        con = connect(path / "core.db")
        migrate(con, MIGRATIONS, path / "bak", catalog=catalog())
        self.clock = FakeClock(T)
        self.ledger, self.journal, self.state = Ledger(con), JournalRepo(con), StateRepo(con)
        self.repo = ProfileRepo(con)
        self.market = FakeMarket()
        self.access = ModelAccess(self.state, self.clock)
        self.sup = Supervisor(self.repo, self.ledger, self.journal, self.state, self.market, NullStream(), self.clock,
                              EventBus(), self.access, catalog(), BINANCE_STANDARD,
                              lambda p: Deps(self.clock, catalog()))
        self.service = ProfileService(self.repo, self.ledger, self.journal, self.state, catalog(), self.clock,
                                      self.sup.refresh)
        Scripted.script, Scripted.seen, Scripted.fail, Scripted.hold = [], [], [], None

    def config(self, **params) -> ProfileConfig:
        return ProfileConfig("scripted", params, (SOL,), 100.0)

    def running(self, **params):
        p = self.service.create("Test", "T", self.config(**params))
        self.service.move(p.id, "start")
        r = self.sup.runners[p.id]
        r.on_candle(Candle(SOL, T // 60 * 60, 100, 100, 100, 100, 1, 1, 1, 1), False)
        return p, r

    async def step(self, r, seconds: int = 0):
        self.clock.t += seconds
        r.tick(self.clock.t)
        await r.wait_idle()


# 10x kaldıraçla 1 SOL, 100 dolardan 10 dolar marjin. Likidasyon 95'in
# altında kalır, SL'li testler likidasyona takılmaz.
def opening(*protection, wait_s=None) -> Decision:
    return Decision([SetLeverage(SOL, 10), PlaceOrder(SOL, Side.LONG, OrderType.MARKET, qty=1, protection=protection)],
                    ["kaldıraç", "aç"], wait_s=wait_s)


def go(coro):
    return asyncio.run(coro)
