"""
file: invalidation.py
importable: Hit, InvalidationWatch

InvalidationWatch bir profilin girişlerinin geçersizlik seviyelerini izler.
on_price() bir paritenin fiyat aralığını alır ve seviyesine değilen notları
döndürür. Bekleyen bir girişin notuna değildiyse emir iptal edilecektir,
açık bir pozisyonun notuna değildiyse Opus uyandırılacaktır. İptali ve
uyandırmayı çalıştırıcı yapar, done() ile not işlenmiş sayılır.

description: Girişlerin geçersizlik fiyatlarının gözcüsü.
"""

# NOTE: Gözcü son işlem fiyatına bakar, mark fiyatına değil. Geçersizlik
# seviyesini Opus grafikten okur ve giriş emri de son fiyatla dolar,
# talimat/sema.md adım 5.
#
# NOTE: İptal broker'ın değil çalıştırıcının işidir. Çalıştırıcı kapıdan
# Actor.SYSTEM ile normal bir CancelOrder gönderir. Canlı broker geldiğinde
# aynı yol Binance'teki emri iptal eder, broker'ın bu kavramı bilmesi
# gerekmez.
#
# NOTE: Not, iptal başarıyla gönderildikten ya da uyandırma yazıldıktan sonra
# işlenmiş sayılır. Arada aynı not ikinci kez dönmesin diye sırada bekleyenler
# bellekte tutulur. İptal gönderilemezse, örneğin emir o arada dolduysa, not
# işlenmemiş kalır ve artık pozisyonun notu olarak izlenir.

from dataclasses import dataclass

from ..domain.models import Side
from ..domain.trading import AccountState, Order, Position
from ..storage.notes import EntryNote, NotesRepo


@dataclass(frozen=True)
class Hit:
    """Seviyesine değilen bir not. order doluysa iptal edilecek giriş, position doluysa uyarılacak pozisyondur."""

    note: EntryNote
    order: Order | None = None
    position: Position | None = None


class InvalidationWatch:
    """Bir profilin geçersizlik gözcüsü.

    Args:
        notes: Giriş notları deposu.
        profile_id: Profil.
    """

    def __init__(self, notes: NotesRepo, profile_id: int) -> None:
        self.notes = notes
        self.profile_id = profile_id
        self._queued: set[int] = set()

    def on_price(self, symbol: str, high: float, low: float, state: AccountState) -> list[Hit]:
        """Fiyat aralığının değdiği, henüz işlenmemiş notları döndürür.

        Args:
            symbol: Parite.
            high: Aralığın en yüksek son fiyatı.
            low: Aralığın en düşük son fiyatı.
            state: Hesabın hali, bekleyen girişler ve açık pozisyonlar buradan okunur.
        """
        orders = {o.id: o for o in state.orders if not o.reduce and o.symbol == symbol}
        positions = {p.id: p for p in state.positions if p.symbol == symbol}
        out = []
        for n in self.notes.pending_invalidations(self.profile_id, list(orders), list(positions)):
            if n.id in self._queued:
                continue
            touched = high >= n.invalid_price if n.side == Side.SHORT else low <= n.invalid_price
            if not touched:
                continue
            self._queued.add(n.id)
            if n.position_id is not None:
                out.append(Hit(n, position=positions.get(n.position_id)))
            else:
                out.append(Hit(n, order=orders[n.order_id]))
        return out

    def done(self, hit: Hit, now: int) -> None:
        self.notes.mark_hit(hit.note.id, now, "cancelled" if hit.order else "woke")
        self._queued.discard(hit.note.id)

    def failed(self, hit: Hit) -> None:
        self._queued.discard(hit.note.id)
