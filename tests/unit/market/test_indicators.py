"""
file: test_indicators.py
importable: yok

Gösterge ve analiz hesaplarının testleri. Bir gösterge serisi mumlarla
kayarsa ya da formülü bozulursa strateji yanlış ana ya da yanlış değere
bakar ve bunu hiçbir hata göstermez.

description: market.indicators ve market.analysis için birim testleri.
"""

import math

import pytest

from opus_trade.market import analysis, indicators
from opus_trade.market.models import Candle


def candle(t: int, high: float, low: float, close: float, volume: float = 1) -> Candle:
    return Candle("BTCUSDT", t, close, high, low, close, volume, volume * close, 1, volume / 2)


def test_rsi_uses_wilder_smoothing_and_stays_aligned():
    """Periyodu iki olan RSI elle hesaplandı. Seri mumlarla aynı uzunlukta, hesaplanamayan baş None olmalı."""
    out = indicators.rsi([10, 11, 10, 12], period=2)
    assert out[:2] == [None, None]
    assert out[2] == pytest.approx(50)
    assert out[3] == pytest.approx(100 - 100 / 6)


def test_atr_series_is_as_long_as_candles():
    """ATR ilk mumda hesaplanamaz. Seri bir kısa dönerse her değer bir önceki muma yazılmış olur."""
    cs = [candle(i, 101, 99, 100) for i in range(20)]
    out = indicators.atr(cs, period=14)
    assert len(out) == 20
    assert out[13] is None and out[14] == pytest.approx(2)


def test_stochastic_flat_window_does_not_divide_by_zero():
    """Fiyatın hiç oynamadığı pencerede %K tanımsızdır, sıfıra bölme yerine ortadaki değeri verir."""
    cs = [candle(i, 100, 100, 100) for i in range(5)]
    assert indicators.stochastic(cs, period=3, smooth=2)["k"][2:] == [50, 50, 50]


def test_levels_merge_nearby_tops_into_one_level():
    """Birbirine yarım ATR'den yakın iki tepe tek seviyedir. Ayrı sayılırsa seviyenin gücü küçük görünür."""
    highs = {10: 110, 22: 110.5, 34: 120}
    cs = [candle(i, highs.get(i, 101), 99, 100) for i in range(45)]
    found = analysis.levels(cs)
    assert [(round(x.price, 2), x.touches) for x in found] == [(110.25, 2), (120, 1)]


def test_correlation_pairs_returns_by_time():
    """Hafta sonu işlem görmeyen altın gibi bir varlık yalnızca ortak anlarda karşılaştırılır. Sıra ile eşleşse getiriler kayardı."""
    closes = [100, 101, 99, 102, 103, 101, 104]
    a = [candle(t, c, c, c) for t, c in enumerate(closes)]
    b = [candle(t, c, c, c) for t, c in enumerate(closes)] + [candle(t, 50, 50, 50) for t in range(100, 103)]
    corr, n = analysis.correlation({"A": a, "B": b})[("A", "B")]
    assert n == 6
    assert corr == pytest.approx(1)


def test_correlation_with_too_few_points_is_nan():
    """Üçten az ortak getiriyle korelasyon anlamsızdır, sayı gibi görünmemeli."""
    a = [candle(t, 100 + t, 100, 100 + t) for t in range(3)]
    corr, n = analysis.correlation({"A": a, "B": a})[("A", "B")]
    assert n == 2 and math.isnan(corr)


def test_swings_come_in_time_order_with_their_kind():
    """Yapı tepe ve diplerin zaman sırasıyla okunur. Fiyata göre sıralansa alçalan tepe yükselen gibi görünürdü."""
    highs, lows = {5: 120, 15: 115}, {10: 90, 20: 95}
    cs = [candle(i, highs.get(i, 105), lows.get(i, 100), 102) for i in range(30)]
    found = analysis.swings(cs, 3)
    assert [(x.time, x.price, x.kind) for x in found] == [(5, 120, "high"), (10, 90, "low"), (15, 115, "high"),
                                                         (20, 95, "low")]


def test_swing_without_enough_candles_on_its_right_is_not_counted():
    """Son mum henüz kapanmamış olabilir. Sağında pencere kadar mum olmayan uç sonradan bozulabilir, sayılmaz."""
    cs = [candle(i, 105, 100, 102) for i in range(20)] + [candle(20, 130, 100, 120), candle(21, 110, 100, 105)]
    assert analysis.swings(cs, 3) == []
    cs += [candle(22, 108, 100, 104), candle(23, 107, 100, 103)]
    assert [(x.time, x.kind) for x in analysis.swings(cs, 3)] == [(20, "high")]


def test_levels_and_swings_share_the_same_extremes():
    """Seviyeler ve yapı aynı uç bulma kodunu kullanır, iki yöntem aynı grafikte farklı tepe görmez."""
    highs = {10: 110, 22: 110.5, 34: 120}
    cs = [candle(i, highs.get(i, 101), 99, 100) for i in range(45)]
    tops = [x.price for x in analysis.swings(cs) if x.kind == "high"]
    assert tops == [110, 110.5, 120]
    assert sum(x.touches for x in analysis.levels(cs)) == len(analysis.swings(cs))
