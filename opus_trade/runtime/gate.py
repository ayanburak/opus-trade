"""
file: gate.py
importable: Gate, RestrictionSource, Restriction

Gate hesabı değiştiren her komutun geçtiği tek kapıdır. Komuttaki fiyatları
önce paritenin fiyat adımına oturtur. submit() komutu sırayla risk korumasına
ve yalnızca stratejinin komutlarında kesin kısıtlara göre denetler, geçerse
broker'a iletir, olan değişiklikleri olay yoluna yayar ve döndürür. Reddedilen komutta RuleError fırlatır, mesajı reddin nedenidir.

description: Bir hesabın komutlarını denetleyip broker'a ileten kapı.
"""

# NOTE: Kesin kısıtlar admin'in stratejiye koyduğu sınırlardır ve yalnızca
# Actor.STRATEGY komutlarına uygulanır. Admin kendi koyduğu kısıta takılmaz,
# sistemin komutları da takılmaz. Risk koruması ise kimden gelirse gelsin
# riski artıran her komuta uygulanır, canlı hesapta admin'in elle açtığı işlem
# de günlük zarar sınırını aşamaz.
#
# NOTE: Fiyatın adıma yuvarlanması kapıda yapılır, Burak'ın 21.11 kararı. Opus,
# panel ve kural stratejileri adımı bilmek zorunda kalmaz, her yanlış hane
# bir düzeltme çağrısına mal olmaz. Broker'ın kuralları ise Binance gibi
# katıdır ve adıma uymayan fiyatı reddeder, kâğıt ile canlı aynı davranır.
#
# NOTE: Kısıtların kaynağı admin müdahaleleridir ve çalıştırıcıyla birlikte
# gelir. Kapı onları yalnızca RestrictionSource soyut sınıfından okur.

from abc import ABC, abstractmethod
from dataclasses import dataclass, replace

from ..broker.base import Broker, ClosePreview, Preview
from ..domain import rules
from ..domain.catalog import Catalog
from ..domain.errors import RuleError
from ..domain.models import TpLevel
from ..domain.trading import (AccountEvent, Actor, AddToPosition, ClosePosition, Command, OpenPosition, PlaceOrder,
                              UpdateProtection)
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
        catalog: Fiyat adımlarının alındığı parite kuralları.
        guard: Hesabın risk koruması, yoksa None.
        restrictions: Admin'in kesin kısıtlarının kaynağı, yoksa None.
    """

    def __init__(self, broker: Broker, bus: EventBus, catalog: Catalog, guard: RiskGuard | None = None,
                 restrictions: RestrictionSource | None = None) -> None:
        self.broker = broker
        self.bus = bus
        self.catalog = catalog
        self.guard = guard
        self.source = restrictions

    # Sıfır SL korumayı kaldırmak demektir, yuvarlanmaz. Sembolü katalogda
    # olmayan komut olduğu gibi geçer, broker onu kendi mesajıyla reddeder.
    def _snap(self, cmd: Command) -> Command:
        symbol = cmd.ref.symbol if isinstance(cmd, UpdateProtection) else getattr(cmd, "symbol", None)
        if not isinstance(cmd, (OpenPosition, PlaceOrder, UpdateProtection)) or symbol not in self.catalog:
            return cmd
        spec = self.catalog.get(symbol)
        tick = lambda x: rules.to_tick(spec, x) if x else x
        changes = {"sl": tick(cmd.sl)}
        if cmd.tp_levels is not None:
            changes["tp_levels"] = tuple(TpLevel(tick(k.price), k.pct, k.qty) for k in cmd.tp_levels)
        if isinstance(cmd, PlaceOrder):
            changes["trigger"] = tick(cmd.trigger)
        return replace(cmd, **changes)

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

    def preview(self, cmd: OpenPosition | PlaceOrder | AddToPosition, actor: Actor, now: int) -> Preview:
        """Giriş ya da ekleme komutunu göndermeden kapının ve hesabın denetiminden geçirir, tahmini sonucunu döndürür.

        Raises:
            RuleError: Risk koruması, bir kesin kısıt ya da hesap kuralları komutu reddederse.
        """
        cmd = self._snap(cmd)
        self._check(cmd, actor, now)
        return self.broker.preview(cmd)

    def preview_close(self, cmd: ClosePosition, actor: Actor, now: int) -> ClosePreview:
        """Kapatma komutunu göndermeden kapının ve hesabın denetiminden geçirir, tahmini sonucunu döndürür.

        Raises:
            RuleError: Risk koruması, bir kesin kısıt ya da hesap kuralları komutu reddederse.
        """
        self._check(cmd, actor, now)
        return self.broker.preview_close(cmd)

    async def submit(self, cmd: Command, actor: Actor, now: int) -> list[AccountEvent]:
        """Komutu denetleyip uygular ve olan değişiklikleri döndürür.

        Raises:
            RuleError: Risk koruması, bir kesin kısıt ya da hesap kuralları komutu reddederse.
        """
        cmd = self._snap(cmd)
        self._check(cmd, actor, now)
        events = await self.broker.execute(cmd, actor)
        for e in events:
            self.bus.publish(e)
        return events
