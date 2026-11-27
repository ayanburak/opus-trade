"""
file: texts.py
importable: TZ, MONTHS, DAYS, INTERVAL_NAMES, ORDER_NAMES, GUARD_NAMES, REASONS, price, clock, date_text, usd, order_name,
            guard_text, event_text, fire_text, invalidation_text

Opus'a giden Türkçe metinlerin ortak yardımcıları. price() bir fiyatı
paritenin büyüklüğüne göre yeterli basamakla, noktalı ondalıkla yazar ve
istemin veri bölümleri içindir. clock() ve date_text()
anları Türkiye saatiyle yazar. event_text() bir hesap olayını, fire_text()
bir tetikleyicinin bildirimini Opus'un okuyacağı cümleye çevirir. Cümle içindeki
sayılar domain.numbers ile virgüllü yazılır.

description: Opus'a giden metinlerin biçim ve cümle yardımcıları.
"""

# NOTE: Saatler sunucunun saat ayarına güvenilmeden doğrudan Türkiye saatine
# çevrilir. Sunucu UTC'dir, yerel saatle yazan kod Opus'a üç saat geri saat
# gösterirdi.
#
# XXX: Bu metinler Opus'a aynen gider. Bir cümle değişirse talimatın şablon
# sürümü değişmez ama Opus'un gördüğü değişir, büyük değişiklikte şablon
# sürümü de artırılır.

from datetime import datetime

from ...domain import numbers
from ...domain.clock import HOME_TZ
from ...domain.trading import AccountEvent, EventKind
from ...triggers.base import Fire

TZ = HOME_TZ
TZ_LABEL = "Türkiye saati, UTC+3"
MONTHS = ("Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım",
          "Aralık")
DAYS = ("Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar")

# Mum aralıklarının Opus'a görünen adları, yapı ve rejim metinlerinde.
INTERVAL_NAMES = {"15m": "15 dakikalık", "1h": "1 saatlik", "4h": "4 saatlik", "1d": "günlük"}

# Kapanış nedenlerinin Opus'a görünen karşılıkları.
REASONS = {"tp": "TP ile kapandı", "sl": "SL ile kapandı", "liquidation": "likide oldu",
           "strategy": "senin kararınla kapandı", "admin": "yönetici tarafından kapatıldı",
           "system": "sistem tarafından kapatıldı"}


def price(x: float | None) -> str:
    if x is None:
        return "yok"
    return numbers.price_text(x)


def local(t: int) -> datetime:
    return datetime.fromtimestamp(t, TZ)


def clock(t: int) -> str:
    return local(t).strftime("%d.%m %H:%M")


def date_text(t: int, weekday_info: bool = False) -> str:
    """Bir anı 15 Kasım 2026 Pazar, 05:00 biçiminde yazar. weekday_info sonuna hafta içi ya da sonu ekler."""
    z = local(t)
    text = f"{z.day} {MONTHS[z.month - 1]} {z.year} {DAYS[z.weekday()]}, {z:%H:%M}"
    if weekday_info:
        text += ". Hafta sonu." if z.weekday() >= 5 else ". Hafta içi."
    return text


# Bir milyonun altı bin dolarla yazılır, yoksa küçük taraf 0,00 milyon görünür.
def usd(x: float) -> str:
    if x >= 1e6:
        return f"{x / 1e6:.2f}".replace(".", ",") + " milyon dolarlık"
    return f"{x / 1e3:.0f} bin dolarlık"


# Giriş emirlerinin Opus'a görünen türü, eylemlerdeki tur alanıyla aynı
# adlar. Kapatan emirler TP, SL ve iz diye anılır. Çalıştırıcının komut
# sonuçları da bu adları kullanır.
ORDER_NAMES = {"market": "piyasa", "limit": "limit", "stop": "kırılım", "take_profit": "geri çekilme"}
GUARD_NAMES = {"take_profit": "TP", "stop": "SL", "trailing_stop": "iz"}


# Bir emrin adı, kapatan emirde TP, SL ya da iz, girişte türüyle giriş emri.
# Türü bilinmeyen eski kayıtlarda yalnızca emir denir.
def order_name(type_, reduce: bool) -> str:
    if type_ is None:
        return "emir"
    return GUARD_NAMES[str(type_)] + " emri" if reduce else ORDER_NAMES[str(type_)] + " giriş emri"


