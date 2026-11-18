"""
file: scheduler.py
importable: Engine, journal_row, journal_list, trade_lists

Engine tek bir profilin hesabını yürütür, her profil için bir Engine kurulur.
start() önce program kapalıyken geçen süreyi yeniden işler, sonra
zamanlayıcıyı ve fonlama takibini başlatır. Canlı fiyatlar bütün profillerde
ortak tek bir akıştan gelir ve web katmanı her mumu on_candle() ile ilgili
profillerin Engine'ine verir. Akış kopup yeniden bağlanınca catch_up() aradaki
mumları REST'ten çekip işler. Her fiyat güncellemesinde hesabı kontrol eder. Bir emir, TP, SL, likidasyon ya da alarm
olursa Opus'u hemen çağırır, Opus'un istediği süre dolunca da tekrar çağırır.
prepare_shutdown() Opus'a sistemin kapanacağını bildirir, kararı
uygulanınca kurulurken verilen on_closed fonksiyonunu çağırır. Programı
kapatmak web katmanının işidir. Olan her şeyi listen() ile
alınan kuyruklara yayınlar, web arayüzü bunları dinler. intervene()
yöneticinin bir müdahalesini kaydeder ve istenirse Opus'u hemen çağırır.
on_liquidation() akıştan gelen likidasyonları profilin gözcülerine verir.

description: Fiyat akışını, hesabı, fonlamayı, kapanışları ve Opus çağrılarını yöneten ana döngü.
"""

# NOTE: Bir profilde aynı anda yalnızca bir Opus çağrısı yapılır. Çağrı
# sürerken yeni bir olay gelirse kaybolmaz, sıraya alınır ve çağrı biter
# bitmez Opus o olaylarla birlikte tekrar çağrılır. Farklı profillerin
# çağrıları birbirini beklemez, aynı anda sürebilir.
#
# NOTE: Opus'un kararları cevap geldiği andaki fiyattan uygulanır. Araç
# kullanan bir çağrı birkaç dakika sürebildiği için bu fiyat istemde
# gördüğü fiyattan farklı olabilir. Gerçek bir yatırımcı da araştırırken
# fiyat oynar, bu yüzden düzeltilmedi.
#
# NOTE: Fonlama olayları Opus'u ayrıca çağırmaz. Ödeme pozisyonların
# marjinine yansır ve Opus bunu bir sonraki çağrısında görür. Yalnızca
# fonlama yüzünden likidasyon olursa Opus hemen çağrılır.
#
# NOTE: Sistem bildirimleri, örneğin kapalı kalınan sürenin özeti, durum
# tablosunda bekletilir ve Opus'un bir sonraki çağrısının nedenine eklenir.
# Böylece kısa bir kapanıştan sonra Opus'u yalnızca haber vermek için
# çağırmak gerekmez, ama bilgi hiçbir zaman kaybolmaz.
#
# NOTE: Hesabı değiştiren her istek commands.py'deki kapıdan geçer: Opus'un
# eylemleri opus kaynağıyla, dönem sonundaki kapatma ve iptaller system
# kaynağıyla, yöneticinin kesin işlemleri admin kaynağıyla. Opus'un Türkçe
# eylemleri kapıya girmeden önce actions.py'de komuta çevrilir. Eksik bir
# alanın KeyError mesajı Türkçe adı taşır ve Opus'a aynen gösterilir.
#
# NOTE: Gözcüler watchers.py'dedir ve profil dosyasındaki watchers listesinden
# açılışta kurulur. Engine onlara likidasyonları, canlı mumları ve saniyede
# bir zamanı verir, bir gözcü mesaj döndürürse Opus'u gözcünün kind'ıyla
# çağırır. Gözcüsü olmayan bir profilde bu yolların hiçbiri bir şey yapmaz.

import asyncio
import json
import logging
import sqlite3
import time
from typing import Any, Awaitable, Callable

from .. import settings
from ..db import connect, get_state, rows, set_state
from ..market import analysis, indicators, liquidations
from ..market import data as market_data
from ..opus import actions, agent
from . import interventions, watchers
from .account import Account, AccountError
from .commands import Command, execute

log = logging.getLogger(__name__)

# Her çağrıda bütün paritelere aynı veri gider: parite kartı, bu
# aralıklardan hesaplanan göstergeler ve settings.PROMPT_CANDLES kadar ham
# mum. Pozisyonu ya da emri olan bir pariteye fazladan veri verilmez.
# Verilirse Opus'un dikkati bilgi düzeni yüzünden önceki seçimlerine
# kilitlenebilir. Daha uzun geçmişe Opus piyasa araçlarıyla kendisi bakar.
_INTERVALS = ("15m", "1h", "4h", "1d")

# Kartta 1 saatlik grafikte fiyatın üstünde ve altında kaçar seviye
# gösterildiği. 4 saatlikte bir tepenin iki yanında beşer mum yirmi saat
# ettiği için yakın tepe ve dipler henüz seviye sayılmıyor.
_CARD_LEVELS = 2

