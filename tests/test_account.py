"""
file: test_account.py
importable: yok

Hesap motorunun senaryo testleri. Her test bir pozisyonu, emri ya da alarmı
baştan sona yürütür ve nakdi, kayıtları ve Opus'a giden olay mesajlarını
kontrol eder. Proje klasöründe uv run pytest ile çalışır.

description: Account sınıfının Binance kurallarını, kaymayı, komisyonu ve fiyat kontrolünü doğru uyguladığını sınayan testler.
"""

# NOTE: Beklenen değerler koddan değil kuralın kendisinden elle hesaplanıp
# yazıldı, ara adımlar yorumlarda duruyor. BTC'de kayma binde 0.2, taker
# komisyonu binde 0.5, maker binde 0.2, miktar adımı 0.001. 50000'den on kat
# elli dolarlık long'un girişi 50010, miktarı 0.009, marjini 45.009, açılış
# komisyonu 0.225045 olur, nakitte 54.765955 kalır. Bir kural bilerek
# değişirse bu rakamlar da elle güncellenir.

import pytest

from opus_trade.engine.account import AccountError

from conftest import NOW

BTC = "BTCUSDT"
ENTRY, QTY, MARGIN, OPEN_FEE = 50010.0, 0.009, 45.009, 0.225045
CASH_AFTER_OPEN = 100 - MARGIN - OPEN_FEE


# Bütün testlerin ortak başlangıcı, 50000'den on kat elli dolarlık long.
def _long(account, **kw) -> dict:
    return account.open_position(BTC, "long", 10, 50, 50000, NOW, **kw)


def _trade(account, id_: int) -> dict:
    return account._trade(id_)


# --- Açılış ---

def test_open_long_applies_slippage_step_and_taker_fee(account):
    p = _long(account)
    assert p["entry_price"] == pytest.approx(ENTRY)
    assert p["qty"] == pytest.approx(QTY)
    assert p["margin"] == pytest.approx(MARGIN)
    assert p["fee"] == pytest.approx(OPEN_FEE)
    assert account.cash == pytest.approx(CASH_AFTER_OPEN)
    # Bakım marjı binde 4: (giriş * miktar - marjin) / (miktar * 0.996)
    assert p["liq_price"] == pytest.approx((ENTRY * QTY - MARGIN) / (QTY * 0.996))


def test_open_short_slips_down(account):
    p = account.open_position(BTC, "short", 10, 50, 50000, NOW)
    assert p["entry_price"] == pytest.approx(49990)
    assert p["qty"] == pytest.approx(0.010)
    assert p["liq_price"] == pytest.approx((49990 * 0.010 + 49.99) / (0.010 * 1.004))


@pytest.mark.parametrize("kw, fragment", [
    ({"symbol": "ETHUSDT"}, "katalogda yok"),
    ({"leverage": 101}, "kaldıraç 1 ile 100"),
    ({"margin": 0}, "Marjin sıfırdan büyük"),
    ({"margin": 4, "leverage": 1}, "en küçük işlem büyüklüğü"),
    ({"sl": 51000}, "SL 51000"),
    ({"tp": 49000}, "TP 49000"),
    ({"tp": 52000, "tp_levels": [{"price": 53000, "pct": 50}]}, "birlikte verilemez"),
    ({"tp_levels": [{"price": 52000, "pct": 60}, {"price": 53000, "pct": 60}]}, "en fazla 100"),
    ({"trail_pct": 60}, "İz süren stop"),
])
def test_open_rejects_invalid_requests(account, kw, fragment):
    args = {"symbol": BTC, "side": "long", "leverage": 10, "margin": 50, "price": 50000, "time": NOW, **kw}
    with pytest.raises(AccountError, match=fragment):
        account.open_position(**args)
    assert account.cash == 100
    assert account.positions() == []


# SOL 10 dolardan on kat yüz dolar: miktar 99.98, gereken para komisyonla
# 100.49 dolar. Fark yüzde ikinin altında olduğu için miktar nakde sığacak
# kadar küçülür: 100 / (1.0002 + 0.005001) = 99.48.
def test_slightly_short_cash_shrinks_size(account):
    p = account.open_position("SOLUSDT", "long", 10, 100, 10, NOW)
    assert p["qty"] == pytest.approx(99.48)
    assert 0 <= account.cash < 0.01


