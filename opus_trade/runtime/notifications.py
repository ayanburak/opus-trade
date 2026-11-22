"""
file: notifications.py
importable: Channel, ChannelError, PanelChannel, TelegramChannel, Notifier, DEFAULT_SETTINGS, settings_from

Yöneticiye giden bildirimler. Notifier olay yolunu dinler, bildirim doğuran
olayları seviyesine göre ayırır, NotificationRepo'ya yazar ve her kanala
gönderir. Olayı yayan taraf bildirimden habersizdir.

Kanallar Channel soyut sınıfına uyar. PanelChannel bildirimi olay yoluna
koyar, panel onu canlı akıştan alır, sesi ve tarayıcı bildirimini kendisi
çalar. TelegramChannel bildirimi bir bot üzerinden gönderir.

settings_from() panelden gelen ses ayarlarını doğrular, DEFAULT_SETTINGS
hiç ayar kaydedilmemişken geçerli olanlardır.

description: Bildirimlerin sınıflandırılması, saklanması ve kanallara gönderimi.
"""

# NOTE: push() olay yolu dışından gelen bildirim içindir, örneğin opus-web
# çekirdeğe ulaşamadığı süreyi çekirdek geri gelince böyle yazar.
#
# NOTE: Seviyeler üçtür. Kritik okunana kadar baloncukta kalır ve iki notalı
# sesle çalar, önemli tek kısa sesle çalar ve sayılır, bilgi yalnızca listede
# durur. Hangi olayın hangi seviyede olduğu yalnızca _classify içindedir.
#
# NOTE: Notifier'ın kuyruğu sınırsızdır, panelin kuyruğu gibi taşınca olay
# atlamaz. Kendi yaydığı notification olaylarını ve araç çağrılarını yok
# sayar.
#
# NOTE: Art arda karar hatası ve model limiti profil başına sayılır, başarılı
# bir günlük kaydı sayacı sıfırlar. Limit bildirimi sistem geneli tektir,
# başarılı bir karar gelene kadar ikinci kez gönderilmez.
#
# TODO: Telegram kanalı yazıldı ve testleri var ama çekirdeğe bağlı değil. Bot
# anahtarı ve sohbet kimliği canlıya geçişten önce sunucudaki bir dosyaya
# yazılıp core.py'de kanal listesine eklenecek.
#
# TODO: Canlı alanın Binance reddi ve mutabakat farkı 8. aşamada kritik olarak
# buraya eklenecek.

import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx

from ..domain import numbers
from ..domain.clock import Clock
from ..domain.trading import AccountEvent, Actor, EventKind
from ..storage.journal import JournalRepo
from ..storage.notifications import NotificationRepo
from ..storage.profiles import ProfileRepo
from ..strategy.llm.texts import clock as clock_text
from .bus import EventBus

log = logging.getLogger(__name__)

# Art arda bu kadar karar hatası kritik bildirimdir.
ERROR_STREAK = 3

_ACTORS = {str(Actor.STRATEGY): "strateji", str(Actor.ADMIN): "yönetici", str(Actor.SYSTEM): "sistem"}


@dataclass(frozen=True)
class _Note:
    level: str
    kind: str
    profile_id: int | None
    title: str
    body: str = ""


# --- Kanallar ---

class ChannelError(Exception):
    pass


class Channel(ABC):
    @abstractmethod
    async def send(self, n: dict) -> None:
        """Kaydedilmiş bir bildirimi gönderir. n'de profile_name de bulunur.

        Raises:
            ChannelError: Bildirim gönderilemezse.
        """


class PanelChannel(Channel):
    def __init__(self, bus: EventBus) -> None:
        self.bus = bus

    async def send(self, n: dict) -> None:
        self.bus.publish({"notification": n})


