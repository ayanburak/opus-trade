"""
file: clock.py
importable: Clock, SystemClock, HOME_TZ

Clock zamanın tek kaynağıdır. Saate ihtiyaç duyan her parça onu yapıcısından
alır ve time modülünü doğrudan çağırmaz. now() saat dilimi UTC olan bir an,
time() unix saniye, ms() unix milisaniye döndürür. SystemClock gerçek saattir.

description: Programın ve testlerin aynı arayüzle kullandığı saat.
"""

# NOTE: Saat soyut bir sınıftır çünkü testler ve ileride geçmiş verinin
# yeniden oynatılması gerçek saati değil kendi verdikleri anı kullanmak
# zorunda. Saati doğrudan time.time() ile okuyan bir parça, örneğin gece
# yarısına denk gelen bir testte, sonucu çalıştığı ana göre değiştirir.

import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# Kullanıcıya ve Opus'a gösterilen saatlerin ve günlük zarar sınırının
# gününün dilimi. Sunucunun saati UTC'dir, bu dilim ondan bağımsızdır.
HOME_TZ = ZoneInfo("Europe/Istanbul")


class Clock(ABC):
    @abstractmethod
    def now(self) -> datetime:
        """Şu anı saat dilimi UTC olan bir datetime olarak döndürür."""

    def time(self) -> int:
        return int(self.now().timestamp())

    def ms(self) -> int:
        return int(self.now().timestamp() * 1000)


class SystemClock(Clock):
    def now(self) -> datetime:
        return datetime.fromtimestamp(time.time(), timezone.utc)
