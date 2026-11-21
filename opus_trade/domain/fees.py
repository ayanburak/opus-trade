"""
file: fees.py
importable: FeeSchedule, BINANCE_STANDARD

FeeSchedule bir hesabın maker ve taker komisyon oranlarıdır. rate() bir
işlemin maker olup olmamasına göre doğru oranı döndürür. BINANCE_STANDARD
Binance vadelinin en alt standart oranlarıdır, BNB indirimi yoktur.

description: Komisyon oranları.
"""

# NOTE: Defterde bekleyip fiyatın gelmesiyle dolan limit emir maker öder.
# Anında gerçekleşen her şey taker öder: piyasa emri, yanlış taraftaki limit,
# tetiklenince piyasa emri olan stop, take-profit ve iz, kapatmalar. Hangi
# işlemin hangisi olduğuna kuralları kullanan taraf karar verir, burası
# yalnızca oranı tutar.
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