# Hata metinleri adresi taşımaz, çünkü adreste bot anahtarı vardır ve hata
# günlüğe yazılır.
class TelegramChannel(Channel):
    """Bir Telegram botu üzerinden tek bir sohbete gönderen kanal.

    Args:
        token: Botun anahtarı.
        chat_id: Mesajların gideceği sohbet.
        client: İstekleri yapan istemci.
        levels: Gönderilecek seviyeler. Bilgi seviyesi varsayılan olarak gitmez.
        base_url: Telegram Bot API'sinin adresi, testte sahte sunucu verilir.
    """

    def __init__(self, token: str, chat_id: str | int, client: httpx.AsyncClient,
                 levels: tuple[str, ...] = ("critical", "important"),
                 base_url: str = "https://api.telegram.org") -> None:
        self.token = token
        self.chat_id = chat_id
        self.client = client
        self.levels = levels
        self.base_url = base_url.rstrip("/")

    async def _call(self, method: str, payload: dict | None = None) -> dict:
        try:
            r = await self.client.post(f"{self.base_url}/bot{self.token}/{method}", json=payload or {})
        except httpx.HTTPError as e:
            raise ChannelError(f"Telegram'a ulaşılamadı: {type(e).__name__}") from None
        try:
            body = r.json()
        except ValueError:
            raise ChannelError(f"Telegram {r.status_code} döndü, cevap okunamadı.") from None
        if not body.get("ok"):
            raise ChannelError(f"Telegram reddetti: {body.get('description') or r.status_code}")
        return body.get("result") or {}

    @staticmethod
    def text(n: dict) -> str:
        mark = {"critical": "🔴", "important": "🔵", "info": "⚪"}[n["level"]]
        who = n.get("profile_name") or "Sistem"
        return f"{mark} {who}: {n['title']}" + (f"\n{n['body']}" if n["body"] else "")

    # Yalnızca kritik bildirim telefonda sesli gelir, diğerleri sessiz düşer.
    async def send(self, n: dict) -> None:
        if n["level"] not in self.levels:
            return
        await self._call("sendMessage", {"chat_id": self.chat_id, "text": self.text(n),
                                         "disable_notification": n["level"] != "critical"})

    async def check(self) -> str:
        """Anahtarın geçerli olduğunu doğrular ve botun kullanıcı adını döndürür.

        Raises:
            ChannelError: Anahtar geçersizse ya da Telegram'a ulaşılamazsa.
        """
        return (await self._call("getMe")).get("username", "")


# --- Ses ayarları ---

DEFAULT_SETTINGS = {"volume": 0.7, "sound": {"critical": True, "important": True},
                    "quiet": {"enabled": False, "start": "23:00", "end": "08:00"}}
_HOUR = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def settings_from(d: dict) -> dict:
    """Panelin ses ayarlarını doğrular ve eksik alanları varsayılanla doldurur.

    Raises:
        ValueError: Ses düzeyi 0 ile 1 arasında değilse ya da saat SS:DD biçiminde değilse.
    """
    volume = float(d.get("volume", DEFAULT_SETTINGS["volume"]))
    if not 0 <= volume <= 1:
        raise ValueError("Ses düzeyi 0 ile 1 arasında olmalı.")
    sound = {k: bool((d.get("sound") or {}).get(k, v)) for k, v in DEFAULT_SETTINGS["sound"].items()}
    quiet = {**DEFAULT_SETTINGS["quiet"], **(d.get("quiet") or {})}
    for k in ("start", "end"):
        if not _HOUR.match(str(quiet[k])):
            raise ValueError("Sessiz saatler 23:00 biçiminde yazılmalı.")
    return {"volume": volume, "sound": sound,
            "quiet": {"enabled": bool(quiet["enabled"]), "start": quiet["start"], "end": quiet["end"]}}


# --- Bildirici ---

