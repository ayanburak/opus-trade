"""
file: settings.py
importable: bütün sabitler, Profile, PROFILES

Programın ortak ayarları, parite kataloğu ve profiller burada durur.
Profiller TOML dosyalarından açılışta bir kez okunur, PROFILES kimlikten
Profile nesnesine bir sözlüktür. Klasör private/profiles/ varsa odur, yoksa
örnek profilin durduğu profiles/. Yeni bir profil için klasöre bir dosya
eklenir ve program yeniden başlatılır.

description: Parite kataloğu, komisyon, kayma, zamanlama, Opus çağrı ayarları ve profillerin yüklenmesi.
"""

# NOTE: Opus'a görünen metinler bu dosyada da Türkçe kalır. SYMBOLS içindeki
# note alanı ve TIMEZONE_LABEL doğrudan Opus'un talimatına yazılır, profil
# dosyalarındaki style metni de öyle. Bunlar ya da bu dosyadaki ortak bir
# kural değişirse Opus'a giden talimat değişir ve o profillerin dönemi sıfırdan
# başlatılmak zorunda kalır.
#
# NOTE: Komisyon, kayma, adım sınırları ve model bütün profillerde ortaktır.
# Profile özel olanlar yalnızca Profile alanlarıdır. Ortak bir değer bir
# profilde farklı olacaksa önce Profile'a alan olarak eklenir.

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

# --- Yollar ---

ROOT = Path(__file__).resolve().parent.parent
# Gerçek profiller kendi deposundaki private/ klasöründe durur. Klasör
# yoksa, örneğin yalnızca public depo indirildiyse, program örnek profille açılır.
PROFILES_DIR = next(d for d in (ROOT / "private" / "profiles", ROOT / "profiles") if d.is_dir())
# Her profilin verisi bu klasörün altında kendi adıyla bir klasörde durur.
# Testlerde bütün veri başka bir klasöre TRADE_DATA ortam değişkeniyle
# yönlendirilir, böylece asıl kayıtlara dokunulmaz.
DATA_DIR = Path(os.environ.get("TRADE_DATA", ROOT / "data"))
WEB_DIR = ROOT / "web"
# Kapatma anahtarı her açılışta buraya yazılır, program kapanınca silinir.
TOKEN_PATH = DATA_DIR / "shutdown.token"
# Bütün profillerin ortak kullandığı piyasa kayıtları, şimdilik likidasyonlar.
MARKET_DB = DATA_DIR / "market.db"

# --- Parite kataloğu ---

# Opus yalnızca bu paritelerde işlem yapabilir. Diğer paritelerin verisine
# piyasa araçlarıyla bakabilir. En küçük büyüklük ve miktar adımı Binance'in
# kendi kurallarıdır. Bakım marjı Binance'in en düşük kademesine yakındır.
# Kayma, piyasa emri, SL ve iz süren stopta fiyatın aleyhe kayma oranıdır.
# Bütün paritelerde aynıdır. Bu hesabın işlem büyüklüklerinde gerçek kayma
# her paritede en fazla bir fiyat adımı kadardır. Paritelere farklı kayma
# vermek BTC'ye gerçekte olmayan bir maliyet avantajı sağlıyordu.
SYMBOLS = {
    "BTCUSDT": {"name": "BTC/USDT", "icon": "btc", "max_leverage": 100, "maint_margin": 0.004, "slippage": 0.0002,
                "min_notional": 50, "qty_step": 0.001},
    "ETHUSDT": {"name": "ETH/USDT", "icon": "eth", "max_leverage": 100, "maint_margin": 0.004, "slippage": 0.0002,
                "min_notional": 20, "qty_step": 0.001},
    "SOLUSDT": {"name": "SOL/USDT", "icon": "sol", "max_leverage": 50, "maint_margin": 0.01, "slippage": 0.0002,
                "min_notional": 5, "qty_step": 0.01},
    "XRPUSDT": {"name": "XRP/USDT", "icon": "xrp", "max_leverage": 50, "maint_margin": 0.01, "slippage": 0.0002,
                "min_notional": 5, "qty_step": 0.1},
    "XAUUSDT": {"name": "XAU/USDT", "icon": "xau", "max_leverage": 50, "maint_margin": 0.01, "slippage": 0.0002,
                "min_notional": 5, "qty_step": 0.001, "note": "altın"},
    "ZECUSDT": {"name": "ZEC/USDT", "icon": "zec", "max_leverage": 50, "maint_margin": 0.01, "slippage": 0.0002,
                "min_notional": 5, "qty_step": 0.001},
}

# --- Piyasa ---

BINANCE_REST = "https://fapi.binance.com"

# Eski /ws/ yolu bağlantıyı kabul ediyor ama hiç veri göndermiyor. Binance
# piyasa akışlarını /market/ yolu altına taşıdı. Bütün profillerin bütün
# pariteleri tek bir birleşik akıştan gelir, akışın adresi pariteler eklenerek
# market_data.live_stream içinde kurulur.
BINANCE_WS = "wss://fstream.binance.com/market/stream?streams="

# Akıştan bu kadar saniye hiç mesaj gelmezse bağlantı ölü sayılır ve
# yeniden kurulur. Normalde birkaç yüz milisaniyede bir mesaj gelir.
STREAM_SILENCE_S = 30

# --- Dönem ---