def test_clearly_short_cash_is_rejected(account):
    with pytest.raises(AccountError, match="Yetersiz nakit"):
        account.open_position("SOLUSDT", "long", 10, 103, 10, NOW)


# --- Kapanış ---

# İki kademe yüzde elli: ilk kademe 0.0045'ten adıma yuvarlanıp 0.004 olur,
# ikinci kademe kalanın tamamıdır. TP maker sayılır, kaymasız ve binde 0.2.
def test_tp_levels_close_in_parts_as_maker(account):
    p = _long(account, tp_levels=[{"price": 51000, "pct": 50}, {"price": 52000, "pct": 50}])
    assert [k["qty"] for k in p["tp_levels"]] == [0.004, None]

    events = account.check_price(BTC, 51500, 50500, NOW + 60)
    assert [e["kind"] for e in events] == ["tp"]
    part = events[0]["trade"]
    assert part["parent_id"] == p["id"] and part["qty"] == pytest.approx(0.004)
    assert part["exit_price"] == 51000
    assert part["pnl"] == pytest.approx((51000 - ENTRY) * 0.004)
    assert part["fee"] == pytest.approx(OPEN_FEE * 0.004 / QTY + 0.004 * 51000 * 0.0002)
    assert "0.004 birimlik kısmı TP 51000" in events[0]["message"]
    rest = account.positions()[0]
    assert rest["qty"] == pytest.approx(0.005) and len(rest["tp_levels"]) == 1

    events = account.check_price(BTC, 52100, 51800, NOW + 120)
    assert [e["kind"] for e in events] == ["tp"]
    assert "tamamı TP 52000" in events[0]["message"]
    assert account.positions() == []
    # Kısmi: 20.004 marjin + 3.96 kâr - 0.0408 komisyon. Kalan: 25.005 + 9.95 - 0.052.
    assert account.cash == pytest.approx(CASH_AFTER_OPEN + 20.004 + 3.96 - 0.0408 + 25.005 + 9.95 - 0.052)
    assert account.stats()["trade_count"] == 2


def test_sl_closes_with_slippage_and_taker_fee(account):
    _long(account, sl=49000)
    events = account.check_price(BTC, 50100, 48900, NOW + 60)
    assert [e["kind"] for e in events] == ["sl"]
    t = events[0]["trade"]
    assert t["exit_price"] == pytest.approx(49000 * 0.9998)
    pnl = (49000 * 0.9998 - ENTRY) * QTY
    assert t["pnl"] == pytest.approx(pnl)
    assert account.cash == pytest.approx(CASH_AFTER_OPEN + MARGIN + pnl - QTY * 49000 * 0.9998 * 0.0005)


def test_liquidation_takes_whole_margin(account):
    p = _long(account)
    events = account.check_price(BTC, 50000, 45000, NOW + 60)
    assert [e["kind"] for e in events] == ["liquidation"]
    t = events[0]["trade"]
    assert t["exit_price"] == pytest.approx(p["liq_price"])
    assert t["pnl"] == pytest.approx(-MARGIN)
    assert account.cash == pytest.approx(CASH_AFTER_OPEN)


# Aralık hem SL'yi hem likidasyonu geçer. Fiyat aşağı inerken önce 46000'e
# varır, pozisyon SL'den kaymayla kapanır ve marjinin tamamı gitmez.
def test_sl_comes_before_liquidation(account):
    _long(account, sl=46000)
    events = account.check_price(BTC, 50000, 45000, NOW + 60)
    assert [e["kind"] for e in events] == ["sl"]
    assert events[0]["trade"]["exit_price"] == pytest.approx(46000 * 0.9998)


# Likidasyon 45198 civarında, SL onun ötesinde 44000'de. Fiyat önce
# likidasyona varır.
def test_sl_beyond_liquidation_liquidates_first(account):
    p = _long(account, sl=44000)
    events = account.check_price(BTC, 50000, 43000, NOW + 60)
    assert [e["kind"] for e in events] == ["liquidation"]
    assert events[0]["trade"]["exit_price"] == pytest.approx(p["liq_price"])


