"""
file: catalog.py
importable: Catalog, SYMBOLS, SLIPPAGE

Catalog işlem yapılabilen paritelerin kurallarını sembolle bulur. get()
katalogda olmayan bir sembolde RuleError fırlatır, mesajı işlem yapılabilen
paritelerin listesini verir. SYMBOLS masanın paritelerini ve kısa Türkçe
adlarını, SLIPPAGE bütün paritelerin ortak kaymasını tutar. Kuralların
kendisi burada yazılı değildir, çekirdek açılırken Binance'ten okur.

description: İşlem yapılabilen paritelerin kural kataloğu.
"""

# NOTE: Kayma bütün paritelerde aynı, yüzde 0,02'dir. Bu hesabın işlem
# büyüklüklerinde gerçek kayma her paritede en fazla bir fiyat adımı kadardır.
# Paritelere farklı kayma vermek BTC'ye gerçekte olmayan bir maliyet avantajı
# sağlıyordu.
#
# NOTE: Fiyat adımı, miktar adımı ve diğer sınırlar elle yazılmaz. Binance
# bunları zaman zaman değiştirir, elle yazılmış bir liste canlının reddedeceği
# emri kâğıt hesapta kabul ederdi. Burak'ın 21.11 kararı.

from .errors import RuleError
from .models import SymbolSpec

# Masanın pariteleri, Binance'teki sembol ve paritenin ne olduğunu anlatan
# kısa ad. Ad boşsa arayüz sembolün başını kullanır.
SYMBOLS = {"BTCUSDT": "", "ETHUSDT": "", "SOLUSDT": "", "XRPUSDT": "", "XAUUSDT": "altın", "ZECUSDT": ""}

SLIPPAGE = 0.0002


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
            raise RuleError(f"{symbol} işlem yapılabilen pariteler arasında yok. "
                            f"Pariteler: {', '.join(self._specs)}.") from None

    @property
    def symbols(self) -> list[str]:
        return list(self._specs)

    def __contains__(self, symbol: str) -> bool:
        return symbol in self._specs
