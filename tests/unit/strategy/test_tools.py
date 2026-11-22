"""
file: test_tools.py
importable: yok

Opus'un piyasa araçları sunucusunun testleri. Bir araç çökerse ya da yanlış
dosyayı okursa Opus o veriyi sessizce kaybeder. Testler ağa çıkmayan
araçları gerçek depolarla çalıştırır.

description: tools.market için birim testleri.
"""

import asyncio

from opus_trade.domain.models import Side
from opus_trade.domain.trading import Trade
from opus_trade.collector.store import MIGRATIONS as MARKET_MIGRATIONS
from opus_trade.collector.store import CollectorStore
from opus_trade.market.models import Liquidation
from opus_trade.storage.database import connect
from opus_trade.storage.ledger import MIGRATIONS, Ledger
from opus_trade.storage.migrate import migrate
from opus_trade.tools import market as tools

from tests.fakes import catalog

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
    migrate(con, MIGRATIONS, tmp_path / "bak", catalog=catalog())
    ledger = Ledger(con)
    mine, other = ledger.create_account("paper", 10, NOW), ledger.create_account("paper", 10, NOW)
    for acc in (mine, other):
        with ledger.atomic():
            pid = ledger.insert_position(acc, symbol="SOLUSDT", side=Side.LONG, qty=1, margin=10, entry_price=100,
                                         entry_time=NOW - 600, best_price=100, best_time=NOW, worst_price=100,
                                         worst_time=NOW, liq_price=90, fee=0, funding=0).id
            ledger.insert_trade(Trade(0, acc, pid, "SOLUSDT", Side.LONG, 1, 10, 100, NOW - 600, 99, NOW, "sl",
                                      -1, 0.1, 0, 104, 98.5, (104.5,), 98.9))
    monkeypatch.setenv("OPUS_CORE_DB", str(tmp_path / "core.db"))
    monkeypatch.setenv("OPUS_ACCOUNT", str(mine))
    text = tools.cikis_karnesi(10)
    assert text.count("SOLUSDT long") == 1
    assert "Değmeyen en yakın TP 104.5, fiyat yüzde 0.50 kala döndü" in text
    assert "TP'ye yüzde 0.6'dan az farkla varamayan işlem 1" in text


def test_trade_history_tells_the_effective_leverage(tmp_path, monkeypatch):
    """Kaldıraç paritenin ayarıdır, işlem kaydında tutulmaz. Araç büyüklüğün marjine oranını etkin kaldıraç diye
    yazar, Opus onu paritenin bugünkü ayarıyla karıştırmaz."""
    con = connect(tmp_path / "core.db")
    migrate(con, MIGRATIONS, tmp_path / "bak", catalog=catalog())
    ledger = Ledger(con)
    acc = ledger.create_account("paper", 10, NOW)
    with ledger.atomic():
        pid = ledger.insert_position(acc, symbol="SOLUSDT", side=Side.SHORT, qty=2, margin=8, entry_price=100,
                                     entry_time=NOW - 600, best_price=100, best_time=NOW, worst_price=100,
                                     worst_time=NOW, liq_price=110, fee=0, funding=0).id
        ledger.insert_trade(Trade(0, acc, pid, "SOLUSDT", Side.SHORT, 2, 8, 100, NOW - 600, 98, NOW, "tp",
                                  4, 0.2, 0, 98, 100.5, (98,), None))
    monkeypatch.setenv("OPUS_CORE_DB", str(tmp_path / "core.db"))
    monkeypatch.setenv("OPUS_ACCOUNT", str(acc))
    assert tools.gecmis_islemlerim(5).startswith(f"#{pid} SOLUSDT short etkin kaldıraç 25.0x, miktar 2 marjin 8.00, "
                                                 f"giriş 100")


def _profile(con, acc):
    con.execute("INSERT INTO profiles (id, name, short, area, account_id, created_at) VALUES (1, 'A', 'a', 'paper', ?, ?)",
                (acc, NOW))
    con.execute("INSERT INTO profile_versions (id, profile_id, number, config, created_at) VALUES (1, 1, 1, '{}', ?)",
                (NOW,))
    con.commit()