# Bir koruma emri ya da bekleyen girişin koruma isteği. whole miktarsız
# olanın ne kadarını kapattığıdır: emirde kalanın tamamı, istekte girişin
# tamamı.
def guard_text(x, whole: str) -> str:
    num = f"#{x.number} " if getattr(x, "number", None) else ""
    level = f"yüzde {x.trail_pct:g}" if x.type == "trailing_stop" else price(x.price)
    return f"{num}{GUARD_NAMES[str(x.type)]} {level} miktar {whole if x.qty is None else f'{x.qty:g}'}"


# Uyandıran olaylar dolum, kapanış, başarısızlık ve süre dolmasıdır. Dolumla
# birleşen pozisyonun ve kurulan korumaların son hali hesap bölümünde zaten
# yazdığı için cümle oraya gönderir. Kapatan emrin başarısızlığı dolan girişin
# kurulamayan korumasıdır, numarası yoktur.
def event_text(e: AccountEvent) -> str:
    """Broker'ın kendi kuralıyla olan bir olayı Opus'a anlatır."""
    pos = f"{e.symbol} {e.side}"
    match e.kind:
        case EventKind.CLOSED:
            t = e.trade
            why = "fonlama ödemesiyle marjini tükendiği için likide oldu" if e.reason == "funding_liquidation" \
                else REASONS.get(e.reason or "", e.reason)
            by = f", kapatan emir #{e.number} {GUARD_NAMES.get(str(e.order_type), '')}".rstrip() if e.number else ""
            return (f"{pos} pozisyonundan {numbers.qty(t.qty, e.symbol)} {numbers.price(e.price)} seviyesinde {why}{by}, "
                    f"net {numbers.num(t.net, 2, sign=True)} dolar.")
        case EventKind.ORDER_FILLED:
            return (f"#{e.number} {pos} {order_name(e.order_type, False)} {numbers.qty(e.qty, e.symbol)}, "
                    f"{numbers.price(e.price)} fiyatından gerçekleşti. Pozisyonun ve kurulan koruma emirlerinin son "
                    f"hali hesap bölümünde.")
        case EventKind.ORDER_FAILED if not e.reduce:
            return f"#{e.number} {pos} {order_name(e.order_type, False)} tetiklendi ama açılamadı: {e.reason}"
        case EventKind.ORDER_FAILED:
            level = f" {numbers.price(e.price)}" if e.price else ""
            return f"{pos} pozisyonunun{level} {order_name(e.order_type, True)} kurulamadı: {e.reason}"
        case EventKind.ORDER_EXPIRED:
            return (f"#{e.number} {pos} {order_name(e.order_type, False)} geçerlilik süresi dolduğu için iptal "
                    f"oldu.")
        case EventKind.FUNDING:
            what = "ödedi" if (e.amount or 0) > 0 else "aldı"
            return f"{pos} pozisyonu fonlama {what}: {numbers.num(abs(e.amount or 0), 4)} dolar."
    return f"{pos}: {e.kind}."


def fire_text(f: Fire, now: int) -> str:
    """Bir tetikleyicinin bildirimini Opus'a anlatır."""
    if f.kind == "liquidation_wave":
        w = f.data
        side = w.dominant
        other = "short" if side == "long" else "long"
        big, small = (w.long_usd, w.short_usd) if side == "long" else (w.short_usd, w.long_usd)
        return (f"{f.symbol}'de bir likidasyon dalgası bitti. {clock(w.start)[-5:]}-{clock(w.end)[-5:]} arasında "
                f"{usd(big)} {side} ve {usd(small)} {other} likidasyonu oldu. Dalganın başladığı dakikadan bu yana "
                f"fiyat en düşük {numbers.price(w.low)}, en yüksek {numbers.price(w.high)} oldu. Son likidasyondan bu yana "
                f"{(now - w.end) // 60} dakika geçti. Binance akışı saniyede yalnızca en büyük likidasyonu "
                f"bildirdiği için tutarlar gerçek toplamın altındadır.")
    if f.kind == "alert":
        a = f.data
        who = "Kurduğun" if a.actor == "strategy" else "Yöneticinin kurduğu"
        return f"{who} #{a.number} {a.symbol} {numbers.price(a.price)} alarmı tetiklendi. Alarm notu: {a.note or 'yok'}"
    return f"{f.symbol} için {f.kind} tetiklendi."


def invalidation_text(symbol: str, side: str, level: float, text: str) -> str:
    """Açık pozisyonun geçersizlik seviyesine son fiyatın değdiğini Opus'a anlatır."""
    return (f"{symbol} {side} pozisyonunun geçersizlik fiyatına, {numbers.price(level)}, son fiyat değdi. Girişte "
            f"tezini bozan koşul olarak şunu yazmıştın: {text}")