# Özkaynak bunun altına düşer ve açık pozisyon kalmazsa dönem biter.
# Bu kadar küçük bir bakiyeyle en küçük işlem büyüklüğü bile açılamaz.
END_EQUITY = 1.0

TIMEZONE_LABEL = "Türkiye saati, UTC+3"

# Program saatleri sürecin yerel saatiyle yazar ve servis TZ ile bu dilimde
# çalışır. Borsa saatleri gibi başka dilimlerden çevrilen saatler ise
# sunucunun ayarına güvenmeden doğrudan bu dilime çevrilir.
TIMEZONE = "Europe/Istanbul"

# Opus'a giden tarihlerde kullanılan Türkçe ay ve gün adları. Gün sırası
# Python'un haftasıyla aynıdır, pazartesi sıfırdır.
MONTHS = ("Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim",
          "Kasım", "Aralık")
DAYS = ("Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar")

# --- Komisyon ---

# Binance vadelinin standart oranları. Fiyatın gelmesini bekleyen emirler,
# yani limit niteliğindeki girişler ve TP kademeleri maker öder. Anında
# gerçekleşen her şey, yani piyasa emri, kırılım emri, SL ve kapatmalar taker öder.
FEE_TAKER = 0.0005
FEE_MAKER = 0.0002

# --- Opus ---

MODEL = "opus"
EFFORT = "high"

# Opus'a bir çağrıda kullanabileceği araç adımı sayısı bildirilir. Sert
# sınır bunun biraz üstündedir, böylece Opus sınıra yaklaşınca kararını
# yazmaya vakit bulur.
TOOL_STEP_LIMIT = 25
HARD_STEP_LIMIT = 30

RESEARCHER_MODEL = "haiku"

# Opus cevap vermezse veya cevap okunamazsa bu kadar dakika sonra tekrar denenir.
ERROR_RETRY_MIN = 10

# Tek bir çağrı bundan uzun sürerse süreç öldürülür.
CALL_TIMEOUT_S = 600

# Opus'a istemde gösterilen önceki kararların sayısı. Daha eskilerine
# piyasa aracından kendisi bakabilir.
PAST_DECISIONS = 8

# Her çağrıda her parite için istemin sonuna eklenen ham mum sayısı. Bir
# aralık listede yoksa ya da sıfırsa o aralığın mumları gönderilmez,
# göstergeleri yine gönderilir. 16.11'deki testte ham mumsuz istem aynı
# kararları yarı maliyetle verdi, Opus ihtiyaç duyduğu mumları araçla
# kendisi çekti. Bu yüzden boş bırakıldı. Eski değer
# {"15m": 12, "1h": 24, "4h": 18, "1d": 14} idi.
PROMPT_CANDLES: dict[str, int] = {}

# --- Web ---

# Bütün profiller tek programda ve tek porttan sunulur. TRADE_SOCKET verilirse
# program port yerine bu Unix soket dosyasını dinler. Sunucuda böyle çalışır,
# çünkü servisin yerel ağ adreslerine erişimi tamamen kapatılır ve nginx
# programa ancak soket üzerinden ulaşabilir.
WEB_HOST = "127.0.0.1"
WEB_PORT = 8765
WEB_SOCKET = os.environ.get("TRADE_SOCKET")

# --- Profiller ---


@dataclass(frozen=True)
class Profile:
    """Bir hesabın profil dosyasından okunan değerleri.

    id dosyanın adıdır ve verinin klasör adı olarak da kullanılır. symbols,
    katalogdan yalnızca bu profilin işlem yapabildiği paritelerdir. style boşsa
    talimata tarz bölümü eklenmez. effort profil dosyasında yazmıyorsa
    bütün profillerin ortak EFFORT değeridir.
    """

    id: str
    name: str
    short: str
    balance: float
    symbols: dict
    period_s: int
    min_wait: int
    max_wait: int
    style: str
    effort: str = EFFORT

    # claude komutu da bu klasörde çalışır. Haiku araştırmacısının bıraktığı
    # iz klasörü bu yolun adını taşır, profiller ayrı klasörde çalıştığı için
    # biri ötekinin çağrısı sürerken onun izini silmez.
    @property
    def work_dir(self) -> Path:
        return DATA_DIR / self.id

    @property
    def db_path(self) -> Path:
        return self.work_dir / "account.db"


# Bir alan eksikse ya da katalogda olmayan bir parite yazılmışsa program
# açılmadan durur ve hangi dosyada ne olduğunu söyler.
def _load(path: Path) -> Profile:
    d = tomllib.loads(path.read_text())
    try:
        unknown = [s for s in d["symbols"] if s not in SYMBOLS]
        if unknown:
            raise SystemExit(f"{path.name}: katalogda olmayan parite: {', '.join(unknown)}")
        return Profile(
            id=path.stem, name=d["name"], short=d["short"], balance=float(d["balance"]),
            symbols={s: SYMBOLS[s] for s in d["symbols"]} if d["symbols"] else dict(SYMBOLS),
            period_s=int(d["period_hours"] * 3600), min_wait=d["min_wait"], max_wait=d["max_wait"], style=d["style"],
            effort=d.get("effort", EFFORT),
        )
    except KeyError as e:
        raise SystemExit(f"{path.name}: {e} alanı eksik.")


PROFILES = {p.id: p for p in map(_load, sorted(PROFILES_DIR.glob("*.toml")))}
