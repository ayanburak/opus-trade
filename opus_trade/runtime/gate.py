"""
file: gate.py
importable: Gate, RestrictionSource, Restriction

Gate hesabı değiştiren her komutun geçtiği tek kapıdır. submit() komutu
sırayla risk korumasına ve yalnızca stratejinin komutlarında kesin kısıtlara
göre denetler, geçerse broker'a iletir, olan değişiklikleri olay yoluna yayar
ve döndürür. Reddedilen komutta RuleError fırlatır, mesajı reddin nedenidir.

description: Bir hesabın komutlarını denetleyip broker'a ileten kapı.
"""

# NOTE: Kesin kısıtlar admin'in stratejiye koyduğu sınırlardır ve yalnızca
# Actor.STRATEGY komutlarına uygulanır. Admin kendi koyduğu kısıta takılmaz,
# sistemin komutları da takılmaz. Risk koruması ise kimden gelirse gelsin
# riski artıran her komuta uygulanır, canlı hesapta admin'in elle açtığı işlem
# de günlük zarar sınırını aşamaz.
#
# NOTE: Kısıtların kaynağı admin müdahaleleridir ve çalıştırıcıyla birlikte
# gelir. Kapı onları yalnızca RestrictionSource soyut sınıfından okur.

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..broker.base import Broker
from ..domain.errors import RuleError
from ..domain.trading import AccountEvent, Actor, Command
from .bus import EventBus
from .limits import Limit, breach
from .risk import RiskGuard


@dataclass(frozen=True)
class Restriction:
    id: int
    limit: Limit


class RestrictionSource(ABC):
    @abstractmethod
    def restrictions(self, now: int) -> list[Restriction]:
        """Verilen anda yürürlükte olan kesin kısıtları döndürür."""


class Gate:
    """Bir hesabın kapısı.

    Args:
        broker: Komutların uygulandığı hesap.
        bus: Değişikliklerin yayıldığı olay yolu.
        guard: Hesabın risk koruması, yoksa None.
        restrictions: Admin'in kesin kısıtlarının kaynağı, yoksa None.
    """

    def __init__(self, broker: Broker, bus: EventBus, guard: RiskGuard | None = None,
                 restrictions: RestrictionSource | None = None) -> None:
        self.broker = broker
        self.bus = bus
        self.guard = guard
        self.source = restrictions

    def _check(self, cmd: Command, actor: Actor, now: int) -> None:
        state = self.broker.state()
        if self.guard and (reason := self.guard.check(cmd, state)):
            raise RuleError(f"Reddedildi, {reason}.")
        if actor is Actor.STRATEGY and self.source:
            found = self.source.restrictions(now)
            hit = breach(cmd, state, [r.limit for r in found])
            if hit:
                rid = next(r.id for r in found if r.limit is hit)
                raise RuleError(f"Reddedildi, yöneticinin #{rid} numaralı kesin kısıtı geçerli: {hit.text()}.")

    async def submit(self, cmd: Command, actor: Actor, now: int) -> list[AccountEvent]:
        """Komutu denetleyip uygular ve olan değişiklikleri döndürür.

        Raises:
            RuleError: Risk koruması, bir kesin kısıt ya da hesap kuralları komutu reddederse.
        """
        self._check(cmd, actor, now)
        events = await self.broker.execute(cmd, actor)
        for e in events:
            self.bus.publish(e)
        return events