def test_history_tools_show_the_review_and_the_entry_thesis(tmp_path, monkeypatch):
    """Sınıflandırma işlemin kaydına bağlanır, Opus kendi işlemlerine baktığında görür. Öğrenme ölçülebilir olur."""
    from opus_trade.storage.notes import NotesRepo
    con = connect(tmp_path / "core.db")
    migrate(con, MIGRATIONS, tmp_path / "bak", catalog=catalog())
    ledger = Ledger(con)
    acc = ledger.create_account("paper", 10, NOW)
    _profile(con, acc)
    with ledger.atomic():
        pid = ledger.insert_position(acc, symbol="SOLUSDT", side=Side.SHORT, qty=2, margin=8, entry_price=100,
                                     entry_time=NOW - 600, best_price=100, best_time=NOW, worst_price=100,
                                     worst_time=NOW, liq_price=110, fee=0, funding=0).id
        ledger.insert_trade(Trade(0, acc, pid, "SOLUSDT", Side.SHORT, 2, 8, 100, NOW - 600, 101, NOW, "sl",
                                  -2, 0.2, 0, 99.5, 101, (), 101))
    notes = NotesRepo(con)
    notes.add_entry(1, acc, "SOLUSDT", "short", {}, 101.5, "101,5 üstü kapanış", NOW - 600, position_id=pid)
    notes.add_review(pid, 1, None, "zamanlama_yanlisti", "Erken girdim.", NOW)
    monkeypatch.setenv("OPUS_CORE_DB", str(tmp_path / "core.db"))
    monkeypatch.setenv("OPUS_ACCOUNT", str(acc))
    line = tools.gecmis_islemlerim(5)
    assert line.endswith("geçersizlik 101.5: 101,5 üstü kapanış, sınıflandırman zamanlama yanlıştı: Erken girdim.")
    assert "sınıflandırman zamanlama yanlıştı" in tools.cikis_karnesi(5)


def test_decision_history_reads_both_record_shapes(tmp_path, monkeypatch):
    """Eski kayıtlar görüş ve güven, yenileri rejim taşır. Araç ikisini de okur, eski kayıt yüzünden çökmez."""
    import json
    from opus_trade.storage.journal import JournalRepo
    con = connect(tmp_path / "core.db")
    migrate(con, MIGRATIONS, tmp_path / "bak", catalog=catalog())
    _profile(con, Ledger(con).create_account("paper", 10, NOW))
    j = JournalRepo(con)
    j.add(1, 1, NOW - 60, [], [], {"decision": {"gorus": "dusus", "guven": 60, "ozet": "eski", "analiz": ""}}, None,
          None)
    new = {"ozet": "yeni", "analiz": "", "pariteler": {"BTCUSDT": {"rejim": {"rejim": "donus", "uygun": "hayir"}}}}
    j.add(1, 1, NOW, [], [], {"decision": new, "rejection": ["x"]}, None, None)
    monkeypatch.setenv("OPUS_CORE_DB", str(tmp_path / "core.db"))
    monkeypatch.setenv("OPUS_PROFILE", "1")
    text = tools.gecmis_kararlarim(5)
    assert "BTCUSDT dönüş, uygun değil, kontrol hatasıyla reddedildi\n  özet: yeni" in text
    assert "görüş dusus, güven 60\n  özet: eski" in text


def test_as_of_cuts_the_clock_and_the_order_book(tmp_path, monkeypatch):
    """Sınamada araçlar geçmiş anın saatini kullanır, o andan sonraki likidasyon görünmez, emir defteri geçmişi yoktur."""
    con = connect(tmp_path / "market.db")
    migrate(con, MARKET_MIGRATIONS, tmp_path / "bak")
    CollectorStore(con).add_liquidations([Liquidation(NOW - 60, "SOLUSDT", Side.LONG, 100.0, 1000.0),
                                          Liquidation(NOW + 60, "SOLUSDT", Side.SHORT, 101.0, 10.0)])
    monkeypatch.setenv("OPUS_MARKET_DB", str(tmp_path / "market.db"))
    monkeypatch.setenv("OPUS_AS_OF", str(NOW))
    monkeypatch.setattr(tools, "_market", None)
    text = tools.likidasyonlar("SOLUSDT", 1)
    assert "long likidasyonları 1 kayıt" in text and "short likidasyonları 0 kayıt" in text
    assert "emir defteri yoktur" in asyncio.run(tools.emir_defteri("SOLUSDT"))
    assert type(tools._rest()).__name__ == "AsOfMarket"
    monkeypatch.setattr(tools, "_market", None)
