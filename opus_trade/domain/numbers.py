"""
file: numbers.py
importable: price_digits, num, price, qty

Cümle içinde geçen sayıların Türkçe yazımı. Ondalık ayırıcı virgüldür,
binlik ayırıcı kullanılmaz: 1608,91. num() bir sayıyı istenen basamakla ya da
gereksiz sıfırları atarak yazar. price() bir fiyatı büyüklüğüne yetecek
basamakla, qty() bir miktarı paritenin birimiyle yazar.

description: Kural hatalarında, hesap olaylarında ve bildirimlerde sayıların Türkçe yazımı.
"""

# NOTE: Binlik ayırıcı bilerek yok. Türkçede binlik ayırıcı noktadır ve Opus
# fiyatları JSON'a noktalı ondalıkla yazar. 1.608 bin altı yüz sekiz mi bir
# virgül altı yüz sekiz mi, karışırdı.
#
# NOTE: Bu biçim yalnızca cümleler içindir. Talimatın tabloları, parite kartları,
# hesap bölümü ve piyasa araçları noktalı ondalıkla kalır, Opus oradan
# kopyaladığı sayıyı JSON'a aynen yazabilsin. Burak'ın 21.11 kararı.


# Fiyatın basamak sayısı büyüklüğüne göre seçilir. Bin doların üstünde bir
# basamak yeter, on doların altında dört basamak gerekir.
def price_digits(x: float) -> int:
    return 1 if x >= 1000 else 2 if x >= 10 else 4


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
    return "yok" if x is None else num(x, price_digits(x))


# Birim sembolün USDT'siz hali sayılır. Masadaki bütün pariteler USDT
# paritesi olduğu için bu yeterli.
def qty(x: float, symbol: str) -> str:
    return f"{num(x)} {symbol.removesuffix('USDT')}"
