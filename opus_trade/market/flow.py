"""
file: flow.py
importable: TradeFlow, WINDOW_S

TradeFlow tekil işlemlerden her parite için saniyelik bir özet tutar: işlem
sayısı, dolar hacmi, alıcının başlattığı dolar hacmi ve saniyenin son fiyatı.
add() bir işlemi ekler. Sorgular paritenin son işleminin saniyesine göre
geriye bakar ve içinde bulunulan yarım saniyeyi de sayar: count, usd ve
buy_usd bir pencerenin toplamını, move fiyatın o kadar saniye önceye göre
değişimini, mean fiyatın saniyelik ortalamasını verir. span paritenin ne
kadar saniyelik geçmişi olduğunu söyler, pencere bundan uzunsa sorgu
eksik veriyle cevap verir. forget() dinlenmeyen paritenin özetini siler.

description: Tekil işlemlerden parite başına saniyelik akış istatistiği.
"""

# NOTE: Her parite için WINDOW_S uzunluğunda halka diziler tutulur. Sayılar
# birikimli tutulur, yani bir saniyenin hanesinde o ana kadarki toplam durur.
# Bir pencerenin toplamı böylece iki hanenin farkıdır ve işlem başına sabit
# sürede hesaplanır. Saniyede yüzlerce işlem gelen bir iğnede her işlemde
# yüzlerce haneyi toplamak gecikmeyi büyütürdü.
#
# NOTE: İşlem gelmeyen saniyeler bir sonraki işlemde doldurulur: sayılar
# aynı kalır, fiyat bir önceki saniyenin fiyatıdır. Ortalama bu yüzden
# zamana göre ağırlıklıdır, sakin geçen dakikalar da sayılır.
#
# NOTE: Zaman Binance'in işlem damgasıdır, programın saati değil. Akış bir an
# gecikse de pencereler kayar ama bozulmaz.
#
# XXX: Bir parite WINDOW_S'den uzun süre işlem görmezse özeti sıfırlanır.
# Dinlenmeyen paritenin özeti forget() ile silinmeli. Silinmezse parite
# kısa bir aradan sonra yeniden dinlendiğinde aradaki saniyeler eski fiyatla
# doldurulur ve olmayan bir hareket görünür.

from .models import Trade

# Strateji fiyatın 20 dakikalık ortalamasına bakıyor, en uzun pencere bu.
WINDOW_S = 1200


# Bir paritenin halka dizileri. head en son işlemin saniyesi, first özetin
# başladığı saniyedir. Dizilerin uzunluğu pencereden bir fazladır, böylece
# tam WINDOW_S saniyelik bir pencerenin başlangıç hanesi henüz ezilmemiş olur.
class _Book:
    def __init__(self, sec: int, price: float, size: int) -> None:
        self.size = size
        self.head = sec
        self.first = sec
        self.count = [0] * size
        self.usd = [0.0] * size
        self.buy = [0.0] * size
        self.close = [price] * size
        self.close_sum = [0.0] * size
        self.close_sum[sec % size] = price

    def advance(self, sec: int) -> None:
        if sec - self.head >= self.size:
            self.__init__(sec, self.close[self.head % self.size], self.size)
            return
        h = self.head % self.size
        n, u, b, c, cs = self.count[h], self.usd[h], self.buy[h], self.close[h], self.close_sum[h]
        for s in range(self.head + 1, sec + 1):
            cs += c
            i = s % self.size
            self.count[i], self.usd[i], self.buy[i], self.close[i], self.close_sum[i] = n, u, b, c, cs
        self.head = sec


