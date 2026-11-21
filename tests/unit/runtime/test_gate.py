"""
file: test_gate.py
importable: yok

Kapının, risk korumasının, kısıtların ve olay yolunun testleri. Kapı riski
azaltan bir komutu engellerse kullanıcı zararını kesemez, riski artıran bir
komutu kaçırırsa canlı hesapta sınır yok demektir.

description: runtime.gate, runtime.risk, runtime.limits ve runtime.bus için birim testleri.
"""

import asyncio

import pytest

from opus_trade.broker.paper import PaperBroker
from opus_trade.domain.errors import RuleError
from opus_trade.domain.fees import BINANCE_STANDARD as FEES
from opus_trade.domain.models import Side
from opus_trade.domain.trading import Actor, ClosePosition, OpenPosition, PlaceOrder, Ref
from opus_trade.runtime.bus import EventBus
from opus_trade.runtime.gate import Gate, Restriction, RestrictionSource
from opus_trade.runtime.limits import MaxLeverage, MaxMargin
from opus_trade.runtime.risk import RiskGuard
from opus_trade.storage.database import connect
from opus_trade.storage.ledger import MIGRATIONS, Ledger
from opus_trade.storage.migrate import migrate
from tests.fakes import FakeClock, catalog

T = 1_795_000_000
SOL = "SOLUSDT"
OPEN = OpenPosition(SOL, Side.LONG, leverage=10, margin=10)


class Fixed(RestrictionSource):
    def __init__(self, items):
        self.items = items

    def restrictions(self, now):
        return self.items


@pytest.fixture
def parts(tmp_path):
    con = connect(tmp_path / "core.db")
    migrate(con, MIGRATIONS, tmp_path / "bak")
    ledger, clock = Ledger(con), FakeClock(T)
    broker = PaperBroker(ledger, ledger.create_account("paper", 100.0, T), catalog(), FEES, clock)
    broker.on_price(SOL, 100, 100, 100, T)
    return broker, RiskGuard(ledger, clock), EventBus()


def submit(gate, cmd, actor=Actor.STRATEGY):
    return asyncio.run(gate.submit(cmd, actor, T))


def test_restriction_binds_the_strategy_but_not_the_admin(parts):
    """Kesin kısıt admin'in stratejiye koyduğu sınırdır. Admin kendi elle açtığı işlemde ona takılmaz."""
    broker, _, bus = parts
    gate = Gate(broker, bus, catalog(), restrictions=Fixed([Restriction(4, MaxLeverage(5))]))
    with pytest.raises(RuleError, match="#4 numaralı kesin kısıtı geçerli: kaldıraç en fazla 5x"):
        submit(gate, OPEN)
    assert submit(gate, OPEN, Actor.ADMIN)


def test_total_margin_limit_counts_pending_orders(parts):
    """Bekleyen emirler dolunca marjin kullanır. Sayılmazsa toplam sınır emirlerle aşılabilir."""
    broker, _, bus = parts
    gate = Gate(broker, bus, catalog(), restrictions=Fixed([Restriction(1, MaxMargin(25, total=True))]))
    submit(gate, PlaceOrder(SOL, Side.LONG, trigger=95, leverage=10, margin=10))
    submit(gate, OPEN)
    with pytest.raises(RuleError, match="toplam marjin"):
        submit(gate, OPEN)


def test_daily_loss_blocks_new_risk_but_never_closing(parts):
    """Günlük zarar sınırı yeni risk almayı durdurur. Zararı kesen kapatma ise her zaman serbest kalmalı."""
    broker, guard, bus = parts
    guard.daily_loss = 0.5
    gate = Gate(broker, bus, catalog(), guard=guard)
    submit(gate, OPEN)
    broker.on_price(SOL, 100, 99, 99, T + 60)
    with pytest.raises(RuleError, match="günlük zarar sınırı"):
        submit(gate, OPEN)
    assert submit(gate, ClosePosition(Ref(1, SOL, Side.LONG)))


def test_halt_stops_the_admin_too(parts):
    """Acil durdurma canlı hesabın son güvencesidir, admin'in elle açmak istediği işlemi de durdurur."""
    broker, guard, bus = parts
    guard.halted = True
    with pytest.raises(RuleError, match="acil durdurma"):
        submit(Gate(broker, bus, catalog(), guard=guard), OPEN, Actor.ADMIN)


def test_gate_puts_prices_on_the_tick_before_the_broker(parts):
    """Opus ve panel fiyat adımını bilmek zorunda değil, kapı fiyatı adıma oturtur. Broker ise adım dışı fiyatı
    reddeder, yuvarlama yalnızca kapıda olmalı."""
    broker, _, bus = parts
    cmd = PlaceOrder(SOL, Side.LONG, trigger=95.004, leverage=10, margin=10, sl=90.006)
    with pytest.raises(RuleError, match="katı olmalı"):
        asyncio.run(broker.execute(cmd, Actor.STRATEGY))
    submit(Gate(broker, bus, catalog()), cmd)
    [o] = broker.state().orders
    assert (o.trigger, o.sl) == (95.0, 90.01)


def test_slow_listener_does_not_block_the_others():
    """Uyuyan bir tarayıcı sekmesinin dolu kuyruğu diğer dinleyicilerin olaylarını durdurmamalı."""
    bus = EventBus(maxsize=1)
    slow, fast = bus.subscribe(), bus.subscribe()
    bus.publish("a")
    fast.get_nowait()
    bus.publish("b")
    assert fast.get_nowait() == "b"
    assert bus.dropped == 1 and slow.qsize() == 1