# Bu nedenlerin mesajı yalnızca o anki çağrı için anlamlıdır. Çağrı hata
# verirse bildirimlere aktarılmaz. Kapalı kalınan sürenin ayrıntısı zaten
# ayrı bir bildirimde durur.
# Yöneticinin müdahaleleri ise kendi tablolarında teslim edilene kadar
# bekledikleri için admin nedeni de bildirimlere aktarılmaz.
_QUIET_KINDS = ("timer", "retry", "restart", "shutdown", "offline_events", "admin")


# Günlük satırlarının başına profilin kısa adı eklenir, iki profil aynı
# terminale yazdığı için satırların hangi hesaba ait olduğu böyle anlaşılır.
class _Prefixed(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        return f"[{self.extra['prefix']}] {msg}", kwargs


class Engine:
    """Bir profilin hesabını, zamanlayıcısını ve Opus çağrılarını yürütür.

    Args:
        profile: Yürütülecek profil.
        market_con: Bütün profillerin ortak kullandığı likidasyon kayıtlarının bağlantısı.
        on_closed: Planlı kapanışta Opus'un son kararı uygulanınca çağrılır.
    """

    def __init__(self, profile: settings.Profile, market_con: sqlite3.Connection,
                 on_closed: Callable[[], None]) -> None:
        self.p = profile
        self.market_con = market_con
        self.log = _Prefixed(log, {"prefix": profile.short})
        self._on_closed = on_closed
        self.con = connect(profile.db_path)
        self.account = Account(self.con, profile)
        self.prices: dict[str, float] = {}
        self.busy = False
        self.shutting_down: dict | None = None
        self._last_candles: dict[str, dict] = {}
        self._last_minute = 0
        self._queue: list[tuple[str, str]] = []
        self._listeners: set[asyncio.Queue] = set()
        self._tasks: list[asyncio.Task] = []
        try:
            self._watchers = [watchers.build(w, list(profile.symbols)) for w in profile.watchers]
        except ValueError as e:
            raise SystemExit(f"{profile.id}: {e}")

    # --- Yayın ---

    def listen(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._listeners.add(q)
        return q

    def release(self, q: asyncio.Queue) -> None:
        self._listeners.discard(q)

    # Kuyruğu dolan bir dinleyici, örneğin uyku modundaki bir sekme, diğerlerini
    # bekletmesin diye mesaj o dinleyici için sessizce atlanır.
    def _broadcast(self, kind: str, data: dict) -> None:
        for q in self._listeners:
            if not q.full():
                q.put_nowait({"kind": kind, "data": data})

    def _broadcast_account(self) -> None:
        self._broadcast("state", self.state())
        self._broadcast("trades", trade_lists(self.con))

    # --- Durum ---

    @property
    def ended(self) -> bool:
        return bool(get_state(self.con, "end_reason"))

    def state(self) -> dict:
        """Arayüzün ve Opus istemin kullandığı hesap ve dönem durumunu döndürür."""
        prices = self.prices
        positions = self.account.positions()
        for p in positions:
            p["upnl"] = self.account.unrealized_pnl(p, prices[p["symbol"]])
        equity = self.account.equity(prices)
        return {
            "prices": prices,
            "cash": self.account.cash,
            "equity": equity,
            "return_pct": (equity / self.p.balance - 1) * 100,
            "start_balance": self.p.balance,
            "start_time": get_state(self.con, "start_time"),
            "end_time": get_state(self.con, "end_time"),
            "end_reason": get_state(self.con, "end_reason"),
            "positions": positions,
            "orders": self.account.pending_orders(),
            "alerts": self.account.alerts(),
            "stats": self.account.stats(),
            "notebook": get_state(self.con, "notebook", ""),
            "next_call": get_state(self.con, "next_call"),
            "offline_periods": get_state(self.con, "offline_periods", []),
            "busy": self.busy,
            "shutting_down": self.shutting_down,
            "symbols": self.p.symbols,
            "fee_taker": settings.FEE_TAKER,
            "fee_maker": settings.FEE_MAKER,
        }

    # --- Kayıt ---

    # Özkaynak dakikada bir ve hesabı değiştiren her olaydan sonra yazılır.
    # Bir paritenin fiyatı henüz gelmediyse, örneğin açılışın ilk saniyelerinde,
    # özkaynak hesaplanamaz ve o an atlanır. Kapalı sürenin yeniden işlenmesi
    # kendi fiyatlarını prices ile verir.
    def _record_equity(self, at: int | None = None, prices: dict[str, float] | None = None) -> None:
        prices = prices or self.prices
        positions = self.account.positions()
        if any(p["symbol"] not in prices for p in positions):
            return
        upnl = sum(self.account.unrealized_pnl(p, prices[p["symbol"]]) for p in positions)
        margin = sum(p["margin"] for p in positions)
        self.con.execute("INSERT OR REPLACE INTO equity (time, cash, equity, margin, upnl) VALUES (?, ?, ?, ?, ?)",
                         (at or int(time.time()), self.account.cash, self.account.equity(prices), margin, upnl))
        self.con.commit()

    def _system(self, kind: str, message: str, detail: dict | None = None) -> None:
        self.con.execute("INSERT INTO system_events (time, kind, message, detail) VALUES (?, ?, ?, ?)",
                         (int(time.time()), kind, message,
                          json.dumps(detail, ensure_ascii=False) if detail is not None else None))
        self.con.commit()

    def _add_notice(self, text: str) -> None:
        set_state(self.con, "notices", get_state(self.con, "notices", []) + [text])

    # Opus çağrısı hata verirse, örneğin abonelik limiti dolduysa, çağrıyı
    # tetikleyen olayların mesajı kaybolmasın diye bildirimlere eklenir.
    # Bildirimler veritabanında durduğu için program yeniden başlasa da
    # kaybolmaz ve ilk başarılı çağrıda Opus'a verilir. _QUIET_KINDS'taki
    # mesajlar yalnızca o anki çağrının nedenini söyler, bunlar eklenmez.
    # Yoksa hata saatlerce sürdüğünde aynı cümle her denemede birikirdi.
    def _keep_events(self, items: list[tuple[str, str]]) -> None:
        notices = get_state(self.con, "notices", [])
        kept = [m for k, m in items if k not in _QUIET_KINDS and m not in notices]
        if kept:
            set_state(self.con, "notices", notices + kept)

    # --- Başlatma ---

    async def _fetch_prices(self) -> None:
        d = await market_data.request("/fapi/v1/ticker/price")
        self.prices = {x["symbol"]: float(x["price"]) for x in d if x["symbol"] in self.p.symbols}

    # Dönem ilk kez başlıyorsa başlangıç ve bitiş zamanı ile her paritenin
    # son fonlama anı o an kaydedilir. Böylece dönemden önceki bir fonlama
    # yanlışlıkla uygulanmaz. Dönem sürüyorsa önce kapalı kalınan süre işlenir.
    async def start(self) -> None:
        await self._fetch_prices()
        now = int(time.time())
        if get_state(self.con, "start_time") is None:
            set_state(self.con, "start_time", now)
            set_state(self.con, "end_time", now + self.p.period_s)
            for s in self.p.symbols:
                set_state(self.con, f"last_funding_{s}", (await market_data.last_funding(s))["time"])
            set_state(self.con, "last_check", now // 60 * 60)
            set_state(self.con, "next_call", now)
            self._queue.append(("start", "Dönem yeni başladı, bu ilk çağrın."))
            self._system("period_start", "Dönem başladı.", {"end_time": now + self.p.period_s})
            self._record_equity(now)
        else:
            self._system("startup", "Program açıldı.")
            await self._replay_offline()
            if not self.ended and get_state(self.con, "next_call", 0) <= now:
                self._queue.append(("restart", "İstediğin çağrı zamanı sistem kapalıyken geçti."))

        self._tasks = [
            asyncio.create_task(self._timer()),
            asyncio.create_task(self._watch_funding()),
        ]
        if self._watchers:
            self._tasks.append(asyncio.create_task(self._tick_watchers()))
        if self._queue:
            self._start_call()

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        self.con.close()

    # Program kapalıyken hiçbir şey kontrol edilmez. Açılışta son görülen
    # dakikadan bu yana geçen bütün paritelerin dakikalık mumları ve
    # fonlamaları zaman sırasıyla hesaptan geçirilir. Böylece kapalı kalınan
    # süre hiç kapalı kalınmamış gibi işlenir. Kapalı dönem kaydedilir ve
    # Opus'a bir sonraki çağrıda bildirilir. Olay olduysa Opus hemen
    # çağrılır. Dönemin bitişi kapalı süreye denk geldiyse yalnızca bitişe
    # kadar işlenir ve dönem bitiş anındaki fiyatlarla kapatılır.
    async def _replay_offline(self) -> None:
        last = get_state(self.con, "last_check")
        shutdown = get_state(self.con, "shutdown")
        set_state(self.con, "shutdown", None)
        if not last or self.ended:
            return
        end_time = get_state(self.con, "end_time")
        now = int(time.time())
        start, stop = last + 60, min(now // 60 * 60, end_time)
        # Bir iki dakikalık beklenmedik kesintiler Opus'a bildirilmez. Planlı
        # bir kapanış ise ne kadar kısa olursa olsun bildirilir.
        if stop <= start and not shutdown:
            return

        stream: list[tuple[int, int, str, dict]] = []
        for s in (self.p.symbols if stop > start else []):
            for m in await market_data.minutes_between(s, start, stop):
                stream.append((m["t"], 1, s, m))
            last_fund = get_state(self.con, f"last_funding_{s}", 0)
            for f in await market_data.fundings_between(s, last_fund + 1, stop):
                if f["time"] > last_fund:
                    stream.append((f["time"], 0, s, f))
        stream.sort(key=lambda x: (x[0], x[1]))

        events: list[dict] = []
        last_price = dict(self.prices)
        minute = 0
        for t, kind, s, x in stream:
            # Özkaynak kapalı süre boyunca da dakikada bir yazılır, böylece
            # özkaynak eğrisinde boşluk kalmaz.
            if t // 60 > minute:
                if minute:
                    self._record_equity(t // 60 * 60, last_price)
                minute = t // 60
            if kind == 0:
                events += self.account.apply_funding(s, x["rate"], x["price"] or last_price[s], x["time"])
                set_state(self.con, f"last_funding_{s}", x["time"])
            else:
                last_price[s] = x["c"]
                for e in self.account.check_price(s, x["h"], x["l"], t):
                    e["message"] = f"{_clock(t)} civarında: {e['message']}"
                    events.append(e)
        if stop > start:
            set_state(self.con, "last_check", stop - 60)

        # Planlı kapanışta kapanış anı kayıtlıdır. Beklenmedik kapanışta en
        # iyi tahmin, canlı fiyatın en son görüldüğü dakikadır.
        went_down = shutdown["time"] if shutdown else last
        periods = get_state(self.con, "offline_periods", [])
        periods.append({"start": went_down, "end": now, "planned": bool(shutdown)})
        set_state(self.con, "offline_periods", periods)

        important = [e for e in events if e["kind"] != "funding"]
        fundings = sum(1 for e in events if e["kind"] == "funding")
        text = (f"Sistem {agent.date_text(went_down)} ile {agent.date_text(now)} arasında kapalıydı, "
                f"{'planlı bir kapanıştı' if shutdown else 'beklenmedik bir kapanıştı'}. Bu sürede çağrılamadın. "
                f"Emirlerin, TP, SL, iz süren stop ve likidasyon bu sürede de işlendi.")
        text += (" Kapalıyken olanlar: " + " ".join(e["message"] for e in important)) if important else \
            " Kapalıyken hiçbir emir, TP, SL, likidasyon ya da alarm tetiklenmedi."
        if fundings:
            text += f" Bu sürede {fundings} fonlama ödemesi uygulandı."
        self._add_notice(text)
        self._system("offline", text, {"start": went_down, "end": now, "planned": bool(shutdown),
                                       "replayed": len(stream), "important": len(important)})
        self.log.info("Sistem %d dakika kapalı kaldı. %d kayıt yeniden işlendi, %d önemli olay oldu.",
                 (now - went_down) // 60, len(stream), len(important))

        if now >= end_time:
            current, self.prices = self.prices, last_price
            self._end_period("Dönemin süresi doldu.")
            self.prices = current
        elif important:
            self._queue.append(("offline_events", "Sistem kapalıyken önemli olaylar oldu."))

    # --- Fiyat ---

    # Akış birkaç saniyede bir her paritenin güncel dakikalık mumunu gönderir.
    # İki güncelleme arasında fiyatın gittiği en uç noktalar, mumun en yüksek
    # ve en düşük değerindeki değişimden anlaşılır. Mumun yükseği arttıysa
    # fiyat arada o yeni yükseğe çıkmış demektir. Yeni bir dakika başladıysa
    # o mumun bütün aralığı son kontrolden sonra yaşanmıştır.
    def _range(self, m: dict) -> tuple[float, float]:
        prev, prev_price = self._last_candles.get(m["s"]), self.prices.get(m["s"])
        if not prev or prev_price is None:
            return m["c"], m["c"]
        if prev["t"] == m["t"]:
            high = max(prev_price, m["c"], m["h"] if m["h"] > prev["h"] else m["c"])
            low = min(prev_price, m["c"], m["l"] if m["l"] < prev["l"] else m["c"])
            return high, low
        return max(prev_price, m["h"]), min(prev_price, m["l"])

    async def on_candle(self, candle: dict, at: int | None = None) -> None:
        """Bu profilin paritelerinden birinin canlı mumunu işler.

        Args:
            candle: Akıştaki biçimde, s anahtarı paritenin sembolü olan mum.
            at: Mum sonradan işleniyorsa olayların yaşandığı tahmini an. Verilirse
                olay mesajlarının başına bu saat yazılır.
        """
        s = candle["s"]
        high, low = self._range(candle)
        self._last_candles[s], self.prices[s] = candle, candle["c"]
        # Yeni bir dakikaya geçildiğinde kaydedilir. Program kapanırsa açılışta
        # bu dakikadan sonrası yeniden işlenir.
        if candle["t"] > self._last_minute:
            self._last_minute = candle["t"]
            set_state(self.con, "last_check", candle["t"])
            if not self.ended:
                self._record_equity(at)
        self._broadcast("candle", candle)
        if self.ended:
            return
        events = self.account.check_price(s, high, low, at or int(time.time()))
        if at:
            for e in events:
                e["message"] = f"{_clock(at)} civarında: {e['message']}"
        self._handle_events(events)
        # Sonradan işlenen mumlar gözcülere verilmez, geç gelen bir bildirim
        # karar için işe yaramaz.
        if not at:
            for w in self._watchers:
                self._watched(w, w.on_candle(candle))

    # --- Gözcüler ---

    def on_liquidation(self, event: dict) -> None:
        """Akıştan gelen bir likidasyonu profilin gözcülerine verir.

        Args:
            event: time, symbol, side, price, qty ve usd anahtarlı kayıt.
        """
        for w in self._watchers:
            self._watched(w, w.on_liquidation(event))

    async def _tick_watchers(self) -> None:
        while True:
            await asyncio.sleep(1)
            now = time.time()
            for w in self._watchers:
                self._watched(w, w.tick(now))

    # Dönem bittiyse ya da planlı kapanış başladıysa gözcü Opus'u çağırmaz.
    # Kapanış çağrısı son karar içindir, araya yeni bir olay girmemeli.
    def _watched(self, w: watchers.Watcher, message: str | None) -> None:
        if message is None or self.ended or self.shutting_down:
            return
        self.log.info(message)
        self._broadcast("event", {"kind": w.kind, "message": message, "time": int(time.time())})
        self.trigger(w.kind, message)

    # Canlı akış koptuğunda yeniden bağlanana kadar geçen mumlar akıştan
    # gelmez. Akış her bağlandığında web katmanı bunu çağırır. Her paritenin
    # son görülen mumundan bu yana geçen dakikalık mumlar REST'ten çekilip
    # bütün paritelerde zaman sırasıyla on_candle'a verilir. Son görülen mum
    # da tekrar gelir, _range onun yalnızca kopuştan sonra yaşanan kısmını
    # sayar. Henüz açık olan dakikanın mumu da gelir, akış aynı dakikayla
    # devam edince yine yalnızca yeni kısmı sayılır. Olay anı mumun sonu
    # sayılır, böylece kopuştan hemen önce açılan bir pozisyonun kapanışı
    # açılışından önceye düşmez. İlk bağlantıda henüz mum görülmemiştir,
    # program kapalıyken geçen süreyi start() zaten işler.
    async def catch_up(self) -> None:
        """Canlı akış kopukken geçen mumları işler, akış her bağlandığında çağrılır."""
        if self.ended or not self._last_candles:
            return
        now = int(time.time())
        missed: list[dict] = []
        for s, prev in list(self._last_candles.items()):
            for m in await market_data.minutes_between(s, prev["t"], now // 60 * 60 + 60):
                missed.append({**m, "s": s})
        missed.sort(key=lambda m: m["t"])
        for m in missed:
            await self.on_candle(m, at=min(m["t"] + 59, now))
        if missed:
            self.log.info("Akış kopukken geçen süre %d mumla işlendi.", len(missed))

    def _handle_events(self, events: list[dict]) -> None:
        for e in events:
            self.log.info(e["message"])
            self._broadcast("event", {"kind": e["kind"], "message": e["message"], "time": int(time.time())})
            if e["kind"] != "funding":
                self.trigger(e["kind"], e["message"])
        if events:
            self._record_equity()
            self._broadcast_account()
            self._check_balance()

    # --- Zamanlayıcı, fonlama ve dönem sonu ---

    async def _timer(self) -> None:
        while True:
            await asyncio.sleep(5)
            if self.ended or self.shutting_down:
                continue
            now = time.time()
            if now >= get_state(self.con, "end_time"):
                self._end_period("Dönemin süresi doldu.")
                continue
            if interventions.due_calls(self.con, int(now)):
                self.trigger("admin", "Yöneticinin istediği çağrı zamanı geldi.")
                continue
            next_call = get_state(self.con, "next_call")
            if not self.busy and next_call and now >= next_call:
                # Son çağrı hata verdiyse bu çağrıyı Opus istemedi, ona
                # gerçek neden söylenir.
                if get_state(self.con, "retry"):
                    self.trigger("retry", "Önceki çağrı denemesi teknik bir hata nedeniyle tamamlanamadı, "
                                          "yeniden çağrılıyorsun.")
                else:
                    self.trigger("timer", "İstediğin bekleme süresi doldu.")

    # Binance'e dakikada bir her paritenin son gerçekleşen fonlaması sorulur.
    # Kayıtlıdan daha yeni bir fonlama varsa o paritenin pozisyonlarına
    # uygulanır. Fonlama aralığı paritelere göre farklı olabildiği için her
    # paritenin son fonlama anı ayrı tutulur.
    async def _watch_funding(self) -> None:
        while True:
            for s in self.p.symbols:
                try:
                    last = await market_data.last_funding(s)
                    if not self.ended and last["time"] > get_state(self.con, f"last_funding_{s}", 0):
                        set_state(self.con, f"last_funding_{s}", last["time"])
                        self._handle_events(self.account.apply_funding(
                            s, last["rate"], last["price"] or self.prices[s], last["time"]))
                except Exception as e:
                    self.log.warning("%s fonlama bilgisi alınamadı: %s", s, e)
            await asyncio.sleep(60)

    def _check_balance(self) -> None:
        if not self.account.positions() and self.account.cash < settings.END_EQUITY:
            self._end_period("Bakiye tükendi.")

    def _end_period(self, reason: str) -> None:
        if self.ended:
            return
        now = int(time.time())
        execute(self.account, self.prices, Command("close_all", {}, "system"), now)
        for a in self.account.alerts():
            execute(self.account, self.prices, Command("cancel_alert", {"alert_id": a["id"]}, "system"), now)
        set_state(self.con, "end_reason", reason)
        set_state(self.con, "next_call", None)
        self._record_equity(now)
        self._system("period_end", reason, {"cash": self.account.cash})
        self._queue.clear()
        self.log.info("Dönem bitti: %s Son özkaynak %.2f dolar.", reason, self.account.cash)
        self._broadcast_account()

    # --- Kapanış ---

    # Opus bir kez çağrılır ve sistemin kapanacağı söylenir. Kararı uygulandıktan
    # sonra kapanış kaydedilir ve program kendine kesme sinyali gönderir.
    # Uvicorn bunu Ctrl+C gibi karşılar ve düzgünce kapanır. Opus o an başka
    # bir çağrıdaysa önce o biter, kapanış çağrısı sıraya girer.
    def prepare_shutdown(self, return_at: int | None) -> None:
        """Opus'a sistemin kapanacağını bildirir, kararı uygulanınca programı kapatır.

        Args:
            return_at: Tahmini dönüş anı, unix saniye. Bilinmiyorsa None.
        """
        if self.shutting_down:
            return
        self.shutting_down = {"time": int(time.time()), "return_at": return_at}
        if self.ended:
            self._shut_down()
            return
        when = f"Tahmini dönüş {agent.date_text(return_at)}." if return_at else "Dönüş zamanı belli değil."
        self.trigger("shutdown", f"Sistem bu çağrıdan hemen sonra kapanacak. {when} Kapalıyken çağrılamazsın ve "
                                 f"alarmların seni çağıramaz. Emirlerin, TP, SL, iz süren stop ve likidasyon işlemeye "
                                 f"devam eder. Sistem açılınca kapalı kalınan süre sana bildirilir.")
        self._broadcast("state", self.state())

    def _shut_down(self) -> None:
        set_state(self.con, "shutdown", self.shutting_down)
        self._system("shutdown", "Planlı kapanış.", self.shutting_down)
        self.log.info("Sistem kapanıyor.")
        self._broadcast("closed", {"time": int(time.time())})
        self._on_closed()

    # --- Müdahale ---

    def intervene(self, kind: str, params: dict, weight: str | None = None, note: str = "",
                  call_now: bool = False) -> dict:
        """Yöneticinin bir müdahalesini kaydeder, kesinse uygular, istenirse Opus'u hemen çağırır.

        Args:
            kind: interventions.KINDS içinden müdahalenin türü.
            params: Türe göre parametreler.
            weight: offer ya da firm, ağırlığı olmayan türlerde None.
            note: Yöneticinin isteğe bağlı açıklaması.
            call_now: True ise Opus hemen çağrılır, False ise müdahale sonraki çağrıda görünür.

        Returns:
            Kaydedilen müdahale.

        Raises:
            ValueError: Girdi geçersizse ya da dönem bittiyse.
            AccountError: Kesin bir hesap işlemi hesap kurallarına takılırsa.
        """
        if self.ended:
            raise ValueError("Dönem bitti, müdahale yapılamaz.")
        rec = interventions.add(self.con, self.account, self.prices, kind, params, weight, note, call_now,
                                int(time.time()))
        self.log.info("Yönetici müdahalesi: %s %s", kind, weight or "")
        self._record_equity()
        self._broadcast_account()
        self._check_balance()
        if call_now:
            self.trigger("admin", "Yönetici seni çağırdı.")
        return rec

    # --- Opus çağrısı ---

    def trigger(self, kind: str, message: str) -> None:
        """Opus'u verilen nedenle çağırır, meşgulse sıraya alır. Dönem bittiyse bir şey yapmaz."""
        if self.ended:
            return
        self._queue.append((kind, message))
        if not self.busy:
            self._start_call()

    def _start_call(self) -> None:
        self.busy = True
        asyncio.create_task(self._call_loop())

    # Sıradaki bütün olaylar tek bir çağrıda birleştirilir. Kapanış bildirimi
    # varsa en sona alınır, böylece Opus kapanmadan önceki son kararını bütün
    # olayları görerek verir. Çağrı hata verirse birleştirilen olaylar
    # _keep_events() ile bildirimlere aktarılır.
    async def _call_loop(self) -> None:
        self._broadcast("state", self.state())
        items: list[tuple[str, str]] = []
        try:
            while self._queue and not self.ended:
                self._queue.sort(key=lambda x: x[0] == "shutdown")
                kind = self._queue[-1][0] if self._queue[-1][0] == "shutdown" else self._queue[0][0]
                items = list(self._queue)
                message = " ".join(m for _, m in items)
                self._queue.clear()
                if not await self._call_opus(kind, message, items):
                    self._keep_events(items)
                items = []
        except Exception as e:
            self.log.exception("Opus çağrı döngüsünde beklenmeyen hata.")
            self._system("loop_error", "Opus çağrı döngüsünde beklenmeyen hata.", {"error": repr(e)})
            self._keep_events(items)
            set_state(self.con, "next_call", int(time.time()) + settings.ERROR_RETRY_MIN * 60)
        finally:
            self.busy = False
            self._broadcast("state", self.state())
            if self.shutting_down:
                self._shut_down()

    # Her pariteye aynı alanlardan oluşan bir kart hazırlanır: özet, açık
    # pozisyon, long short dağılımı, likidasyonlar, yakın seviyeler,
    # göstergeler ve ham mumlar. Pozisyonu ya da emri olan bir parite fazladan
    # veri almaz. Açık pozisyon ve long short verisi alınamazsa çağrı
    # durdurulmaz, kartta veri yok yazar.
    async def _gather_market(self, state: dict) -> dict:
        since = int(time.time()) - 86400

        async def one(s: str) -> tuple[str, dict, dict]:
            lists = dict(zip(_INTERVALS, await asyncio.gather(
                *(market_data.candles(i, 250, s) for i in _INTERVALS))))
            day, fund, oi, pos = await asyncio.gather(
                market_data.daily_summary(s), market_data.funding_status(s),
                self._optional(market_data.open_interest_change(s)), self._optional(market_data.positioning(s)))
            ind = {i: indicators.summary(m) for i, m in lists.items()}
            g4, g1d = ind["4h"], ind["1d"]
            side = lambda g, name: "üstünde" if g["close"] > g[name] else "altında"
            daily = f"EMA50 {side(g1d, 'ema50')}" + (f", EMA200 {side(g1d, 'ema200')}" if "ema200" in g1d else "")
            days = lists["1d"]
            price = self.prices[s]
            found = analysis.levels(lists["1h"])
            waves = liquidations.waves(self.market_con, s, since)
            summary = {"price": price, "change_pct": day["change_pct"],
                       "change_7d": (days[-1]["c"] / days[-8]["c"] - 1) * 100 if len(days) >= 8 else 0.0,
                       "volume_usd": day["volume_usd"], "funding": fund["est_rate"],
                       "next_funding": fund["next_funding"],
                       "trend_4h": f"EMA20 {side(g4, 'ema20')}, EMA50 {side(g4, 'ema50')}", "trend_1d": daily,
                       "volatility": g1d["atr14"] / g1d["close"] * 100, "open_interest": oi, "positioning": pos,
                       "liquidations": liquidations.totals(self.market_con, s, since),
                       "liquidation_wave": max(waves, key=lambda w: w["long_usd"] + w["short_usd"], default=None),
                       "levels_above": [x for x in found if x["price"] > price][:_CARD_LEVELS],
                       "levels_below": [x for x in found if x["price"] <= price][-_CARD_LEVELS:]}
            detail = {"indicators": ind, "candles": {i: lists[i][-n:] for i in _INTERVALS
                                                     if (n := settings.PROMPT_CANDLES.get(i))}}
            return s, summary, detail

        result = await asyncio.gather(*(one(s) for s in self.p.symbols))
        market = {"summary": {s: x for s, x, _ in result}, "detail": {s: d for s, _, d in result}}
        return market

    async def _optional(self, job: Awaitable) -> Any:
        try:
            return await job
        except Exception as e:
            self.log.warning("Parite kartı için ek veri alınamadı: %s", e)
            return None

    def _recent_decisions(self) -> list[dict]:
        found = rows(
            self.con,
            "SELECT time, decision, action_results FROM journal WHERE decision IS NOT NULL ORDER BY id DESC LIMIT ?",
            (settings.PAST_DECISIONS,),
        )
        return [
            {"time": r["time"], "summary": json.loads(r["decision"])["ozet"],
             "results": [x["message"] for x in json.loads(r["action_results"])]}
            for r in reversed(found)
        ]

    # Çağrı hata verdiyse False döner, olayların mesajını _call_loop() saklar.
    async def _call_opus(self, kind: str, message: str, items: list[tuple[str, str]] | None = None) -> bool:
        now = int(time.time())
        notices = get_state(self.con, "notices", [])
        if notices:
            message = " ".join(notices) + " " + message
        rec = {"time": now, "trigger": kind, "trigger_detail": message,
               "events": [{"kind": k, "message": m} for k, m in (items or [(kind, message)])] +
                         [{"kind": "notice", "message": n} for n in notices],
               "model": settings.MODEL, "effort": settings.EFFORT, "code_version": agent.code_version(),
               "tools_digest": await agent.tools_digest()}
        # İstem kurulurken görülen müdahaleler ancak çağrı başarılı olursa
        # teslim edilmiş sayılır. Çağrı sürerken gelenler bir sonrakine kalır.
        shown = interventions.pending(self.con, now)
        admin = {"items": interventions.lines(shown),
                 "restrictions": interventions.restriction_lines(interventions.restrictions(self.con, now))}
        try:
            state = self.state()
            prompt = agent.build_prompt(self.p, message, state, await self._gather_market(state),
                                        self._recent_decisions(), admin)
            rec["prompt"] = prompt
            system = agent.system_prompt(self.p, state["start_time"], state["end_time"])
            self.log.info("Opus çağrılıyor: %s", message[:200])
            answer = await agent.call(self.p, system, prompt, lambda t: self._broadcast("tool", t))
        except Exception as e:
            self.log.warning("Opus çağrısı başarısız: %s", e)
            rec["error"] = str(e)
            rec["tools"] = getattr(e, "tools", [])
            rec["next_call"] = now + settings.ERROR_RETRY_MIN * 60
            self._system("call_error", "Opus çağrısı başarısız oldu.", {"error": str(e)})
            set_state(self.con, "retry", True)
            self._save(rec)
            return False

        set_state(self.con, "notices", [])
        set_state(self.con, "retry", False)
        interventions.mark_delivered(self.con, [r["id"] for r in shown], int(time.time()))
        decision = answer["decision"]
        rec["prices"] = dict(self.prices)
        results = self._apply(decision["eylemler"])
        self._record_equity()
        wait = max(self.p.min_wait, min(self.p.max_wait, decision["sonraki_cagri_dk"]))
        set_state(self.con, "notebook", decision["not_defteri"])
        rec.update(
            raw_response=answer["raw"], decision=decision,
            action_results=results, tools=answer["tools"], next_call=int(time.time()) + wait * 60,
            duration_s=answer["duration_s"], cost_usd=answer["cost_usd"], usage=answer["usage"],
        )
        self._save(rec)
        self._check_balance()
        self.log.info("Opus karar verdi, %d araç kullandı: %s", len(answer["tools"]), decision["ozet"])
        return True

    # Araçların sonuçları büyük olabildiği için journal'a girmez, tool_calls
    # tablosuna her araç bir satır olarak yazılır. journal'daki tools alanı
    # arayüzün listesi için sonuçsuz kısa halidir.
    def _save(self, rec: dict) -> None:
        set_state(self.con, "next_call", rec["next_call"])
        fields = dict(rec)
        full = fields.get("tools") or []
        fields["tools"] = [{k: v for k, v in x.items() if k not in ("result", "id")} for x in full]
        for name in ("decision", "action_results", "tools", "events", "usage", "prices"):
            if name in fields:
                fields[name] = json.dumps(fields[name], ensure_ascii=False)
        cur = self.con.execute(
            f"INSERT INTO journal ({', '.join(fields)}) VALUES ({', '.join('?' * len(fields))})",
            tuple(fields.values()),
        )
        self.con.executemany(
            "INSERT INTO tool_calls (journal_id, seq, name, title, input, result, is_error, duration_s, sub) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(cur.lastrowid, i, x["name"], x.get("title"), json.dumps(x.get("input"), ensure_ascii=False),
              x.get("result"), x.get("is_error"), x.get("duration_s"), int(bool(x.get("sub"))))
             for i, x in enumerate(full)],
        )
        self.con.commit()
        self._broadcast("journal", journal_row(self.con, cur.lastrowid))
        self._broadcast_account()

    # Eylemler sırayla uygulanır, biri reddedilirse diğerleri yine denenir.
    # Her eylemin sonucu Opus'a bir sonraki çağrıda gösterilir, böylece
    # reddedilen bir isteği ve nedenini görür. Kesin kısıtlar her eylemden
    # önce yeniden okunur, eylemler arasında bir kısıtın süresi dolabilir.
    def _apply(self, items: list[dict]) -> list[dict]:
        results = []
        for a in items:
            now, kind = int(time.time()), a.get("tip", "")
            try:
                cmd = actions.to_command(a)
                limits = interventions.restrictions(self.con, now, firm_only=True)
                results.append({"type": kind, "ok": True,
                                "message": execute(self.account, self.prices, cmd, now, limits)})
            except AccountError as e:
                results.append({"type": kind, "ok": False, "message": f"{kind} reddedildi: {e}"})
            except KeyError as k:
                results.append({"type": kind, "ok": False, "message": f"{kind} reddedildi: {k} alanı eksik."})
        return results


def _clock(t: int) -> str:
    return time.strftime("%d.%m %H:%M", time.localtime(t))


# --- Web için okuma yardımcıları ---

PUBLIC_ERROR = "Sistemsel bir hata oluştu."

# prompt ve raw_response büyük olduğu için liste görünümünde gönderilmez,
# arayüz bir kartın ayrıntısını açınca tam kaydı ayrıca ister.
def journal_row(con, id_: int, full: bool = False) -> dict:
    fields = "*" if full else ("id, time, trigger, trigger_detail, decision, action_results, tools, "
                               "next_call, duration_s, cost_usd, error")
    r = rows(con, f"SELECT {fields} FROM journal WHERE id = ?", (id_,))[0]
    for name in ("decision", "action_results", "tools", "events", "usage", "prices"):
        if name in r:
            r[name] = json.loads(r[name]) if r[name] else None
    # Sayfa herkese açık. Hatanın ham metni, örneğin abonelik limitinin
    # dolduğu, sayfada gösterilmez. Ham metin veritabanında ve servis
    # kayıtlarında durur, teşhis için oradan okunur.
    if r.get("error"):
        r["error"] = PUBLIC_ERROR
    return r


def journal_list(con, limit: int = 300) -> list[dict]:
    ids = [r["id"] for r in rows(con, "SELECT id FROM journal ORDER BY id DESC LIMIT ?", (limit,))]
    return [journal_row(con, i) for i in ids]


def trade_lists(con) -> dict:
    out = {
        "trades": rows(con, "SELECT * FROM trades ORDER BY id"),
        "orders": rows(con, "SELECT * FROM orders ORDER BY id"),
        "alerts": rows(con, "SELECT * FROM alerts ORDER BY id"),
    }
    for table in ("trades", "orders"):
        for r in out[table]:
            r["tp_levels"] = json.loads(r["tp_levels"])
    return out
