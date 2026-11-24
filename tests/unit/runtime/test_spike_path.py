"""
file: test_spike_path.py
importable: yok

İğne dönüşünün uçtan uca yolu: Supervisor'a gelen tekil işlemler ortak
özete girer, tetikleyici patlamayı bildirir, hızlı strateji tick beklemeden
limit emri verir ve emir sonraki işlemle dolar. Parçalar tek tek doğruyken
aralarındaki bağlantı kopuksa strateji hiç işlem açmaz, bu test onu korur.

description: Tekil işlemden dolan emre kadar hızlı yolun birlikte sınanması.
"""

import asyncio

from opus_trade.domain.profile import ProfileConfig, Universe
from opus_trade.domain.trading import OrderType
from opus_trade.market.models import DailyStats, Trade
from opus_trade.market.screen import MarketScreen
from tests.fakes import FakeMarket
from tests.unit.runtime.world import SOL, T, World


class Market(FakeMarket):
    async def all_daily_stats(self):
        return [DailyStats(SOL, 30, 130, 100, 1e9)]


def test_trades_become_a_signal_an_order_and_a_fill(tmp_path):
    """Sakin geçen 400 saniyeden sonra 5 saniyede 1,04'ten 1,08'e alış patlaması. Strateji 10 dolarlık hesapta
    eşikler geçildiği an, 1,0646'da, patlamanın tersine 1,07'den short limit verir. Hareket uzayıp fiyat 1,08'e
    varınca emir dolar, TP'si ve son fiyatla tetiklenen SL'si kurulur. Miktar 4,90'dır: TP 1,03'te de 5 dolarlık
    en küçük büyüklüğü geçmeli, yoksa TP kurulamazdı."""
    w = World(tmp_path)
    w.sup.screen = MarketScreen(Market(), w.clock)
    asyncio.run(w.sup.screen.refresh())
    cfg = ProfileConfig("spike_fade", {}, (), 10.0, ({"type": "trade_burst", "list_size": 1, "ranking": "range"},),
                        universe=Universe.MARKET)
    p = w.service.create("İğne", "İ", cfg)
    w.service.move(p.id, "start")
    r = w.sup.runners[p.id]
    t0 = T - 460
    quiet = [Trade(SOL, (t0 + s) * 1000, 1.00, 10, s % 10 == 0) for s in range(0, 400, 5)]
    quiet += [Trade(SOL, (t0 + s) * 1000, 1.03, 10, True) for s in range(400, 455, 5)]
    burst = [Trade(SOL, (t0 + 455) * 1000 + i * 50, round(1.04 + 0.04 * i / 99, 6), 10, True) for i in range(100)]

    async def main():
        for x in quiet + burst:
            await w.sup.on_trade(x)
            await asyncio.sleep(0)
        await r.wait_idle()

    asyncio.run(main())
    st = r.broker.state()
    assert [(x.symbol, x.side.value, x.entry_price, x.qty) for x in st.positions] == [(SOL, "short", 1.07, 4.9)]
    assert sorted((o.type, o.by_last) for o in st.orders) == [(OrderType.STOP, True), (OrderType.TAKE_PROFIT, False)]
    entry = next(e for e in w.journal.entries(p.id, limit=10) if e["record"].get("signals"))
    assert entry["record"]["signals"][0]["result"] == "açıldı" and "event" in entry["record"]["latency_ms"]
    assert [x["label"] for x in entry["results"]] == ["SOLUSDT kaldıraç 14x", "SOLUSDT short limit 1.06997"]