def test_agent_partial_close_and_dust_rule(account):
    p = _long(account)
    part = account.close_position(p["id"], 50000, NOW + 60, "agent", ratio=0.5)
    assert part["parent_id"] == p["id"] and part["qty"] == pytest.approx(0.004)
    assert account.positions()[0]["qty"] == pytest.approx(0.005)
    # 0.0045 kapatılınca 0.0005 kalırdı, bir adımdan az olduğu için tamamı kapanır.
    last = account.close_position(p["id"], 50000, NOW + 120, "agent", qty=0.0045)
    assert last["id"] == p["id"] and last["status"] == "closed"
    assert account.positions() == []


# --- İz süren stop ---

# Yüzde iki iz: en iyi fiyat 51000 olunca stop 49980'e çıkar, fiyat oraya
# inince stop SL gibi kaymayla kapanır.
def test_trailing_stop_follows_best_price(account):
    _long(account, trail_pct=2)
    assert account.check_price(BTC, 51000, 50500, NOW + 60) == []
    p = account.positions()[0]
    assert p["best_price"] == 51000 and p["sl"] == pytest.approx(49980)
    events = account.check_price(BTC, 50000, 49900, NOW + 120)
    assert [e["kind"] for e in events] == ["sl"]
    assert events[0]["trade"]["exit_price"] == pytest.approx(49980 * 0.9998)


# İz sonradan eklenirse açılıştan beri görülen en iyi fiyat kullanılır. 51000
# görüldükten sonra fiyat 50000'deyken yüzde bir iz 50490'a düşer, fiyat bunu
# zaten geçmiş. En küçük geçerli iz 1 - 50000 / 51000 = yüzde 1.96.
def test_late_trailing_stop_behind_price_is_rejected(account):
    p = _long(account)
    account.check_price(BTC, 51000, 50500, NOW + 60)
    with pytest.raises(AccountError, match="en az yüzde 1.96"):
        account.update_protection(p["id"], 50000, trail_pct=1)
    moved = account.update_protection(p["id"], 50000, trail_pct=3)
    assert moved["sl"] == pytest.approx(51000 * 0.97)


def test_update_protection_removes_with_zero(account):
    p = _long(account, tp=52000, sl=49000)
    p = account.update_protection(p["id"], 50000, tp=0, sl=0)
    assert p["tp_levels"] == [] and p["sl"] is None
    with pytest.raises(AccountError, match="SL 50500"):
        account.update_protection(p["id"], 50000, sl=50500)


# --- Ekleme ---

# Eklenen parça 51000'den taker: giriş 51010.2, miktar 0.009, marjin 45.90918.
# Ortalama giriş (50010 + 51010.2) / 2, kaldıraç toplam büyüklük / toplam marjin.
def test_add_to_position_averages_entry(account):
    p = _long(account)
    p = account.add_to_position(p["id"], 10, 50, 51000)
    assert p["qty"] == pytest.approx(0.018)
    assert p["entry_price"] == pytest.approx((ENTRY + 51010.2) / 2)
    assert p["margin"] == pytest.approx(MARGIN + 45.90918)
    assert p["leverage"] == pytest.approx(10.0)
    assert account.cash == pytest.approx(CASH_AFTER_OPEN - 45.90918 - 0.009 * 51010.2 * 0.0005)


# --- Emirler ---

# Fiyatın lehine gelen emir limit gibi maker sayılır: tetikten kaymasız
# açılır. 49000'den on kat elli dolar: miktar 0.010, marjin 49, komisyon 0.098.
def test_order_below_fills_as_maker_at_trigger(account):
    o = account.place_order(BTC, "long", 49000, 10, 50, 50000, NOW)
    assert o["condition"] == "below" and o["maker"] == 1
    events = account.check_price(BTC, 49500, 48900, NOW + 60)
    assert [e["kind"] for e in events] == ["order_filled"]
    t = events[0]["trade"]
    assert t["entry_price"] == 49000 and t["qty"] == pytest.approx(0.010)
    assert t["fee"] == pytest.approx(0.098) and t["order_id"] == o["id"]
    assert account.pending_orders() == []