class Notifier:
    """Olay yolundan bildirim üreten dinleyici.

    Args:
        bus: Dinlenen olay yolu. Kuyruk kurulurken açılır, böylece run() başlamadan yayılanlar da kaybolmaz.
        repo: Bildirimlerin deposu.
        profiles: Hesaptan profili ve profil adını bulmak için.
        journal: Opus'un cevabını ve düzeltme çağrısını okumak için.
        clock: Saat.
        channels: Bildirimlerin gönderileceği kanallar.
    """

    def __init__(self, bus: EventBus, repo: NotificationRepo, profiles: ProfileRepo, journal: JournalRepo,
                 clock: Clock, channels: list[Channel]) -> None:
        self.bus = bus
        self.repo = repo
        self.profiles = profiles
        self.journal = journal
        self.clock = clock
        self.channels = channels
        self.queue = bus.subscribe(unbounded=True)
        self._accounts: dict[int, int] = {}
        self._errors: dict[int, int] = {}
        self._limit_sent = False

    def _profile_of(self, account_id: int) -> int | None:
        if account_id not in self._accounts:
            self._accounts = {p.account_id: p.id for p in self.profiles.all() if p.account_id is not None}
        return self._accounts.get(account_id)

    def _account(self, e: AccountEvent) -> list[_Note]:
        pid = self._profile_of(e.account_id)
        head = f"{f'#{e.number} ' if e.number else ''}{e.symbol} {e.side}"
        match e.kind:
            case EventKind.OPENED:
                return [_Note("important", "opened", pid, f"{head} açıldı",
                              f"Giriş {numbers.price(e.price)}, açan {_ACTORS.get(str(e.actor), str(e.actor))}.")]
            case EventKind.CLOSED:
                t = e.trade
                net = f"Çıkış {numbers.price(e.price)}, net {numbers.num(t.net, 2, sign=True)} dolar."
                if t.reason == "liquidation":
                    return [_Note("critical", "liquidation", pid, f"{head} likide oldu", net)]
                if t.reason in ("tp", "sl"):
                    return [_Note("important", t.reason, pid, f"{head} {t.reason.upper()} çalıştı", net)]
                return [_Note("important", "closed", pid, f"{head} kapandı",
                              f"{net} Kapatan {_ACTORS.get(str(e.actor), str(e.actor))}.")]
            case EventKind.ORDER_FILLED:
                return [_Note("important", "order_filled", pid, f"{head} emri doldu",
                              f"Fiyat {numbers.price(e.price)}.")]
            case EventKind.ORDER_EXPIRED:
                return [_Note("info", "order_expired", pid, f"{head} emrinin süresi doldu")]
            case EventKind.ORDER_FAILED:
                return [_Note("info", "order_failed", pid, f"{head} emri açılamadı", e.reason or "")]
        return []

    def _journal(self, pid: int, jid: int) -> list[_Note]:
        self._errors[pid] = 0
        self._limit_sent = False
        entry = self.journal.get(jid)
        notes = []
        rejection = entry["record"].get("rejection")
        if rejection:
            notes.append(_Note("info", "rejected", pid, "Karar kontrol hatasıyla reddedildi", " ".join(rejection)[:500]))
        elif any(w["kind"] == "correction" for w in entry["wakes"]):
            failed = [r["message"] for r in entry["results"] if not r["ok"]]
            notes.append(_Note("info", "correction", pid, "Düzeltme çağrısı yapıldı", " ".join(failed)[:500]))
        reply = (entry["record"].get("reply") or "").strip()
        if reply:
            notes.append(_Note("important", "reply", pid, "Opus yöneticiye cevap yazdı", reply[:1000]))
        return notes

    def _error(self, pid: int, text: str, limit: bool) -> list[_Note]:
        self._errors[pid] = self._errors.get(pid, 0) + 1
        if limit:
            if self._limit_sent:
                return []
            self._limit_sent = True
            return [_Note("critical", "model_limit", None, "Model limiti doldu", text[:500])]
        if self._errors[pid] == ERROR_STREAK:
            return [_Note("critical", "decide_errors", pid, f"Art arda {ERROR_STREAK} karar hatası",
                          f"Son hata: {text[:500]}")]
        return []

    def _classify(self, item) -> list[_Note]:
        if isinstance(item, AccountEvent):
            return self._account(item)
        if not isinstance(item, dict):
            return []
        pid = item.get("profile")
        if "journal" in item:
            return self._journal(pid, item["journal"])
        if "decide_error" in item:
            return self._error(pid, item["decide_error"], item.get("limit", False))
        if "decision_rejected" in item:
            return [_Note("critical", "decision_rejected", pid, "Karar düzeltme hakkı bitince de reddedildi",
                          "Bu çağrıda hiçbir eylem uygulanmadı, defter değişmedi. Son ret: "
                          + " ".join(item["decision_rejected"])[:500])]
        if "suggestion" in item:
            return [_Note("important", "suggestion", pid, "Opus bir öneri yazdı", item["suggestion"][:1000])]
        if "daily_loss" in item:
            return [_Note("critical", "daily_loss", pid, "Günlük zarar sınırı aşıldı",
                          f"Sınır {numbers.num(item['daily_loss'])} dolar. Bugün yeni risk alınmıyor, açık pozisyonlar "
                          f"korunuyor.")]
        if "version" in item:
            return [_Note("info", "version", pid, f"Sürüm {item['version']} yürürlükte")]
        if item.get("fire") is not None and item["fire"].kind == "alert":
            a = item["fire"].data
            return [_Note("info", "alert", pid, f"{a.symbol} {numbers.price(a.price)} alarmı tetiklendi",
                          f"Kuran {_ACTORS.get(a.actor, a.actor)}." + (f" Not: {a.note}" if a.note else ""))]
        if item.get("stream") == "down":
            return [_Note("critical", "stream_down", None, "Piyasa akışı koptu",
                          f"İki dakikadan uzun süredir mum gelmiyor. Son mum {clock_text(item['since'])}.")]
        if item.get("collector") == "down":
            return [_Note("critical", "collector_down", None, "Veri toplayıcı durdu",
                          f"Beş dakikadan uzun süredir veri yazmıyor. Son kayıt {clock_text(item['since'])}.")]
        if item.get("collector") == "up":
            return [_Note("info", "collector_up", None, "Veri toplayıcı yeniden yazıyor")]
        if item.get("stream") == "up":
            return [_Note("info", "stream_up", None, "Piyasa akışı geri geldi",
                          f"{item['silent_s'] // 60} dakika kopuk kaldı, kaçan dakikalar işleniyor.")]
        return []

    async def push(self, level: str, kind: str, profile_id: int | None, title: str, body: str = "") -> dict:
        """Bir bildirimi kaydeder ve kanallara gönderir. Bir kanalın hatası diğerlerini durdurmaz.

        Raises:
            ValueError: Seviye bilinmiyorsa.
        """
        n = self.repo.add(self.clock.time(), profile_id, level, kind, title, body)
        name = self.profiles.get(profile_id).name if profile_id is not None else None
        for ch in self.channels:
            try:
                await ch.send({**n, "profile_name": name})
            except Exception as e:
                log.warning("Bildirim %s kanalına gönderilemedi: %s", type(ch).__name__, e)
        return n

    async def handle(self, item) -> list[dict]:
        """Bir olayı işler ve üretilen bildirimleri döndürür."""
        return [await self.push(n.level, n.kind, n.profile_id, n.title, n.body) for n in self._classify(item)]

    async def drain(self) -> None:
        """Kuyrukta bekleyen olayların hepsini işler. Testler ve kapanış için."""
        while not self.queue.empty():
            await self._safe(self.queue.get_nowait())

    async def _safe(self, item) -> None:
        try:
            await self.handle(item)
        except Exception:
            log.exception("Bildirim üretilemedi.")

    async def run(self) -> None:
        """İptal edilene kadar olay yolunu dinler."""
        try:
            while True:
                await self._safe(await self.queue.get())
        finally:
            self.bus.unsubscribe(self.queue)

