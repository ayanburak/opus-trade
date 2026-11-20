"""
file: catalog.py
importable: Catalog, DEFAULT_SPECS

Catalog işlem yapılabilen paritelerin kurallarını sembolle bulur. get()
katalogda olmayan bir sembolde RuleError fırlatır, mesajı işlem yapılabilen
paritelerin listesini verir. DEFAULT_SPECS bugünkü parite listesidir.

description: İşlem yapılabilen paritelerin kural kataloğu.
"""

# NOTE: Kayma bütün paritelerde aynı, yüzde 0,02'dir. Bu hesabın işlem
# büyüklüklerinde gerçek kayma her paritede en fazla bir fiyat adımı kadardır.
# Paritelere farklı kayma vermek BTC'ye gerçekte olmayan bir maliyet avantajı
# sağlıyordu.
#
# TODO: Canlı broker için en küçük büyüklük ve miktar adımı Binance'in
# exchangeInfo ucundan okunmalı. Binance bu değerleri zaman zaman değiştiriyor,
# elle yazılmış bir liste canlıda emrin reddedilmesine yol açar.

from .errors import RuleError
from .models import SymbolSpec

DEFAULT_SPECS = (
    SymbolSpec("BTCUSDT", "BTC/USDT", 100, 0.004, 0.0002, 50, 0.001),
    SymbolSpec("ETHUSDT", "ETH/USDT", 100, 0.004, 0.0002, 20, 0.001),
    SymbolSpec("SOLUSDT", "SOL/USDT", 50, 0.01, 0.0002, 5, 0.01),
    SymbolSpec("XRPUSDT", "XRP/USDT", 50, 0.01, 0.0002, 5, 0.1),
    SymbolSpec("XAUUSDT", "XAU/USDT", 50, 0.01, 0.0002, 5, 0.001, note="altın"),
    SymbolSpec("ZECUSDT", "ZEC/USDT", 50, 0.01, 0.0002, 5, 0.001),
)


class Catalog:
    """Sembolden parite kuralına erişim. Sıra verilen sıradır."""

    def __init__(self, specs: tuple[SymbolSpec, ...] = DEFAULT_SPECS) -> None:
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
