"""
file: profiles.py
importable: ProfileError, ProfileService

ProfileService profillerin yaşam döngüsünü yönetir. create() bir profili
kâğıt hesabıyla ve ilk sürümüyle kurar. update_config() yeni bir sürüm
ekler. start(), pause(), stop() ve archive() durumunu değiştirir. clone()
bir profilin ayarlarını ve hafızasını yeni bir kâğıt profile kopyalar.
reset_account() kâğıt hesabı kapatıp yeni bir hesap açar. publish()
herkese açık sayfadaki profili seçer. Kurala aykırı her istek Türkçe
mesajlı ProfileError fırlatır. Bir profil değişince kurulurken verilen
on_change çağrılır, çalıştırıcıları yöneten taraf onu dinler.

description: Profillerin oluşturulması, sürümlenmesi ve durum geçişleri.
"""

# NOTE: Her ayar strateji ve tetikleyici kayıt defterlerindeki şemayla
# doğrulanır ve varsayılanlarla doldurulmuş haliyle sürüme yazılır. Böylece bir
# sürümün ayarı, sonradan kodda bir varsayılan değişse de, o gün geçerli olan
# değerleri taşır.
#
# NOTE: Durum geçişleri: draft, paused ve stopped başlatılabilir. running
# duraklatılabilir. running ve paused durdurulabilir. draft ve stopped
# arşivlenebilir. Arşivlenen profil salt okunurdur ve yayından kalkar.
# Durdururken açık pozisyonları kapatmak bu servisin değil, kapıya
# CloseAll gönderen çağıranın işidir.
#
# NOTE: Canlı profil ve canlıya alma canlı broker'la birlikte gelir. Bugün
# yalnızca kâğıt profil kurulabilir.

from typing import Callable

from ..domain.catalog import Catalog
from ..domain.clock import Clock
from ..domain.profile import Area, Profile, ProfileConfig, State, Version
from ..registry import validate
from ..storage.journal import JournalRepo
from ..storage.ledger import Ledger
from ..storage.profiles import ProfileRepo
from ..strategy.base import STRATEGIES
from ..triggers.base import TRIGGERS

_RISK_KEYS = ("max_leverage", "max_notional", "daily_loss")

_MOVES = {
    "start": ({State.DRAFT, State.PAUSED, State.STOPPED}, State.RUNNING, "başlatılamaz"),
    "pause": ({State.RUNNING}, State.PAUSED, "duraklatılamaz"),
    "stop": ({State.RUNNING, State.PAUSED}, State.STOPPED, "durdurulamaz"),
    "archive": ({State.DRAFT, State.STOPPED}, State.ARCHIVED, "arşivlenemez"),
}

_STATE_NAMES = {State.DRAFT: "taslak", State.RUNNING: "çalışıyor", State.PAUSED: "duraklatılmış",
                State.STOPPED: "durdurulmuş", State.ARCHIVED: "arşivlenmiş"}


class ProfileError(ValueError):
    pass


