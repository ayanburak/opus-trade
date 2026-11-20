"""
file: risk.py
importable: RiskGuard, DAY_TZ

RiskGuard bir hesabın kalıcı risk sınırlarıdır: sabit sınırlar, günlük zarar
sınırı ve acil durdurma. check() riski artıran bir komutu kabul etmezse
nedenini Türkçe döndürür. day_pnl() günün kâr ve zararını, breached() günlük
zarar sınırının aşılıp aşılmadığını söyler. Kâğıt profilde isteğe bağlıdır,
canlı profilde zorunludur.

description: Hesabın günlük zarar sınırı, acil durdurması ve sabit risk sınırları.
"""

# NOTE: Günün kâr ve zararı, gün başından beri kapanan işlemlerin net sonucu ile
# açık pozisyonların o anki kâr ve zararının toplamıdır. Gün Türkiye saatiyle
# gece yarısı başlar. Sınır aşılınca riski artıran komutlar reddedilir, profili
# duraklatmak breached()'e bakan çalıştırıcının işidir.
#
# NOTE: Acil durdurma yalnızca yeni risk alınmasını engeller. Açık pozisyonları
# kapatmak acil durdurmayı tetikleyen tarafın ayrı bir CloseAll komutuyla yaptığı
# iştir.

from datetime import datetime, time

from ..domain.clock import HOME_TZ, Clock
from ..domain.trading import AccountState, Command, RISKY
from ..storage.ledger import Ledger
from .limits import Limit, breach

DAY_TZ = HOME_TZ


class RiskGuard:
    """Bir hesabın risk sınırları.

    Args:
        ledger: Kapanan işlemlerin okunduğu defter.
        clock: Günün başlangıcı için saat.
        limits: Sabit sınırlar, örneğin en yüksek kaldıraç.
        daily_loss: Günlük en büyük zarar, dolar. None ise sınır yok.
    """

    def __init__(self, ledger: Ledger, clock: Clock, limits: list[Limit] | None = None,
                 daily_loss: float | None = None) -> None:
        self.ledger = ledger
        self.clock = clock
        self.limits = list(limits or [])
        self.daily_loss = daily_loss
        self.halted = False

    def _day_start(self) -> int:
        now = self.clock.now().astimezone(DAY_TZ)
        return int(datetime.combine(now.date(), time(0), DAY_TZ).timestamp())

    # Açık pozisyonların kâr ve zararı özkaynaktan nakdi ve kilitli marjini
    # düşünce kalan kısımdır, fiyatları broker zaten uygulamıştır.
    def day_pnl(self, state: AccountState) -> float:
        realized = sum(t.net for t in self.ledger.trades(state.account_id, since=self._day_start()))
        return realized + state.equity - state.cash - sum(p.margin for p in state.positions)

    def breached(self, state: AccountState) -> bool:
        return self.daily_loss is not None and self.day_pnl(state) <= -self.daily_loss

    def check(self, cmd: Command, state: AccountState) -> str | None:
        """Riski artıran komut reddedilecekse nedenini, kabul edilecekse None döndürür."""
        if not isinstance(cmd, RISKY):
            return None
        if self.halted:
            return "acil durdurma açık, yeni risk alınmaz"
        if self.breached(state):
            return f"günlük zarar sınırı olan {self.daily_loss:g} dolara ulaşıldı, bugün yeni risk alınmaz"
        hit = breach(cmd, state, self.limits)
        return f"risk sınırı: {hit.text()}" if hit else None
