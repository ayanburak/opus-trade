"""
file: test_tools.py
importable: yok

Opus'un piyasa araçları sunucusunun testleri. Bir araç çökerse ya da yanlış
dosyayı okursa Opus o veriyi sessizce kaybeder. Testler ağa çıkmayan
araçları gerçek depolarla çalıştırır.

description: tools.market için birim testleri.
"""

import asyncio

from opus_trade.domain.models import Side, TpLevel
from opus_trade.domain.trading import Trade
from opus_trade.collector.store import MIGRATIONS as MARKET_MIGRATIONS
from opus_trade.collector.store import CollectorStore
from opus_trade.market.models import Liquidation
from opus_trade.storage.database import connect
from opus_trade.storage.ledger import MIGRATIONS, Ledger
from opus_trade.storage.migrate import migrate
from opus_trade.tools import market as tools

NOW = 1_795_000_000


def test_tool_list_is_complete():
    """Opus'un talimatı bu araçları anlatır. Biri listeden düşerse Opus olmayan bir aracı arar."""
    names = {t.name for t in asyncio.run(tools.server.list_tools())}
    assert names == {"mumlar", "gostergeler_hesapla", "emir_defteri", "fonlama_gecmisi", "acik_pozisyon_gecmisi",
                     "long_short_oranlari", "gosterge", "seviyeler", "hacim_profili", "korelasyon", "likidasyonlar",
                     "ny_acilisi", "acilis_gecmisi", "gecmis_kararlarim", "gecmis_islemlerim", "cikis_karnesi"}


def test_liquidations_tool_reads_the_collector_store(tmp_path, monkeypatch):
    """Likidasyon aracı toplayıcının deposunu okur. Yanlış dosyaya bakarsa Opus hiç likidasyon olmadığını sanır."""
    con = connect(tmp_path / "market.db")
    migrate(con, MARKET_MIGRATIONS, tmp_path / "bak")
    CollectorStore(con).add_liquidations([Liquidation(NOW - 60, "SOLUSDT", Side.LONG, 100.0, 1000.0),
                                          Liquidation(NOW - 30, "SOLUSDT", Side.SHORT, 101.0, 10.0)])
    monkeypatch.setenv("OPUS_MARKET_DB", str(tmp_path / "market.db"))
    monkeypatch.setattr(tools.time, "time", lambda: NOW)
    text = tools.likidasyonlar("solusdt", 1)
    assert "long likidasyonları 1 kayıt, 100.0 bin dolar" in text and "long 100.0 bin dolar @ 100" in text


def test_exit_report_reads_the_profile_account(tmp_path, monkeypatch):
    """Çıkış karnesi yalnızca profilin kendi hesabını okur ve değmeyen TP'yi kapanıştaki kademelerden bulur."""
    con = connect(tmp_path / "core.db")
    migrate(con, MIGRATIONS, tmp_path / "bak")
    ledger = Ledger(con)
    mine, other = ledger.create_account("paper", 10, NOW), ledger.create_account("paper", 10, NOW)
    for acc in (mine, other):
        with ledger.atomic():
            pid = ledger.insert_position(acc, number=1, symbol="SOLUSDT", side=Side.LONG, leverage=10, qty=1,
                                         margin=10, entry_price=100, entry_time=NOW - 600, tp_levels=(), sl=None,
                                         trail_pct=None, best_price=100, best_time=NOW, worst_price=100,
                                         worst_time=NOW, liq_price=90, fee=0, funding=0, order_id=None).id
            ledger.insert_trade(Trade(0, acc, pid, 1, "SOLUSDT", Side.LONG, 10, 1, 10, 100, NOW - 600, 99, NOW, "sl",
                                      -1, 0.1, 0, 104, 98.5, (TpLevel(104.5, 100),), 98.9))
    monkeypatch.setenv("OPUS_CORE_DB", str(tmp_path / "core.db"))
    monkeypatch.setenv("OPUS_ACCOUNT", str(mine))
    text = tools.cikis_karnesi(10)
    assert text.count("#1 SOLUSDT long") == 1
    assert "Değmeyen en yakın TP 104.5, fiyat yüzde 0.50 kala döndü" in text
    assert "TP'ye yüzde 0.6'dan az farkla varamayan işlem 1" in text
