"""
file: test_fast.py
importable: yok

Hızlı karar yolunun testleri. Burada bir hata ya sinyalle emir arasına
saniyelik tick'i geri sokar, ya karar sürerken gelen bir dolumu kaybeder, ya
da hatalı bir kararda bildirimi siler.

description: Çalıştırıcının hızlı karar yolu ve uyandırma kodlaması için birim testleri.
"""

import asyncio
from dataclasses import dataclass

from opus_trade.domain.profile import ProfileConfig
from opus_trade.runtime import wakes as codec
from opus_trade.strategy.base import STRATEGIES, Decision, Strategy
from opus_trade.triggers.base import Fire
from tests.unit.runtime.world import SOL, T, World


class Quick(Strategy):
    fast = True
    seen: list = []
    fail: list = []
    hold: asyncio.Event | None = None

    async def decide(self, ctx):
        Quick.seen.append(ctx)
        if Quick.hold:
            await Quick.hold.wait()
        if Quick.fail:
            raise RuntimeError(Quick.fail.pop(0))
        return Decision()


if "quick" not in STRATEGIES.entries:
    STRATEGIES.register("quick", "Test", "", (), lambda deps: Quick(), Quick)


@dataclass
class Burst:
    side: str
    move: float


def fire(ms: int = T * 1000 - 250) -> Fire:
    return Fire("burst", T, SOL, Burst("short", 0.04), ms)


def quick(w: World):
    Quick.seen, Quick.fail, Quick.hold = [], [], None
    p = w.service.create("Hızlı", "H", ProfileConfig("quick", {}, (SOL,), 100.0))
    w.service.move(p.id, "start")
    return w.sup.runners[p.id]


def test_fast_strategy_decides_without_waiting_for_the_tick(tmp_path):
    """Sinyal ile emir arasına saniyelik tick girerse iğnenin ucu kaçar. Uyandırma veritabanına da yazılmamalı."""
    w = World(tmp_path)
    r = quick(w)

    async def main():
        r.wake("trigger", fire())
        assert w.state.wakes(r.pid) == []
        await r.wait_idle()

    asyncio.run(main())
    assert [x.kind for x in Quick.seen[-1].wakes] == ["start", "trigger"]
    entry = w.journal.entries(r.pid, limit=1)[0]
    assert entry["record"]["latency_ms"] == {"decide": 0, "apply": 0, "event": 250}


def test_wakes_during_a_decision_run_right_after_it(tmp_path):
    """Karar sürerken gelen dolum ya da yeni sinyal kaybolmamalı ve bir sonraki tick'i beklememeli."""
    w = World(tmp_path)
    r = quick(w)

    async def main():
        Quick.hold = asyncio.Event()
        r.wake("trigger", fire())
        await asyncio.sleep(0)
        r.wake("account", "dolum")
        Quick.hold.set()
        await r.wait_idle()
        await asyncio.sleep(0)
        await r.wait_idle()

    asyncio.run(main())
    assert [[x.kind for x in c.wakes] for c in Quick.seen] == [["start", "trigger"], ["account"]]


def test_failed_decision_keeps_its_wakes(tmp_path):
    """Hatalı kararın bildirimi silinirse strateji o sinyali hiç görmez. Yeniden deneme bekleme süresinden sonra
    olur, hata her işlemde tekrarlanmamalı."""
    w = World(tmp_path)
    r = quick(w)
    Quick.fail = ["bozuk"]

    async def main():
        r.wake("trigger", fire())
        await r.wait_idle()
        r.wake("account", "dolum")
        await asyncio.sleep(0)

    asyncio.run(main())
    assert len(Quick.seen) == 1 and r.retry_at > T
    assert [x.kind for x in r._queue] == ["start", "trigger", "account"]


def test_fast_context_skips_the_ledger_wide_reads(tmp_path):
    """Hazır gerçekler ve işlem özeti defterin tamamını okur. Hızlı yolda her karar bunu yapsaydı gecikme defter
    büyüdükçe artardı."""
    w = World(tmp_path)
    r = quick(w)
    asyncio.run(w.step(r))
    ctx = Quick.seen[-1]
    assert ctx.stats == {} and ctx.history == [] and ctx.facts.today_count == 0 and ctx.start_balance == 100


def test_any_dataclass_fire_is_stored_with_its_stamp():
    """Yeni tetikleyicilerin bildirimleri günlüğe yazılabilmeli, yoksa her yeni tetikleyici kaydı bozar. Damga
    kaybolursa gecikme sonradan hesaplanamaz."""
    back = codec.decode(codec.encode(fire()))
    assert back == Fire("burst", T, SOL, {"side": "short", "move": 0.04}, T * 1000 - 250)
