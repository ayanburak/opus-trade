"""
file: texts.py
importable: TZ, MONTHS, DAYS, price, clock, date_text, usd, levels_text, event_text, fire_text

Opus'a giden Türkçe metinlerin ortak yardımcıları. price() bir fiyatı
paritenin büyüklüğüne göre yeterli basamakla yazar. clock() ve date_text()
anları Türkiye saatiyle yazar. event_text() bir hesap olayını, fire_text()
bir tetikleyicinin bildirimini Opus'un okuyacağı cümleye çevirir.

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

from ...domain.clock import HOME_TZ
from ...domain.trading import AccountEvent, EventKind
from ...triggers.base import Fire

TZ = HOME_TZ
TZ_LABEL = "Türkiye saati, UTC+3"
MONTHS = ("Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım",
          "Aralık")
DAYS = ("Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar")

# Kapanış nedenlerinin Opus'a görünen karşılıkları.
REASONS = {"tp": "TP ile kapandı", "sl": "SL ile kapandı", "liquidation": "likide oldu",
           "strategy": "senin kararınla kapandı", "admin": "yönetici tarafından kapatıldı",
           "system": "sistem tarafından kapatıldı"}


def price(x: float | None) -> str:
    if x is None:
        return "yok"
    return f"{x:.1f}" if x >= 1000 else f"{x:.2f}" if x >= 10 else f"{x:.4f}"


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


def levels_text(levels) -> str:
    if not levels:
        return "yok"
    return ", ".join(f"{price(k.price)} yüzde {k.pct:g}" for k in levels)


def event_text(e: AccountEvent) -> str:
    """Broker'ın kendi kuralıyla olan bir olayı Opus'a anlatır."""
    head = f"#{e.number} {e.symbol} {e.side}"
    match e.kind:
        case EventKind.CLOSED:
            t = e.trade
            why = "fonlama ödemesiyle marjini tükendiği için likide oldu" if e.reason == "funding_liquidation" \
                else REASONS.get(e.reason or "", e.reason)
            return f"{head} pozisyonundan {t.qty:g} birim {price(e.price)} seviyesinde {why}, net {t.net:+.2f} dolar."
        case EventKind.ORDER_FILLED:
            return f"{head} emri {price(e.price)} fiyatından gerçekleşti, pozisyon #{e.number} numarasıyla açıldı."
        case EventKind.ORDER_FAILED:
            return f"{head} emri tetiklendi ama açılamadı: {e.reason}"
        case EventKind.ORDER_EXPIRED:
            return f"{head} emrinin geçerlilik süresi doldu, emir iptal oldu."
        case EventKind.FUNDING:
            what = "ödedi" if (e.amount or 0) > 0 else "aldı"
            return f"{head} pozisyonu fonlama {what}: {abs(e.amount or 0):.4f} dolar."
    return f"{head}: {e.kind}."


def fire_text(f: Fire, now: int) -> str:
    """Bir tetikleyicinin bildirimini Opus'a anlatır."""
    if f.kind == "liquidation_wave":
        w = f.data
        side = w.dominant
        other = "short" if side == "long" else "long"
        big, small = (w.long_usd, w.short_usd) if side == "long" else (w.short_usd, w.long_usd)
        return (f"{f.symbol}'de bir likidasyon dalgası bitti. {clock(w.start)[-5:]}-{clock(w.end)[-5:]} arasında "
                f"{usd(big)} {side} ve {usd(small)} {other} likidasyonu oldu. Dalganın başladığı dakikadan bu yana "
                f"fiyat en düşük {price(w.low)}, en yüksek {price(w.high)} oldu. Son likidasyondan bu yana "
                f"{(now - w.end) // 60} dakika geçti. Binance akışı saniyede yalnızca en büyük likidasyonu "
                f"bildirdiği için tutarlar gerçek toplamın altındadır.")
    if f.kind == "alert":
        a = f.data
        who = "Kurduğun" if a.actor == "strategy" else "Yöneticinin kurduğu"
        return f"{who} #{a.number} {a.symbol} {price(a.price)} alarmı tetiklendi. Alarm notu: {a.note or 'yok'}"
    return f"{f.symbol} için {f.kind} tetiklendi."
