"""
file: test_cards.py
importable: yok

Parite kartındaki yapı satırının testleri. Opus şemanın 2. adımında yönü bu
rakamlardan okur. Satır yanlış aralığı ya da yanlış sırayı gösterirse Opus
doğru okuduğu halde kontrol hatası alır.

description: strategy.llm.cards yapı satırı için birim testleri.
"""

from opus_trade.market.models import Candle
from opus_trade.strategy.llm import cards
from opus_trade.strategy.llm.texts import clock, price

H = 3600


def _candles(step: int, highs: dict[int, float], lows: dict[int, float], n: int = 40) -> list[Candle]:
    return [Candle("BTCUSDT", i * step, 100, highs.get(i, 105), lows.get(i, 95), 100, 1, 100, 1, 0.5) for i in range(n)]


def test_structure_line_shows_last_three_tops_and_bottoms_per_interval_old_to_new():
    """Kartta profilin her yapı aralığı için son üç tepe ve dip eskiden yeniye yazar, üst aralık önce gelir."""
    hourly = _candles(H, {5: 120, 12: 118, 19: 116, 26: 114}, {8: 80, 15: 82, 22: 84, 29: 86})
    four = _candles(4 * H, {6: 130}, {14: 70})
    lines = cards._structure_lines({"1h": hourly, "4h": four}, ("4h", "1h"))
    assert lines[0] == f"4 saatlik son tepeler: {price(130)} {clock(24 * H)}. 4 saatlik son dipler: {price(70)} {clock(56 * H)}."
    assert lines[1].startswith(f"1 saatlik son tepeler: {price(118)} {clock(12 * H)}, {price(116)} {clock(19 * H)}, {price(114)} {clock(26 * H)}.")
    assert lines[1].endswith(f"1 saatlik son dipler: {price(82)} {clock(15 * H)}, {price(84)} {clock(22 * H)}, {price(86)} {clock(29 * H)}.")


def test_structure_line_says_none_when_there_is_no_swing():
    """Uç yoksa yok yazar, boş bir liste Opus'a eksik veri gibi görünmez."""
    flat = _candles(H, {}, {})
    assert cards._structure_lines({"1h": flat}, ("1h",)) == ["1 saatlik son tepeler: yok. 1 saatlik son dipler: yok."]