def test_breakout_order_is_taker_with_slippage(account):
    o = account.place_order(BTC, "long", 51000, 10, 50, 50000, NOW)
    assert o["condition"] == "above" and o["maker"] == 0
    events = account.check_price(BTC, 51100, 50900, NOW + 60)
    assert events[0]["trade"]["entry_price"] == pytest.approx(51000 * 1.0002)


def test_order_expires(account):
    account.place_order(BTC, "long", 49000, 10, 50, 50000, NOW, valid_min=5)
    assert account.check_price(BTC, 50100, 49950, NOW + 299) == []
    events = account.check_price(BTC, 50100, 49950, NOW + 300)
    assert [e["kind"] for e in events] == ["order_expired"]
    assert account.pending_orders() == []


# Emir kaydedilirken nakit kontrol edilmez, gerçekleşme anında edilir.
def test_order_that_cannot_open_is_cancelled(account):
    o = account.place_order(BTC, "long", 49000, 10, 500, 50000, NOW)
    events = account.check_price(BTC, 49500, 48900, NOW + 60)
    assert [e["kind"] for e in events] == ["order_failed"]
    assert "Yetersiz nakit" in events[0]["message"]
    assert account.pending_orders() == [] and account.positions() == []
    status = account.con.execute("SELECT status FROM orders WHERE id = ?", (o["id"],)).fetchone()[0]
    assert status == "cancelled"


# 49000'den on kat on beş dolar 0.00306 eder, adıma yuvarlanıp 0.003 olur.
# Yüzde otuz üçlük kademe 0.00099 eder ve sıfıra düşer. Emir tetik anında
# açılamayacağı için kurulurken reddedilir.
def test_order_with_unsplittable_levels_is_rejected(account):
    levels = [{"price": 50000 + 1000 * i, "pct": 33} for i in range(1, 4)]
    with pytest.raises(AccountError, match="en küçük adımın"):
        account.place_order(BTC, "long", 49000, 10, 15, 50000, NOW, tp_levels=levels)
    assert account.pending_orders() == []


def test_alert_triggers_once(account):
    account.set_alert(BTC, 51000, "direnç", 50000, NOW)
    assert account.check_price(BTC, 50500, 49900, NOW + 60) == []
    events = account.check_price(BTC, 51000, 50000, NOW + 120)
    assert [e["kind"] for e in events] == ["alert"]
    assert "Alarm notun: direnç" in events[0]["message"]
    assert account.check_price(BTC, 51500, 50500, NOW + 180) == []


# --- Fonlama ---

# Oran pozitifken long öder, short alır. Ödeme büyüklük üzerinden hesaplanır
# ve marjinden düşer: long 0.009 * 50000 * 0.0001 = 0.045 öder, short
# 0.010 * 50000 * 0.0001 = 0.05 alır.
def test_funding_long_pays_short_receives(account):
    long_ = _long(account)
    short = account.open_position(BTC, "short", 10, 50, 50000, NOW)
    events = account.apply_funding(BTC, 0.0001, 50000, NOW + 3600)
    assert [e["kind"] for e in events] == ["funding", "funding"]
    assert "ödedi" in events[0]["message"] and "aldı" in events[1]["message"]
    a, b = account.positions()
    assert a["margin"] == pytest.approx(MARGIN - 0.045) and a["funding"] == pytest.approx(0.045)
    assert b["margin"] == pytest.approx(49.99 + 0.05) and b["funding"] == pytest.approx(-0.05)
    assert a["liq_price"] > long_["liq_price"] and b["liq_price"] > short["liq_price"]


def test_funding_skips_positions_opened_later(account):
    _long(account)
    assert account.apply_funding(BTC, 0.0001, 50000, NOW - 1) == []


def test_funding_that_exhausts_margin_liquidates(account):
    _long(account)
    events = account.apply_funding(BTC, 0.2, 50000, NOW + 3600)
    assert [e["kind"] for e in events] == ["liquidation"]
    assert events[0]["trade"]["pnl"] == pytest.approx(-MARGIN)
    assert account.positions() == []


# --- Özet ---

def test_equity_counts_open_profit(account):
    _long(account)
    assert account.equity({BTC: 51000, "SOLUSDT": 10}) == pytest.approx(CASH_AFTER_OPEN + MARGIN + (51000 - ENTRY) * QTY)
