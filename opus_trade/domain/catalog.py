"""
file: catalog.py
importable: Catalog, SYMBOLS, SLIPPAGE, slippage_for

Catalog işlem yapılabilen paritelerin kurallarını sembolle bulur. Katalog
masanın sabit paritelerini ve Binance'te işleme açık bütün USDT perp'lerini
taşır. get() katalogda olmayan bir sembolde RuleError fırlatır. SYMBOLS
masanın sabit paritelerini ve kısa Türkçe adlarını, SLIPPAGE masanın
paritelerinin ortak kaymasını tutar. slippage_for() diğer paritelerin
kaymasını yirmi dört saatlik dolar hacminden bulur. Kuralların kendisi burada yazılı
değildir, çekirdek açılırken Binance'ten okur.

description: İşlem yapılabilen paritelerin kural kataloğu.
"""

# NOTE: Masanın paritelerinde kayma aynı, yüzde 0,02'dir. Bu hesabın işlem
# büyüklüklerinde gerçek kayma bu paritelerde en fazla bir fiyat adımı kadardır.
# Paritelere farklı kayma vermek BTC'ye gerçekte olmayan bir maliyet avantajı
# sağlıyordu.
#
# NOTE: Diğer paritelerin kayması hacme göre kademelidir. Küçük bir coin'de
# iğne anında piyasa emri defterin birkaç kademesini birden yer, sabit yüzde
# 0,02 küçük hesabı olduğundan iyi gösterirdi. Hacmi bilinmeyen parite en
# kötü kademeyi alır. Hacim çekirdek açılırken okunur, kademe gün içinde
# değişmez.
#
# NOTE: Fiyat adımı, miktar adımı ve diğer sınırlar elle yazılmaz. Binance
# bunları zaman zaman değiştirir, elle yazılmış bir liste canlının reddedeceği
# emri kâğıt hesapta kabul ederdi. Burak'ın 21.11 kararı.

from .errors import RuleError
from .models import SymbolSpec

# Masanın sabit pariteleri, Binance'teki sembol ve paritenin ne olduğunu
# anlatan kısa ad. Bu pariteler katalogda her zaman bulunur, biri eksikse
# çekirdek açılmaz. Ad boşsa arayüz sembolün başını kullanır.
SYMBOLS = {"BTCUSDT": "", "ETHUSDT": "", "SOLUSDT": "", "XRPUSDT": "", "XAUUSDT": "altın", "ZECUSDT": ""}

SLIPPAGE = 0.0002

# Yirmi dört saatlik dolar hacminin alt sınırı ve o kademenin kayması.
_TIERS = ((500e6, 0.0002), (50e6, 0.0005), (5e6, 0.001))
_WORST = 0.002


def slippage_for(volume: float | None) -> float:
    if volume is None:
        return _WORST
    return next((slip for floor, slip in _TIERS if volume >= floor), _WORST)


class Catalog:
    """Sembolden parite kuralına erişim. Sıra verilen sıradır."""

    def __init__(self, specs: tuple[SymbolSpec, ...]) -> None:
        self._specs = {s.symbol: s for s in specs}

    def get(self, symbol: str) -> SymbolSpec:
        """Sembolün kurallarını döndürür.

        Raises:
            RuleError: Sembol katalogda yoksa.
        """
        try:
            return self._specs[symbol]
        except KeyError:
            raise RuleError(f"{symbol} işlem yapılabilen pariteler arasında yok.") from None

    @property
    def symbols(self) -> list[str]:
        return list(self._specs)

    def __contains__(self, symbol: str) -> bool:
        return symbol in self._specs
