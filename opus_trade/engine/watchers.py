"""
file: watchers.py
importable: Watcher, LiquidationWave, CATALOG, build

Gözcüler piyasayı izleyip hesapta olmayan bir olay gördüklerinde Opus'u
çağırmak için bir mesaj üretir. Her gözcü Watcher soyut sınıfına uyar ve üç
girişi vardır: on_liquidation() akıştan gelen her likidasyonu, on_candle()
her canlı mumu, tick() saniyede bir geçen zamanı alır. Bir giriş olay
bulursa Opus'a gidecek Türkçe mesajı döndürür, bulmazsa None döndürür.
build() bir gözcü tanımından, yani type anahtarı ve ayarlarını taşıyan bir
sözlükten, CATALOG'daki sınıfı kurar. Engine her profilin gözcülerini
açılışta build() ile kurar ve döndürdükleri mesajla Opus'u çağırır.

description: Piyasadaki olayları izleyip Opus'u çağıran, katalogla genişleyen gözcüler.
"""

# NOTE: Bugün gözcüleri yalnızca profil dosyasının watchers listesi kurar.
# İleride Opus'un kendisinin bir eylemle gözcü kurması istenirse aynı
# build() kullanılır, eylem bir gözcü tanımı üretip Engine'e verir. Bu
# yüzden tanım düz bir sözlüktür ve kurulum katalogdan geçer.
#
# NOTE: Yeni bir gözcü Watcher'dan türeyen bir sınıf ve CATALOG'a bir satırla
# eklenir. Engine'e ve zamanlayıcıya dokunmak gerekmez. Gözcü yalnızca
# ihtiyaç duyduğu girişi ezer, diğerleri hiçbir şey yapmaz.
#
# NOTE: Gözcülerin durumu bellektedir. Program yeniden başlarsa yarım kalan
# dalga unutulur. Program kapalıyken geçen süre yeniden işlenirken de
# gözcüler çalışmaz, çünkü geç gelen bir bildirim karar için işe yaramaz.
#
# XXX: Her gözcü çağrısı abonelik limitinden düşer. cooldown_s bu yüzden
# vardır ve sıfır verilse bile aynı gözcü dakikada birden sık çağıramaz.

import time
from abc import ABC
from typing import Any

# Aynı gözcünün iki çağrısı arasında en az bu kadar saniye geçer.
_MIN_COOLDOWN_S = 60


def _clock(t: float) -> str:
    return time.strftime("%H:%M", time.localtime(t))


def _price(x: float) -> str:
    return f"{x:.1f}" if x >= 1000 else f"{x:.2f}" if x >= 10 else f"{x:.4f}"


# Bir milyonun altı bin dolarla yazılır, yoksa küçük taraf 0,00 milyon görünür.
def _usd(usd: float) -> str:
    if usd >= 1e6:
        return f"{usd / 1e6:.2f}".replace(".", ",") + " milyon dolarlık"
    return f"{usd / 1e3:.0f} bin dolarlık"


# --- Soyut Sınıf ---

# Girişlerin hepsi varsayılan olarak hiçbir şey yapmaz, böylece bir gözcü
# yalnızca kendi işine yarayan girişi yazar. Bekleme süresi burada tutulur,
# alt sınıflar mesajı doğrudan döndürmek yerine _emit() üzerinden geçirir.
class Watcher(ABC):
    """Bir profil için piyasayı izleyen gözcü.

    Args:
        symbols: Gözcünün izlediği pariteler, profilin pariteleri.
        cooldown_s: Aynı gözcünün iki çağrısı arasında geçmesi gereken en kısa süre, saniye.
    """

    # Günlükteki ve arayüzdeki çağrılma nedeninin kimliği. Alt sınıf verir.
    kind = ""

    def __init__(self, symbols: list[str], cooldown_s: int = 300) -> None:
        self.symbols = set(symbols)
        self.cooldown_s = max(int(cooldown_s), _MIN_COOLDOWN_S)
        self._last_emit = 0.0

    def on_liquidation(self, event: dict) -> str | None:
        """Akıştan gelen bir likidasyonu alır.

        Args:
            event: time, symbol, side, price, qty ve usd anahtarlı kayıt.
        """
        return None

    def on_candle(self, candle: dict) -> str | None:
        """Bir canlı mumu alır. Mum akıştaki biçimdedir, s anahtarı paritedir."""
        return None

    def tick(self, now: float) -> str | None:
        """Geçen zamanı alır. Engine saniyede bir çağırır."""
        return None

    # Bekleme süresi dolmadan gelen olay atılır, sıraya alınmaz. Eski bir
    # olay sonradan bildirilirse Opus artık geçerli olmayan bir duruma göre
    # karar verebilirdi.
    def _emit(self, now: float, message: str) -> str | None:
        if now - self._last_emit < self.cooldown_s:
            return None
        self._last_emit = now
        return message


# --- Gözcüler ---

