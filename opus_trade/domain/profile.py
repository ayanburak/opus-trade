"""
file: profile.py
importable: Area, State, ProfileConfig, Profile, Version

Bir profilin kaydı ve sürümünün ayarları. Profile adını, alanını, durumunu,
hesabını ve yürürlükteki sürümünü taşır. Version değişmez bir ayar
kopyasıdır. ProfileConfig bir sürümün ayarlarıdır: strateji türü ve
parametreleri, pariteler, başlangıç bakiyesi, tetikleyiciler ve risk
sınırları.

description: Profil, sürüm ve profil ayarlarının tipleri.
"""

# NOTE: Bakiye sürümün ayarıdır ama yalnızca hesap açılırken kullanılır. Çalışan
# bir profilde bakiyeyi değiştiren yeni sürüm hesabı değiştirmez, kâğıt hesap
# sıfırlanınca yeni hesap o bakiyeyle açılır.
#
# NOTE: triggers her biri type anahtarlı sözlüklerdir, gerisi tetikleyicinin
# parametreleridir. risk max_leverage, max_notional ve daily_loss anahtarlarını
# taşıyabilir, verilmeyen sınır yoktur.

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class Area(StrEnum):
    PAPER = "paper"
    LIVE = "live"


class State(StrEnum):
    DRAFT = "draft"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    ARCHIVED = "archived"


@dataclass(frozen=True)
class ProfileConfig:
    strategy: str
    params: dict[str, Any]
    symbols: tuple[str, ...]
    balance: float
    triggers: tuple[dict[str, Any], ...] = ()
    risk: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["symbols"], d["triggers"] = list(self.symbols), list(self.triggers)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ProfileConfig":
        return cls(strategy=d["strategy"], params=dict(d.get("params", {})), symbols=tuple(d["symbols"]),
                   balance=float(d["balance"]), triggers=tuple(d.get("triggers", ())), risk=dict(d.get("risk", {})))


@dataclass(frozen=True)
class Version:
    id: int
    profile_id: int
    number: int
    config: ProfileConfig
    note: str
    created_at: int


@dataclass(frozen=True)
class Profile:
    id: int
    name: str
    short: str
    area: Area
    state: State
    account_id: int | None
    version_id: int | None
    published: bool
    created_at: int
