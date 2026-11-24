"""
file: policy.py
importable: TeamPolicy

Ekip kotası. TeamPolicy rol başına bir çağrıda başlatılabilecek görev
sayısını ve günlük üst sınırı taşır. remaining() bugün kullanılanlara
göre bu çağrıda kalan hakkı verir: çağrı kotası ile günlük sınırdan kalanın
küçüğü.

    policy = TeamPolicy({"haiku": 10}, {"haiku": 100})
    policy.remaining({"haiku": 95})  # {"opus": 0, "sonnet": 0, "haiku": 5}

description: Alt görevlerin çağrı ve gün başına kotası.
"""

# NOTE: Kota bir profil parametresidir, panelden değişir. Günlük sınır
# aboneliği korumak içindir, bütün LLM profilleri aynı abonelikten düşer.

from dataclasses import dataclass, field

from .roles import ROLE_KEYS


@dataclass(frozen=True)
class TeamPolicy:
    """Rol başına kota. Yazılmayan rolün kotası sıfırdır.

    Args:
        per_call: Rolden bir çağrıdaki en fazla görev sayısına.
        daily: Rolden bir gündeki en fazla görev sayısına.
    """

    per_call: dict[str, int] = field(default_factory=dict)
    daily: dict[str, int] = field(default_factory=dict)

    def remaining(self, used: dict[str, int]) -> dict[str, int]:
        """Bu çağrıda kalan hakkı rol başına döndürür.

        Args:
            used: Rolden bugün kullanılan görev sayısına.
        """
        return {r: max(0, min(self.per_call.get(r, 0), self.daily.get(r, 0) - used.get(r, 0))) for r in ROLE_KEYS}
