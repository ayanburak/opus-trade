"""
file: checks.py
importable: Review, fail, cancelled_by, worst_stop, entry_price

Karar adımlarının ortak denetim parçaları, talimat/sema.md. Review bir
kararın denetim sonucudur: kontrol hataları, bozuldu denen emirlerin iptali,
ret olmayan notlar ve adımların kendi sonuçları. fail() bir ret metnini
kurar. cancelled_by() bu kararda iptal edilen bekleyen girişleri verir.
worst_stop() bir pozisyonun en kötü stopunu, entry_price() bir girişin
fiyatını verir, giriş denetimleri bunları paylaşır.
Denetimlerin kendisi steps paketindeki adım modüllerindedir.

description: Karar denetiminin sonucu ve ret metinlerinin ortak biçimi.
"""

# NOTE: Biçimi şema zorlar, adımlarda yalnızca şemanın ifade edemediği
# denetimler var: Opus'un yazdığı rakamlarla seçtiği yönün çelişmesi, hesapla
# listenin tutması, gerçek riskin hesabı. Her ret metni hangi kontrol olduğunu
# ve rakamlarla nedenini söyler. Kalan düzeltme hakkını istem ekler.
#
# NOTE: Opus'un beyanı Opus'u bağlar, talimat/ilkeler.md. Bir kontrol Opus'un
# yerine karar vermez, yalnızca kendi yazdığıyla çelişen eylemi reddeder.
# Yeni bir kontrol eklenmeden önce bunun bir düşünme adımı mı yoksa Opus'un
# yerine verilen bir karar mı olduğu tartılır.
#
# XXX: Ret metinleri Opus'a gider. Düşünce süreci gibi ifadeler kullanılmaz,
# Opus çağrıyı reddediyor.

from dataclasses import dataclass, field
from typing import Any

from ...domain import rules
from ...domain.models import Side
from ...domain.trading import OrderType, PlaceOrder, Ref
from ..base import Context


@dataclass
class Review:
    """Bir kararın denetim sonucu.

    Args:
        errors: Kontrol hataları, biri bile varsa karar reddedilir.
        cancels: Bozuldu denen bekleyen girişler.
        notes: Ret olmayan açıklamalar, örneğin geçerli denen planın taşındığı.
        parts: Adımın anahtarından o adımın kendi sonucuna, örneğin yapı adımının rejim okuması.
    """

    errors: list[str] = field(default_factory=list)
    cancels: list[Ref] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    parts: dict[str, Any] = field(default_factory=dict)


def fail(step: str, why: str) -> str:
    """Bir ret metni kurar. step adımın numarası ve alanıdır, örneğin adım 1, emirler."""
    return f"Kontrol: {step}. Neden: {why}"


def cancelled_by(k: dict, review: Review) -> set[tuple[str, str]]:
    """Bu kararda iptal edilen bekleyen girişlerin parite ve yönleri: bozuldu denenler ve emir_iptal eylemleri."""
    out = {(r.symbol, str(r.side)) for r in review.cancels}
    out |= {(a["sembol"], a["yon"]) for a in k["eylemler"] if a.get("tip") == "emir_iptal"}
    return out


# İz süren stopun seviyesi gördüğü en iyi fiyattan hesaplanır.
def worst_stop(ctx: Context, symbol: str, side: Side) -> float | None:
    """Açık pozisyonun stoplarından en kötüsünü döndürür, stop yoksa None."""
    levels = []
    for o in ctx.state.orders:
        if not (o.reduce and o.symbol == symbol and o.side is side):
            continue
        if o.type is OrderType.STOP:
            levels.append(o.price)
        elif o.type is OrderType.TRAILING_STOP and o.best:
            levels.append(rules.trail_level(side, o.best, o.trail_pct))
    if not levels:
        return None
    return min(levels) if side is Side.LONG else max(levels)


def entry_price(cmd: PlaceOrder, ctx: Context) -> float:
    """Girişin fiyatını döndürür: emrin fiyatı, piyasa girişinde son fiyat."""
    return cmd.price if cmd.price is not None else ctx.prices[cmd.symbol]