class TradeFlow:
    """Parite başına saniyelik akış özeti.

    Args:
        window_s: Geriye bakılabilecek en uzun süre, saniye.
    """

    def __init__(self, window_s: int = WINDOW_S) -> None:
        self.window = window_s
        self._books: dict[str, _Book] = {}

    # Saniyesi en son işlemden geride kalan işlem, akışta sıranın bozulduğu
    # nadir durumdur. O işlem son saniyeye yazılır, geçmiş hane değiştirilmez,
    # yoksa ondan sonraki bütün birikimli haneler düzeltilmek zorunda kalırdı.
    def add(self, trade: Trade) -> None:
        sec = trade.ms // 1000
        b = self._books.get(trade.symbol)
        if b is None:
            b = self._books[trade.symbol] = _Book(sec, trade.price, self.window + 1)
        elif sec > b.head:
            b.advance(sec)
        i = b.head % b.size
        b.count[i] += 1
        b.usd[i] += trade.usd
        if trade.buy:
            b.buy[i] += trade.usd
        b.close_sum[i] += trade.price - b.close[i]
        b.close[i] = trade.price

    def forget(self, keep: set[str]) -> None:
        for s in [s for s in self._books if s not in keep]:
            del self._books[s]

    def has(self, symbol: str) -> bool:
        return symbol in self._books

    # --- Sorgular ---

    # Pencerenin başlangıç ve bitiş saniyesi. Başlangıç hanesi pencereden
    # hemen önceki saniyedir, toplam iki birikimli hanenin farkıdır. Özetin
    # başından ya da halkanın tuttuğu en eski saniyeden eskiye uzanan pencere
    # orada kesilir.
    def _start(self, b: _Book, seconds: int, back: int) -> tuple[int, int]:
        end = b.head - back
        start = max(end - seconds, b.first - 1, b.head - b.size + 1)
        return start, end

    def _sum(self, arr_name: str, symbol: str, seconds: int, back: int) -> float:
        b = self._books[symbol]
        arr = getattr(b, arr_name)
        start, end = self._start(b, seconds, back)
        before = arr[start % b.size] if start >= b.first else 0
        return arr[end % b.size] - before

    def count(self, symbol: str, seconds: int, back: int = 0) -> int:
        """Son seconds saniyedeki işlem sayısını döndürür. back verilirse pencere o kadar saniye geriden biter."""
        return int(self._sum("count", symbol, seconds, back))

    def usd(self, symbol: str, seconds: int, back: int = 0) -> float:
        """Son seconds saniyedeki dolar hacmini döndürür."""
        return self._sum("usd", symbol, seconds, back)

    def buy_usd(self, symbol: str, seconds: int, back: int = 0) -> float:
        """Son seconds saniyede alıcının başlattığı işlemlerin dolar hacmini döndürür."""
        return self._sum("buy", symbol, seconds, back)

    def last(self, symbol: str) -> float:
        b = self._books[symbol]
        return b.close[b.head % b.size]

    def price_at(self, symbol: str, seconds_ago: int) -> float:
        """seconds_ago saniye önceki saniyenin son fiyatını döndürür. Özet daha kısaysa ilk fiyatı döndürür."""
        b = self._books[symbol]
        return b.close[max(b.head - seconds_ago, b.first) % b.size]

    def move(self, symbol: str, seconds: int) -> float:
        """Son fiyatın seconds saniye önceki fiyata göre oransal değişimini döndürür."""
        return self.last(symbol) / self.price_at(symbol, seconds) - 1

    def mean(self, symbol: str, seconds: int) -> float:
        """Son seconds saniyenin saniyelik son fiyatlarının ortalamasını döndürür."""
        b = self._books[symbol]
        start, end = self._start(b, seconds, 0)
        before = b.close_sum[start % b.size] if start >= b.first else 0.0
        return (b.close_sum[end % b.size] - before) / (end - start)

    def span(self, symbol: str) -> int:
        """Paritenin özetinin kaç saniyeyi kapsadığını döndürür, içinde bulunulan saniye dahil."""
        b = self._books[symbol]
        return b.head - b.first + 1

    def head_ms(self, symbol: str) -> int:
        return self._books[symbol].head * 1000
