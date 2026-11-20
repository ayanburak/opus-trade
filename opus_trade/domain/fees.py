"""
file: fees.py
importable: FeeSchedule, BINANCE_STANDARD

FeeSchedule bir hesabın maker ve taker komisyon oranlarıdır. rate() bir
işlemin maker olup olmamasına göre doğru oranı döndürür. BINANCE_STANDARD
Binance vadelinin en alt standart oranlarıdır, BNB indirimi yoktur.

description: Komisyon oranları.
"""

# NOTE: Fiyatın gelmesini bekleyen işlemler maker öder: lehine tetiklenen emir
# girişleri ve TP kademeleri. Anında gerçekleşen her şey taker öder: piyasa
# emri, kırılım emri, SL ve kapatmalar. Hangi işlemin hangisi olduğuna
# kuralları kullanan taraf karar verir, burası yalnızca oranı tutar.
#
# NOTE: Oranlar sabit değil bir nesnedir, çünkü canlı hesabın oranı Binance'teki
# VIP kademesine göre değişebilir. Kâğıt ve canlı broker kendi oranını alır.

from dataclasses import dataclass


@dataclass(frozen=True)
class FeeSchedule:
    taker: float
    maker: float

    def rate(self, maker: bool) -> float:
        return self.maker if maker else self.taker


BINANCE_STANDARD = FeeSchedule(taker=0.0005, maker=0.0002)
