"""
file: screen.py
importable: MarketScreen, RANKINGS

MarketScreen bütün piyasanın son yirmi dört saatlik özetini tutar ve ondan
sıralı izleme listeleri kurar. refresh() özeti Binance'ten yeniler, top() bir
sıralamaya göre ilk n pariteyi verir. Sıralamalar: range aralığın düşük
fiyata oranı, change mutlak değişim yüzdesi, both ikisinin ilk n'lerinin
birleşimi. Liste yalnızca verilen evrendeki paritelerden kurulur.
version her yenilemede artar, listeyi kullanan taraf değişikliği buradan
anlar.

description: Bütün piyasanın günlük özetinden izleme listeleri.
"""

# NOTE: Özet saat başı yenilenir ve saat boyunca sabit kalır. Strateji metni
# listenin saat başı kurulmasını istiyor, ayrıca liste her an değişseydi
# dinlenen akışlar da her an değişirdi. Yenilemeyi Supervisor zamanlar.
#
# NOTE: Aralık düşük fiyata bölünür, çünkü özet açılış fiyatını taşımıyor.
# Sıralama için yeterli, iki ölçü aynı paritelerin sırasını değiştirmez.

from collections.abc import Collection

from ..domain.clock import Clock
from .models import DailyStats
from .source import MarketData

RANKINGS = ("both", "range", "change")


def _range(d: DailyStats) -> float:
    return (d.high - d.low) / d.low if d.low > 0 else 0.0


class MarketScreen:
    """Bütün piyasanın günlük özeti ve ondan kurulan listeler.

    Args:
        market: Piyasa verisi.
        clock: Saat.
    """

    def __init__(self, market: MarketData, clock: Clock) -> None:
        self.market = market
        self.clock = clock
        self.stats: dict[str, DailyStats] = {}
        self.updated: int | None = None
        self.version = 0

    async def refresh(self) -> None:
        """Özeti Binance'ten yeniler.

        Raises:
            MarketDataError: Özet alınamazsa. Eski özet yerinde kalır.
        """
        found = await self.market.all_daily_stats()
        self.stats = {d.symbol: d for d in found}
        self.updated = self.clock.time()
        self.version += 1

    def top(self, n: int, by: str, universe: Collection[str]) -> list[str]:
        """Sıralamaya göre ilk n pariteyi büyükten küçüğe döndürür. both'ta liste n'den uzun olabilir.

        Args:
            n: Her sıralamadan alınacak parite sayısı.
            by: range, change ya da both.
            universe: Listeye girebilecek pariteler.
        """
        rows = [d for s, d in self.stats.items() if s in universe]
        by_range = [d.symbol for d in sorted(rows, key=_range, reverse=True)[:n]]
        by_change = [d.symbol for d in sorted(rows, key=lambda d: abs(d.change_pct), reverse=True)[:n]]
        if by == "range":
            return by_range
        if by == "change":
            return by_change
        return by_range + [s for s in by_change if s not in by_range]

    def describe(self, symbol: str) -> dict | None:
        """Paritenin özetini arayüz için döndürür, özette yoksa None."""
        d = self.stats.get(symbol)
        if d is None:
            return None
        return {"symbol": symbol, "range_pct": round(_range(d) * 100, 2), "change_pct": d.change_pct,
                "quote_volume": d.quote_volume}
