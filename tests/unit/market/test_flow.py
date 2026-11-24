"""
file: test_flow.py
importable: yok

Saniyelik akış özetinin testleri. Sinyalin bütün eşikleri bu sayılardan
okunur: bir pencerenin bir saniye kayması ya da sessiz saniyelerin yanlış
doldurulması olmayan bir patlamayı gösterir ya da gerçek olanı gizler.
Beklenen rakamlar elle hesaplandı.

description: market.flow için birim testleri.
"""

import pytest

from opus_trade.market.flow import TradeFlow
from opus_trade.market.models import Trade

S = "DOGEUSDT"
T0 = 1_795_000_000


def tr(sec: float, price: float, qty: float = 1, buy: bool = True) -> Trade:
    return Trade(S, int(sec * 1000), price, qty, buy)


def test_window_sums_count_the_current_second_and_skip_older_ones():
    """Son 5 saniye, içinde bulunulan yarım saniye dahil beş hanedir. Altıncı saniye geriye taşan bir işlem
    sayılırsa hız katı şişer."""
    f = TradeFlow(window_s=60)
    f.add(tr(T0, 10, 1))                  # 10 dolar, alıcı
    f.add(tr(T0 + 1, 10, 2, buy=False))   # 20 dolar, satıcı
    f.add(tr(T0 + 5, 11, 1))              # 11 dolar, alıcı
    f.add(tr(T0 + 5.5, 12, 1))            # 12 dolar, alıcı, aynı saniye
    assert f.count(S, 5) == 3 and f.usd(S, 5) == 43 and f.buy_usd(S, 5) == 23
    assert f.count(S, 6) == 4 and f.usd(S, 6) == 53


def test_back_moves_the_window_into_the_past():
    """Normal, patlamanın kendisini saymadan önceki dakikalardan ölçülür. Pencere geriye kaymazsa patlama kendi
    normalini büyütür ve hız katı küçük görünür."""
    f = TradeFlow(window_s=60)
    for s in range(10):
        f.add(tr(T0 + s, 10))
    for _ in range(9):
        f.add(tr(T0 + 10, 10))
    assert f.count(S, 1) == 9
    assert f.count(S, 5, back=1) == 5


def test_move_compares_with_the_close_of_the_earlier_second():
    """5 saniyelik hareket, 5 saniye önceki saniyenin son fiyatına göredir. Sessiz saniye bir önceki fiyatı taşır."""
    f = TradeFlow(window_s=60)
    f.add(tr(T0, 100))
    f.add(tr(T0 + 0.9, 101))
    f.add(tr(T0 + 5, 104.03))
    assert f.price_at(S, 5) == 101
    assert f.price_at(S, 2) == 101
    assert f.move(S, 5) == pytest.approx(0.03)


def test_mean_weights_quiet_seconds_by_time():
    """Fiyatın 20 dakikalık ortalaması zamana göredir. Yalnızca işlem olan saniyeler sayılsa, sakin bir coin'in
    ortalaması patlamanın birkaç saniyesine çekilir ve kopma küçük görünür."""
    f = TradeFlow(window_s=60)
    f.add(tr(T0, 10))
    f.add(tr(T0 + 3, 20))
    # Saniyelerin son fiyatları: 10, 10, 10, 20
    assert f.mean(S, 4) == 12.5
    assert f.mean(S, 2) == 15
    assert f.mean(S, 60) == 12.5 and f.span(S) == 4


def test_long_silence_resets_and_forget_drops_the_symbol():
    """Pencereden uzun süren sessizlikten sonra eski sayılar yeni pencereye karışmamalı. Dinlenmeyen parite
    unutulmazsa yeniden dinlendiğinde aradaki saniyeler eski fiyatla dolar ve olmayan bir hareket görünür."""
    f = TradeFlow(window_s=10)
    f.add(tr(T0, 10))
    f.add(tr(T0 + 50, 20))
    assert f.count(S, 10) == 1 and f.span(S) == 1 and f.price_at(S, 5) == 20
    f.forget(set())
    assert not f.has(S)


def test_ring_keeps_exactly_the_window():
    """En uzun pencere tam olarak tutulmalı. Halka bir hane kısa olsaydı 20 dakikalık pencerenin ilk hanesi
    ezilir ve toplam yanlış çıkardı."""
    f = TradeFlow(window_s=10)
    for s in range(25):
        f.add(tr(T0 + s, 10 + s))
    assert f.count(S, 10) == 10
    assert f.count(S, 30) == 10
    assert f.mean(S, 10) == pytest.approx(sum(10 + s for s in range(15, 25)) / 10)
