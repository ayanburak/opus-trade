"""
file: price_alert.py
importable: Alert, PriceAlerts

PriceAlerts bir profilin fiyat alarmlarıdır. add() bir alarm kurar,
cancel() kaldırır, on_candle() fiyat bir alarma değince onu Fire olarak
döndürür ve listeden çıkarır. Alarm işlem açmaz, yalnızca stratejiyi
uyandırır ve kurulurken verilen notu taşır.

description: Fiyat alarmı tetikleyicisi.
"""

# NOTE: Alarm hesabın bir kaydı değildir ve Binance'te karşılığı yoktur, bu
# yüzden broker'da değil tetikleyicilerdedir. actor alarmı kimin kurduğunu
# tutar, stratejiye giden metin yönetici kurduysa bunu söyler.
#
# NOTE: Alarmların numarası pozisyon ve emirlerden ayrıdır. Kalıcı olarak
# saklanması çalıştırıcının işidir, bu sınıf yalnızca bellekteki listeyi
# yönetir ve load() ile geri yüklenir.

from dataclasses import dataclass

from ..market.models import Candle
from .base import Fire, Trigger


@dataclass(frozen=True)
class Alert:
    number: int
    symbol: str
    price: float
    below: bool
    note: str
    actor: str
    created_at: int


class PriceAlerts(Trigger):
    kind = "alert"

    def __init__(self) -> None:
        self._alerts: dict[int, Alert] = {}

    def load(self, alerts: list[Alert]) -> None:
        self._alerts = {a.number: a for a in alerts}

    @property
    def alerts(self) -> list[Alert]:
        return sorted(self._alerts.values(), key=lambda a: a.number)

    def add(self, symbol: str, price: float, current: float, note: str, actor: str, now: int) -> Alert:
        """Bir alarm kurar. Fiyat güncel fiyatın altındaysa düşüşte, üstündeyse yükselişte tetiklenir.

        Raises:
            ValueError: Seviye sıfır ya da negatifse veya güncel fiyata çok yakınsa.
        """
        if price <= 0 or abs(price - current) / current < 1e-5:
            raise ValueError("Alarm seviyesi güncel fiyattan farklı ve pozitif olmalı.")
        n = 1
        while n in self._alerts:
            n += 1
        a = Alert(n, symbol, price, price < current, note, actor, now)
        self._alerts[n] = a
        return a

    def cancel(self, number: int) -> Alert:
        """Bir alarmı kaldırır ve kaldırılan alarmı döndürür.

        Raises:
            ValueError: Bu numarada alarm yoksa.
        """
        if number not in self._alerts:
            raise ValueError(f"#{number} numaralı alarm yok.")
        return self._alerts.pop(number)

    def on_candle(self, candle: Candle) -> list[Fire]:
        out = []
        for a in [a for a in self.alerts if a.symbol == candle.symbol]:
            if (candle.low <= a.price) if a.below else (candle.high >= a.price):
                del self._alerts[a.number]
                out.append(Fire(self.kind, candle.time, a.symbol, a))
        return out