class ProfileService:
    """Profillerin yaşam döngüsü.

    Args:
        repo: Profil deposu.
        ledger: Hesap defteri.
        journal: Strateji hafızasının deposu, kopyalama ve sıfırlama için.
        catalog: İşlem yapılabilen pariteler.
        clock: Saat.
        on_change: Bir profil değişince profilin kimliğiyle çağrılır.
    """

    def __init__(self, repo: ProfileRepo, ledger: Ledger, journal: JournalRepo, catalog: Catalog, clock: Clock,
                 on_change: Callable[[int], None] = lambda pid: None) -> None:
        self.repo = repo
        self.ledger = ledger
        self.journal = journal
        self.catalog = catalog
        self.clock = clock
        self.on_change = on_change

    def validate(self, config: ProfileConfig) -> ProfileConfig:
        """Ayarı doğrular ve varsayılanlarla doldurulmuş halini döndürür.

        Raises:
            ProfileError: Strateji türü, bir parametre, parite, bakiye, tetikleyici ya da risk sınırı geçersizse.
        """
        try:
            params = validate(STRATEGIES.get(config.strategy).params, config.params)
            triggers = []
            for t in config.triggers:
                spec = dict(t)
                kind = spec.pop("type", None)
                triggers.append({"type": kind, **validate(TRIGGERS.get(kind).params, spec)})
        except ValueError as e:
            raise ProfileError(str(e)) from None
        if not config.symbols:
            raise ProfileError("En az bir parite seçilmeli.")
        unknown = [s for s in config.symbols if s not in self.catalog]
        if unknown:
            raise ProfileError(f"İşlem yapılabilen pariteler arasında olmayan: {', '.join(unknown)}.")
        if config.balance <= 0:
            raise ProfileError("Başlangıç bakiyesi sıfırdan büyük olmalı.")
        bad = [k for k, v in config.risk.items() if k not in _RISK_KEYS or v is None or v <= 0]
        if bad:
            raise ProfileError(f"Geçersiz risk sınırı: {', '.join(bad)}.")
        return ProfileConfig(config.strategy, params, tuple(config.symbols), config.balance, tuple(triggers),
                             dict(config.risk))

    def _get(self, profile_id: int) -> Profile:
        try:
            return self.repo.get(profile_id)
        except KeyError:
            raise ProfileError(f"{profile_id} numaralı profil yok.") from None

    def _editable(self, p: Profile) -> None:
        if p.state is State.ARCHIVED:
            raise ProfileError(f"{p.name} arşivlenmiş, değiştirilemez.")

    def create(self, name: str, short: str, config: ProfileConfig, note: str = "İlk sürüm.") -> Profile:
        config = self.validate(config)
        now = self.clock.time()
        p = self.repo.create(name, short, Area.PAPER, now)
        self.repo.update(p.id, account_id=self.ledger.create_account("paper", config.balance, now))
        self.repo.add_version(p.id, config, note, now)
        self.on_change(p.id)
        return self.repo.get(p.id)

    def update_config(self, profile_id: int, config: ProfileConfig, note: str) -> Version:
        p = self._get(profile_id)
        self._editable(p)
        v = self.repo.add_version(p.id, self.validate(config), note, self.clock.time())
        self.on_change(p.id)
        return v

    def move(self, profile_id: int, action: str) -> Profile:
        """Profilin durumunu start, pause, stop ya da archive ile değiştirir."""
        allowed, target, verb = _MOVES[action]
        p = self._get(profile_id)
        if p.state not in allowed:
            raise ProfileError(f"{p.name} şu an {_STATE_NAMES[p.state]}, {verb}.")
        p = self.repo.update(p.id, state=target)
        if target is State.ARCHIVED and p.published:
            self.repo.publish(None)
        self.on_change(p.id)
        return self.repo.get(p.id)

    # Kopya taslak olarak başlar, kendi kâğıt hesabını sürümün bakiyesiyle açar.
    # Hafıza kopyalanır, LLM stratejisinde not defteri yeni profile geçer.
    def clone(self, profile_id: int, name: str, short: str) -> Profile:
        src = self._get(profile_id)
        config = self.repo.version(src.version_id).config
        p = self.create(name, short, config, note=f"{src.name} profilinden kopyalandı.")
        self.journal.set_memory(p.id, self.journal.memory(src.id))
        return p

    # Eski hesap kapanır ama kayıtları kalır. Hafıza silinir, eski hesabın
    # pozisyonlarını anlatan bir not defteri yeni hesapta yanıltır.
    def reset_account(self, profile_id: int) -> Profile:
        p = self._get(profile_id)
        if p.area is not Area.PAPER:
            raise ProfileError("Yalnızca kâğıt hesap sıfırlanabilir.")
        if p.state not in (State.DRAFT, State.STOPPED):
            raise ProfileError(f"{p.name} önce durdurulmalı.")
        now = self.clock.time()
        self.ledger.close_account(p.account_id, now)
        balance = self.repo.version(p.version_id).config.balance
        self.repo.update(p.id, account_id=self.ledger.create_account("paper", balance, now))
        self.journal.set_memory(p.id, {})
        self.on_change(p.id)
        return self.repo.get(p.id)

    def publish(self, profile_id: int | None) -> None:
        if profile_id is not None:
            p = self._get(profile_id)
            self._editable(p)
        self.repo.publish(profile_id)
