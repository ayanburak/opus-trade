"""
file: facts.py
importable: LATE_MIN, build, origins

build() bir profilin hesap gerçeklerini defterden, günlükten ve not
deposundan hesaplar ve stratejiye Context.facts olarak gider: günün
işlemleri, art arda kayıp, son kapanan pozisyonlar, dolma oranı, parite ve
yön başına son stop, açık pozisyonların ve emirlerin kaynağı ve giriş
notları, sınıflandırılmayı bekleyen pozisyonlar, kör kalınan süre ve
yöneticinin son yirmi dört saatteki işlemleri ve bugün çalışan alt görevler.
Hiçbir şey yazmaz.

description: Stratejiye giden hazır hesap gerçeklerinin hesabı.
"""

# NOTE: Pozisyonun kaynağı onu açan OPENED olayının actor'üdür. Bekleyen bir
# emir dolarak açtıysa olayın actor'ü sistemdir, kaynak o emri veren ORDER_PLACED
# olayının actor'üdür. Yöneticinin kapattığı pozisyon son parçasının nedeni
# admin olandır. İkisi de sınıflandırılmaz, Opus'un tezinin sonucu değildir.
#
# NOTE: Stop sayılan kapanış zararla biten SL ve likidasyondur. İz süren stop
# da SL nedeniyle kapanır, kârdaysa stop sayılmaz. Opus'un son stoptan
# farkını yazma zorunluluğu tezin yanlış çıktığı durum içindir.
#
# NOTE: Sınıflandırılmayı bekleyenler yürürlükteki sürüm başladıktan sonra
# kapananlardır. Eski sürümün pozisyonları yeni şemayla hiç tanışmadı,
# geriye dönük sınıflandırma istenmez.

from datetime import datetime, time
from typing import Callable

from ..domain.trading import AccountEvent, AccountState, Actor, EventKind
from ..storage.journal import JournalRepo
from ..storage.ledger import Ledger
from ..storage.notes import EntryNote, NotesRepo
from ..strategy.base import ClosedPosition, Facts, Thesis
from ..strategy.llm.texts import clock
from .risk import DAY_TZ
from .wakes import event_from_dict

# Son kararın istediği uyanma anından bu kadar dakika sonra uyanan Opus'a kör
# kaldığı süre söylenir, talimat/istem.md. Birkaç dakikalık gecikme kararın
# kendi süresinden ya da sırada beklemekten gelir, söylenmeye değmez.
LATE_MIN = 15

# Kartta gösterilen son kapanan pozisyon sayısı.
_RECENT = 5

# Dolma oranına giren son bekleyen giriş emri sayısı.
_FILL_WINDOW = 10

_STOP_REASONS = ("sl", "liquidation", "funding_liquidation")

_DAY_S = 86400


def _thesis(n: EntryNote) -> Thesis:
    return Thesis(n.symbol, n.side, n.reason, n.invalid_price, n.invalid_text, n.created_at)


def _day_start(now: int) -> int:
    d = datetime.fromtimestamp(now, DAY_TZ).date()
    return int(datetime.combine(d, time(0), DAY_TZ).timestamp())


# Olaylardan emir ve pozisyon başına kaynak çıkarılır. Emir kaynağı onu
# kuran olaydır. Pozisyonun açılışı sistemin olayıysa açılıştaki dolan emre
# bakılır.
def origins(events: list[dict]) -> tuple[dict[int, str], dict[int, str]]:
    """Hesap olaylarından pozisyon ve emir kimliği başına kaynağı, strategy, admin ya da system döndürür."""
    orders: dict[int, str] = {}
    filled_by: dict[int, int] = {}
    positions: dict[int, str] = {}
    for e in events:
        kind, actor = e["kind"], e["actor"]
        if kind == EventKind.ORDER_PLACED and e["order_id"] is not None:
            orders.setdefault(e["order_id"], actor)
        elif kind == EventKind.ORDER_FILLED and e["position_id"] is not None and e["order_id"] is not None:
            filled_by.setdefault(e["position_id"], e["order_id"])
        elif kind == EventKind.OPENED and e["position_id"] is not None:
            if actor == Actor.SYSTEM:
                actor = orders.get(filled_by.get(e["position_id"]), Actor.STRATEGY)
            positions.setdefault(e["position_id"], actor)
    return {k: str(v) for k, v in positions.items()}, {k: str(v) for k, v in orders.items()}


