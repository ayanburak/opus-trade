"""
file: test_numbers.py
importable: yok

Cümle içindeki sayıların Türkçe yazımının testleri. Noktalı ondalık panelde ve
Opus'un okuduğu cümlelerde İngilizce görünür. Binlik ayırıcı çıkarsa 1.608
gibi bir fiyat bin altı yüz sekiz mi bir virgül altı yüz sekiz mi, okunamaz.

description: domain.numbers için birim testleri.
"""

from opus_trade.domain import numbers


def test_num_writes_comma_and_no_thousands_separator():
    """Ondalık virgülle ayrılır, büyük sayıda binlik ayırıcı yoktur."""
    assert numbers.num(5.6, 2) == "5,60"
    assert numbers.num(1608.91) == "1608,91"
    assert numbers.num(1234567.5) == "1234567,5"


def test_num_without_digits_drops_trailing_zeros_and_float_noise():
    """Basamak verilmezse sayı kendi haliyle yazılır. Kayan nokta kırıntısı 0,30000000000000004 diye görünmez."""
    assert numbers.num(0.041) == "0,041"
    assert numbers.num(100.0) == "100"
    assert numbers.num(0.1 + 0.2) == "0,3"


def test_num_sign_marks_profit_and_loss():
    """Net sonuçta artı ve eksi işareti kâr ile zararı ayırır."""
    assert numbers.num(2.1, 2, sign=True) == "+2,10"
    assert numbers.num(-2.1, 2, sign=True) == "-2,10"


def test_price_and_qty():
    """Fiyat büyüklüğüne göre basamak alır, miktar paritenin birimiyle yazılır."""
    assert numbers.price(97.98) == "97,98"
    assert numbers.price(1608.94) == "1608,9"
    assert numbers.price(None) == "yok"
    assert numbers.qty(0.041, "ETHUSDT") == "0,041 ETH"


def test_small_prices_keep_their_digits():
    """Bir doların altındaki fiyat yuvarlanıp başka bir fiyata dönüşmez. Sabit dört basamak 0,001148'lik bir emri
    Opus'a 0,0011 diye gösteriyordu. Opus'un gördüğü noktalı yazı da aynı kuraldadır. Büyük fiyatlar değişmez."""
    from opus_trade.strategy.llm.texts import price
    assert numbers.price(0.001148) == "0,001148" and price(0.001148) == "0.001148"
    assert numbers.price(0.00105) == "0,00105" and price(0.000012345) == "0.000012345"
    assert numbers.price(0.5) == "0,5000" and price(1.2345) == "1.2345" and price(59822.3) == "59822.3"