# Dalga, liquidations.waves ile aynı tanımı kullanır: aralarında quiet_s'den
# uzun boşluk olmayan kayıtlar aynı dalgadır. Dalga, son kaydından quiet_s
# geçince bitmiş sayılır. Bitiş hem tick'te hem de yeni bir kayıt geldiğinde
# kontrol edilir, böylece tick gecikse bile iki dalga birbirine karışmaz.
#
# Fiyat aralığı likidasyon fiyatlarından değil, dalganın başladığı dakikadan
# bitişine kadarki mumların en düşük ve en yüksek fiyatından ölçülür. 18.11
# testinde aralık likidasyon fiyatlarından ölçülünce dalgadan sonraki dönüş
# kuralı kârını kaybetti, mumlardan ölçülünce korudu. Mum henüz gelmediyse,
# örneğin program yeni açıldıysa, likidasyon fiyatları kullanılır.
class LiquidationWave(Watcher):
    """Bir likidasyon dalgası bitince, baskın taraf min_usd'yi geçtiyse Opus'u çağırır.

    Args:
        symbols: İzlenen pariteler.
        min_usd: Baskın tarafın, yani long ya da short likidasyonlarından büyük olanının en az tutarı, dolar.
            Tek bir sayı bütün paritelerde geçerlidir. Parite adından tutara bir sözlük her paritenin eşiğini ayrı verir.
        quiet_s: Dalganın bittiği sayılması için son likidasyondan sonra geçmesi gereken süre, saniye.
        cooldown_s: Bu gözcünün iki çağrısı arasındaki en kısa süre, saniye.
    """

    kind = "liquidation_wave"

    def __init__(self, symbols: list[str], min_usd: float | dict[str, float] = 300_000, quiet_s: int = 120,
                 cooldown_s: int = 300) -> None:
        super().__init__(symbols, cooldown_s)
        if isinstance(min_usd, dict):
            missing = self.symbols - set(min_usd)
            if missing:
                raise TypeError(f"min_usd şu paritelerin eşiğini vermiyor: {', '.join(sorted(missing))}")
            self.min_usd = {s: float(v) for s, v in min_usd.items()}
        else:
            self.min_usd = float(min_usd)
        self.quiet_s = int(quiet_s)
        self._waves: dict[str, dict] = {}
        self._candles: dict[str, dict] = {}

    def on_candle(self, candle: dict) -> str | None:
        s = candle["s"]
        self._candles[s] = candle
        w = self._waves.get(s)
        if w is not None and candle["t"] >= w["start"] // 60 * 60:
            w["low"], w["high"] = min(w["low"], candle["l"]), max(w["high"], candle["h"])
        return None

    def on_liquidation(self, event: dict) -> str | None:
        s = event["symbol"]
        if s not in self.symbols:
            return None
        msg = self._close_if_quiet(s, event["time"])
        w = self._waves.get(s)
        if w is None:
            w = self._waves[s] = {"start": event["time"], "end": event["time"], "long": 0.0, "short": 0.0,
                                  "low": event["price"], "high": event["price"]}
            c = self._candles.get(s)
            if c and c["t"] == event["time"] // 60 * 60:
                w["low"], w["high"] = min(w["low"], c["l"]), max(w["high"], c["h"])
        w["end"] = event["time"]
        w[event["side"]] += event["usd"]
        w["low"], w["high"] = min(w["low"], event["price"]), max(w["high"], event["price"])
        return msg

    # Aynı saniyede birden fazla parite biterse eşiğine göre en güçlü dalga
    # bildirilir. Bekleme süresi bütün paritelerde ortaktır, diğerleri atılır.
    def tick(self, now: float) -> str | None:
        done = [d for s in list(self._waves) if (d := self._finish(s, now))]
        if not done:
            return None
        return self._emit(now, max(done)[1])

    def _close_if_quiet(self, s: str, now: float) -> str | None:
        d = self._finish(s, now)
        return self._emit(now, d[1]) if d else None

    def _threshold(self, s: str) -> float:
        return self.min_usd[s] if isinstance(self.min_usd, dict) else self.min_usd

    # Biten ve eşiği geçen dalganın gücünü, yani baskın tutarın eşiğe oranını,
    # ve mesajını döndürür. Dalga bitmediyse ya da eşiğin altındaysa None.
    def _finish(self, s: str, now: float) -> tuple[float, str] | None:
        w = self._waves.get(s)
        if w is None or now - w["end"] <= self.quiet_s:
            return None
        del self._waves[s]
        side = "long" if w["long"] >= w["short"] else "short"
        if w[side] < self._threshold(s):
            return None
        other = "short" if side == "long" else "long"
        return w[side] / self._threshold(s), (
            f"{s[:-4]}/USDT'de bir likidasyon dalgası bitti. {_clock(w['start'])}-{_clock(w['end'])} arasında "
            f"{_usd(w[side])} {side} ve {_usd(w[other])} {other} "
            f"likidasyonu oldu. Dalganın başladığı dakikadan bu yana fiyat en düşük {_price(w['low'])}, "
            f"en yüksek {_price(w['high'])} oldu. "
            f"Son likidasyondan bu yana {int(now - w['end']) // 60} dakika geçti. Binance akışı saniyede yalnızca "
            f"en büyük likidasyonu bildirdiği için tutarlar gerçek toplamın altındadır.")


# --- Katalog ---

# Anahtar profil dosyasındaki type değeridir.
CATALOG: dict[str, type[Watcher]] = {
    "liquidation_wave": LiquidationWave,
}


# Tanımdaki bilinmeyen bir tür ya da ayar ValueError verir. Engine bu hatayı
# açılışta görür, program yanlış yazılmış bir gözcüyle sessizce açılmaz.
def build(spec: dict[str, Any], symbols: list[str]) -> Watcher:
    """Bir gözcü tanımından gözcüyü kurar.

    Args:
        spec: type anahtarı CATALOG'daki bir tür, diğer anahtarları o türün ayarları olan sözlük.
        symbols: Gözcünün izleyeceği pariteler.

    Returns:
        Kurulmuş gözcü.

    Raises:
        ValueError: Tür katalogda yoksa ya da ayarlardan biri o türde yoksa.
    """
    params = dict(spec)
    kind = params.pop("type", None)
    if kind not in CATALOG:
        raise ValueError(f"bilinmeyen gözcü türü: {kind}")
    try:
        return CATALOG[kind](symbols, **params)
    except TypeError as e:
        raise ValueError(f"{kind} gözcüsünün ayarları hatalı: {e}") from None
