"""
file: numbers.py
importable: price_digits, price_text, num, price, qty

Cümle içinde geçen sayıların Türkçe yazımı. Ondalık ayırıcı virgüldür,
binlik ayırıcı kullanılmaz: 1608,91. num() bir sayıyı istenen basamakla ya da
gereksiz sıfırları atarak yazar. price() bir fiyatı büyüklüğüne yetecek
basamakla, qty() bir miktarı paritenin birimiyle yazar. price_text() aynı
fiyatı noktalı ondalıkla yazar, talimatın, kartların ve hesap bölümünün
fiyatları oradan gelir.

description: Kural hatalarında, hesap olaylarında ve bildirimlerde sayıların Türkçe yazımı.
"""

# NOTE: Binlik ayırıcı bilerek yok. Türkçede binlik ayırıcı noktadır ve Opus
# fiyatları JSON'a noktalı ondalıkla yazar. 1.608 bin altı yüz sekiz mi bir
# virgül altı yüz sekiz mi, karışırdı.
#
# NOTE: Bu biçim yalnızca cümleler içindir. Talimatın tabloları, parite kartları,
# hesap bölümü ve piyasa araçları noktalı ondalıkla kalır, Opus oradan
# kopyaladığı sayıyı JSON'a aynen yazabilsin. Burak'ın 21.11 kararı.

import math


# Fiyatın basamak sayısı büyüklüğüne göre seçilir. Bin doların üstünde bir
# basamak yeter, on doların altında dört basamak gerekir. Bir doların altında
# beş anlamlı haneye yetecek kadar basamak verilir. Sabit dört basamak
# 0,001148'lik AKEUSDT'yi 0,0011 yazıyordu, Opus kendi emrinin fiyatını yüzde
# dört yanlış görecekti, 27.11.
def price_digits(x: float) -> int:
    if x >= 1000:
        return 1
    if x >= 10:
        return 2
    if x >= 1 or x <= 0:
        return 4
    return max(4, 4 - math.floor(math.log10(x)))


# Bir doların altında sondaki sıfırlar atılır, en az dört basamak kalır.
# 0,00105 0,0010500 diye yazılmasın, 0,5 de eskisi gibi 0,5000 kalsın.
def price_text(x: float) -> str:
    """Fiyatı noktalı ondalıkla, büyüklüğüne yetecek basamakla yazar. Örneğin 59822.3, 97.98 ya da 0.001148."""
    d = price_digits(x)
    text = f"{x:.{d}f}"
    if d > 4:
        head, tail = text.split(".")
        text = f"{head}.{tail.rstrip('0').ljust(4, '0')}"
    return text


def num(x: float, digits: int | None = None, sign: bool = False) -> str:
    """Sayıyı virgüllü ondalıkla yazar.

    Args:
        x: Sayı.
        digits: Ondalık basamak sayısı. None ise sayı kendi basamaklarıyla, sondaki sıfırlar atılarak yazılır.
        sign: True ise pozitif sayının başına artı konur.

    Returns:
        Örneğin 5,60 ya da +12,40 ya da 0,041.
    """
    plus = "+" if sign else ""
    if digits is None:
        text = f"{round(x, 10):{plus}.10f}".rstrip("0").rstrip(".")
    else:
        text = f"{x:{plus}.{digits}f}"
    return text.replace(".", ",")


def price(x: float | None) -> str:
    return "yok" if x is None else price_text(x).replace(".", ",")


# Birim sembolün USDT'siz hali sayılır. Masadaki bütün pariteler USDT
# paritesi olduğu için bu yeterli.
def qty(x: float, symbol: str) -> str:
    return f"{num(x)} {symbol.removesuffix('USDT')}"