def _closed(ledger: Ledger, account_id: int, origins: dict[int, str]) -> list[ClosedPosition]:
    out = []
    for pid, parts in ledger.closed_positions(account_id):
        last = parts[-1]
        net = sum(t.net for t in parts)
        out.append(ClosedPosition(pid, last.symbol, str(last.side), net, last.reason, last.exit_time,
                                  origins.get(pid, str(Actor.STRATEGY)),
                                  stopped=last.reason in _STOP_REASONS and net < 0))
    return out


def _streak(closed: list[ClosedPosition]) -> int:
    n = 0
    for c in reversed(closed):
        if c.net >= 0:
            break
        n += 1
    return n


def build(ledger: Ledger, journal: JournalRepo, notes: NotesRepo, profile_id: int, state: AccountState,
          version_start: int, now: int, describe: Callable[[list[AccountEvent]], str] = lambda e: str(e[0].kind)) -> Facts:
    """Bir profilin hesap gerçeklerini hesaplar.

    Args:
        ledger: Hesap defteri.
        journal: Profilin günlüğü, son kararın anı ve istediği uyanma için.
        notes: Giriş notları ve sınıflandırmalar.
        profile_id: Profil.
        state: Hesabın şimdiki hali, açık pozisyonlar ve bekleyen emirler buradan okunur.
        version_start: Yürürlükteki sürümün başlangıç anı.
        now: Şu an.
        describe: Bir olayı Türkçe cümleye çeviren fonksiyon, yöneticinin işlemleri için.
    """
    account_id = state.account_id
    events = ledger.events(account_id)
    position_origins, order_origins = origins(events)
    closed = _closed(ledger, account_id, position_origins)

    day = _day_start(now)
    today_parts = [t for t in ledger.trades(account_id, day)]
    today = [c for c in closed if c.closed_at >= day]

    last_stop: dict[str, bool] = {}
    for c in closed:
        last_stop[f"{c.symbol} {c.side}"] = c.stopped

    # Dolma oranına yalnızca stratejinin verdiği emirler girer. Yöneticinin
    # emri Opus'un isabetini ölçmez.
    resolved = [o for o in ledger.resolved_entries(account_id, 200)
                if order_origins.get(o["id"], str(Actor.STRATEGY)) == Actor.STRATEGY][:_FILL_WINDOW]
    fill_rate = (sum(1 for o in resolved if o["status"] == "filled"), len(resolved))

    reviewed = notes.reviewed_ids(profile_id)
    unreviewed = tuple(c for c in closed if c.position_id not in reviewed and c.closed_at >= version_start
                       and c.origin == Actor.STRATEGY and c.reason != Actor.ADMIN)

    open_positions = [p.id for p in state.positions]
    open_orders = [o.id for o in state.orders if not o.reduce]
    position_notes = {pid: tuple(_thesis(n) for n in found)
                      for pid, found in notes.for_positions(open_positions).items()}
    order_notes = {oid: _thesis(n) for oid in open_orders if (n := notes.for_order(oid)) is not None}

    last = journal.last_success(profile_id)
    last_call = last[0] if last else None
    late = None
    if last and last[1] is not None and now - last[1] >= LATE_MIN * 60:
        late = (now - last[1]) // 60
    invalidated = tuple((_thesis(n), t) for n, t in notes.cancelled_since(profile_id, last_call or 0))

    admin = tuple(f"{clock(e['time'])}: {describe([event_from_dict(e['detail'])])}" for e in events
                  if e["actor"] == Actor.ADMIN and e["time"] >= now - _DAY_S)

    return Facts(today_count=len(today), today_stops=sum(1 for c in today if c.stopped),
                 today_net=sum(t.net for t in today_parts), loss_streak=_streak(closed),
                 recent=tuple(closed[-_RECENT:]), fill_rate=fill_rate, last_stop=last_stop,
                 position_origins={k: v for k, v in position_origins.items() if k in open_positions},
                 order_origins={k: v for k, v in order_origins.items() if k in open_orders},
                 unreviewed=unreviewed, position_notes=position_notes, order_notes=order_notes,
                 invalidated=invalidated, last_call=last_call, late_min=late, admin_24h=admin,
                 version_start=version_start, team_today=journal.team_counts(profile_id, day))
