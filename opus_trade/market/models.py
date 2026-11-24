"""
file: models.py
importable: Candle, Trade, Liquidation, Premium, Funding, OpenInterest, LongShortRatio, TakerVolume, DailyStats, OrderBook

Piyasa verisinin değişmez tipleri. Tekil işlem dışında bütün zamanlar unix
saniyedir, tekil işlemin zamanı milisaniyedir. Fiyatlar ve hacimler float. Binance'in dizi ve kısaltmalı alanlı cevapları bu tiplere
binance paketinde çevrilir, kodun geri kalanı Binance'in biçimini bilmez.

description: Mum, tekil işlem, likidasyon, fonlama, açık pozisyon ve oran verisinin tipleri.
"""

# NOTE: Mumun hacim alanları Binance'in verdiği dört değerdir: baz varlık
# cinsinden hacim, dolar cinsinden hacim, işlem sayısı ve agresif alıcıların
# baz cinsinden hacmi. Araştırma verisi ve toplayıcı hepsini saklar, göstergeler
# yalnızca volume'u kullanır.
#
# NOTE: slots=True mumlarda bellek içindir. Bir istem binlerce mum taşıyabilir.

from dataclasses import dataclass

from ..domain.models import Side


@dataclass(frozen=True, slots=True)
class Candle:
    symbol: str
    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float
    trades: int
    taker_buy_volume: float


# Binance'in aggTrade akışındaki bir işlem: aynı anda aynı fiyattan aynı
# alıcı tarafla gerçekleşen dolumların toplamı. buy işlemi başlatanın, yani
# piyasa emriyle gelen tarafın alıcı olduğunu söyler. Binance bunu ters
# anlamlı m alanıyla verir, alıcı defterdeki taraf ise m doğrudur. ms
# Binance'in işlem damgasıdır, programın saatinden bağımsızdır, gecikme
# ölçümü bu sayede yapılır.
@dataclass(frozen=True, slots=True)
class Trade:
    symbol: str
    ms: int
    price: float
    qty: float
    buy: bool

    @property
    def usd(self) -> float:
        return self.price * self.qty


# side likide olan pozisyonun yönüdür. Binance'in akışındaki SELL emri bir
# long'un, BUY emri bir short'un zorla kapatılmasıdır, çeviri binance
# paketinde yapılır. Binance bu akışta her parite için saniyede yalnızca en
# büyük likidasyonu gönderir, bu yüzden kayıtlar toplam tutarı değil
# dalgaların zamanını, yönünü ve büyüklük sırasını doğru gösterir.
@dataclass(frozen=True, slots=True)
class Liquidation:
    time: int
    symbol: str
    side: Side
    price: float
    qty: float

    @property
    def usd(self) -> float:
        return self.price * self.qty


# est_funding sıradaki fonlamada uygulanacak tahmini orandır. Binance bu
# değerin geçmişini vermez, yalnızca canlıyken okunabilir.
@dataclass(frozen=True, slots=True)
class Premium:
    time: int
    symbol: str
    mark: float
    index: float
    est_funding: float
    next_funding: int


@dataclass(frozen=True, slots=True)
class Funding:
    time: int
    symbol: str
    rate: float
    mark: float


# usd anlık açık pozisyon ucunda boştur, Binance yalnızca kontrat miktarını verir.
@dataclass(frozen=True, slots=True)
class OpenInterest:
    time: int
    symbol: str
    qty: float
    usd: float | None


@dataclass(frozen=True, slots=True)
class LongShortRatio:
    time: int
    symbol: str
    long_pct: float
    ratio: float


@dataclass(frozen=True, slots=True)
class TakerVolume:
    time: int
    symbol: str
    buy_sell_ratio: float
    buy_volume: float
    sell_volume: float


@dataclass(frozen=True, slots=True)
class DailyStats:
    symbol: str
    change_pct: float
    high: float
    low: float
    quote_volume: float


# Her taraf fiyat ve miktar çiftleridir. bids yüksekten düşüğe, asks düşükten
# yükseğe sıralıdır, yani ikisinde de en iyi fiyat baştadır.
@dataclass(frozen=True, slots=True)
class OrderBook:
    time: int
    symbol: str
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]
