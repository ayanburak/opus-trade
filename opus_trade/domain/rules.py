"""
file: rules.py
importable: TOLERANCE, slipped, round_qty, min_notional, liq_price, unrealized_pnl, funding_payment,
            order_is_maker, check_leverage, check_sl, check_trail, trail_level, trailed_sl,
            check_tp_levels, split_tp, size

Binance USDⓈ-M vadelinin izole marjinli hesap kuralları, saf fonksiyonlar
olarak. Hiçbiri durum tutmaz, veritabanına ya da ağa dokunmaz. Kâğıt broker
bu kurallarla hesabı simüle eder, canlı broker ise emri Binance'e göndermeden
önce aynı kurallarla kontrol eder. Kurala aykırı bir girdi RuleError
fırlatır.

description: Kaldıraç, büyüklük, kayma, likidasyon, TP kademesi ve iz süren stop kuralları.
"""

# NOTE: Kayma aleyhe işler. Alışta fiyat yükselir, satışta düşer. Long giriş ve
# short çıkış alıştır, short giriş ve long çıkış satıştır. Maker sayılan
# işlemler kaymasız ve tam seviyesinden gerçekleşir.
#
# NOTE: Fonksiyonlar fiyatı ve miktarı float olarak alır. Kuruşun altındaki
# kayan nokta farkları geçerli bir işlemin reddedilmesine yol açmasın diye
# karşılaştırmalarda TOLERANCE payı kullanılır.

import math

from .errors import RuleError
from .models import Side, SymbolSpec, TpLevel, Sizing

TOLERANCE = 1e-9

# Nakit bir işleme bu orandan daha az eksik kalıyorsa işlem reddedilmez, miktar
# nakde sığacak kadar küçültülür. Bakiyenin tamamıyla işlem açmak isteyen biri
# genelde yuvarlak bir marjin verir ve komisyon yüzünden birkaç sent eksik
# kalır. Bunu reddetmek anlamsız olurdu.
_CASH_SLACK = 0.02

# İz süren stop en iyi fiyattan en fazla bu kadar yüzde uzakta olabilir.
_MAX_TRAIL_PCT = 50


# --- Fiyat ve miktar ---

def slipped(spec: SymbolSpec, price: float, buy: bool) -> float:
    return price * (1 + spec.slippage) if buy else price * (1 - spec.slippage)


# Miktar adımın katına aşağı yuvarlanır, Binance adımın katı olmayan miktarı
# kabul etmez. Küçük pay, 0.03 gibi bir değerin kayan noktada 0.029999 olup
# bir adım eksik yuvarlanmasını önler. Sondaki round aynı nedenle oluşan
# 0.30000000000000004 gibi kuyrukları siler.
def round_qty(spec: SymbolSpec, qty: float) -> float:
    return round(math.floor(qty / spec.qty_step + 1e-6) * spec.qty_step, 10)


# En küçük büyüklük, Binance'in dolar sınırı ile tek bir miktar adımının
# değerinden büyük olanıdır. BTC'de 0.001 BTC çoğu zaman elli dolardan fazla
# ettiği için belirleyici olan adımdır.
def min_notional(spec: SymbolSpec, price: float) -> float:
    step = spec.qty_step
    return max(math.ceil(spec.min_notional / price / step - 1e-9) * step, step) * price


# Likidasyon, marjin ile açık zararın toplamının bakım marjına düştüğü
# fiyattır. Formül marjin üzerinden kurulduğu için ekleme, kısmi kapanış ve
# fonlamayla marjin değiştiğinde de aynı formül doğru sonucu verir.
def liq_price(spec: SymbolSpec, side: Side, entry: float, qty: float, margin: float) -> float:
    mm = spec.maint_margin
    if side is Side.LONG:
        return max(0.0, (entry * qty - margin) / (qty * (1 - mm)))
    return (entry * qty + margin) / (qty * (1 + mm))


def unrealized_pnl(side: Side, entry: float, qty: float, price: float) -> float:
    return (price - entry) * qty * side.sign


# Oran pozitifse long öder short alır, negatifse tersi. Pozitif sonuç
# pozisyonun ödediği, negatif sonuç aldığı tutardır. Ödeme o anki mark
# fiyatıyla pozisyonun büyüklüğü üzerinden hesaplanır.
def funding_payment(side: Side, qty: float, mark: float, rate: float) -> float:
    return qty * mark * rate * side.sign


# Fiyatın lehine geldiği emir, yani long için aşağıdaki, short için yukarıdaki
# tetik bir limit emri gibi maker sayılır. Ters yöndeki kırılım emri stop emri
# gibi taker sayılır ve kayma yer.
def order_is_maker(side: Side, trigger: float, price: float) -> bool:
    return (side is Side.LONG) == (trigger < price)


# --- Doğrulama ---

def check_leverage(spec: SymbolSpec, leverage: float) -> None:
    if not 1 <= leverage <= spec.max_leverage:
        raise RuleError(f"{spec.symbol} için kaldıraç 1 ile {spec.max_leverage} arasında olmalı, {leverage:g} verildi.")


# ref bir pozisyonda güncel fiyat ya da giriş fiyatı, bir emirde tetik
# fiyatıdır. SL'nin ref'in zarar tarafında olması gerekir.
def check_sl(side: Side, ref: float, sl: float | None) -> None:
    if sl is None:
        return
    if side is Side.LONG and sl >= ref:
        raise RuleError(f"Long için SL {sl:g}, referans fiyat {ref:g} altında olmalı.")
    if side is Side.SHORT and sl <= ref:
        raise RuleError(f"Short için SL {sl:g}, referans fiyat {ref:g} üstünde olmalı.")


def check_trail(pct: float | None) -> None:
    if pct is not None and not 0 < pct <= _MAX_TRAIL_PCT:
        raise RuleError(f"İz süren stop mesafesi sıfırdan büyük ve en fazla yüzde {_MAX_TRAIL_PCT} olmalı.")


# --- İz süren stop ---

# best pozisyonun açılıştan beri gördüğü en iyi fiyattır: long'da en yüksek,
# short'ta en düşük.
def trail_level(side: Side, best: float, pct: float) -> float:
    return best * (1 - pct / 100) if side is Side.LONG else best * (1 + pct / 100)


# İz sonradan eklenince en iyi fiyat sıfırlanmaz. Fiyat o en iyi noktadan
# verilen yüzdeden fazla geri döndüyse stop daha kurulurken fiyatın gerisinde
# kalır ve pozisyon hemen kapanırdı. Bu istek reddedilir, mesaj verilebilecek
# en küçük yüzdeyi söyler.
def trailed_sl(side: Side, best: float, pct: float, sl: float | None, price: float) -> float:
    """İz eklenen bir pozisyonun yeni SL'sini döndürür.

    Args:
        side: Pozisyonun yönü.
        best: Pozisyonun açılıştan beri gördüğü en iyi fiyat.
        pct: İz süren stopun en iyi fiyattan yüzde uzaklığı.
        sl: Pozisyonun var olan SL'si, yoksa None.
        price: Güncel fiyat.

    Returns:
        İzin seviyesi ile var olan SL'den fiyata yakın olanı.

    Raises:
        RuleError: İzin seviyesi güncel fiyatı zaten geçmişse.
    """
    level = trail_level(side, best, pct)
    if (level >= price) if side is Side.LONG else (level <= price):
        least = (1 - price / best) * 100 if side is Side.LONG else (price / best - 1) * 100
        raise RuleError(
            f"İz süren stop yüzde {pct:g} verildi. Pozisyonun gördüğü en iyi fiyat {best:g}, stop {level:g} "
            f"seviyesine düşüyor ve güncel fiyat {price:g} bunu zaten geçmiş. "
            f"Bu pozisyonda iz yüzde {least:.2f}'den büyük olmalı.")
    if sl is None or (level > sl if side is Side.LONG else level < sl):
        return level
    return sl


# --- TP kademeleri ---

# Kademeler fiyata en yakından en uzağa sıralanır. Fiyat ilerledikçe kademeler
# bu sırayla gerçekleşir, eşleştirici de listenin başına bakar.
def check_tp_levels(side: Side, ref: float, levels: list[TpLevel]) -> list[TpLevel]:
    """Kademeleri doğrular ve gerçekleşecekleri sırayla döndürür.

    Args:
        side: Pozisyonun ya da emrin yönü.
        ref: Kademelerin kâr tarafında olması gereken fiyat, güncel ya da tetik fiyatı.
        levels: Fiyat ve yüzde taşıyan kademeler.

    Returns:
        Fiyata en yakın kademe başta olacak şekilde sıralanmış kademeler.

    Raises:
        RuleError: Bir kademenin fiyatı ya da yüzdesi geçersizse ya da yüzdelerin toplamı yüzü geçiyorsa.
    """
    total = 0.0
    for k in levels:
        if k.price <= 0 or k.pct <= 0:
            raise RuleError("Her TP kademesinin fiyatı ve yüzdesi sıfırdan büyük olmalı.")
        if (side is Side.LONG and k.price <= ref) or (side is Side.SHORT and k.price >= ref):
            where = "üstünde" if side is Side.LONG else "altında"
            raise RuleError(f"{side} için TP {k.price:g}, referans fiyat {ref:g} {where} olmalı.")
        total += k.pct
    if total > 100 + TOLERANCE:
        raise RuleError(f"TP kademelerinin yüzdeleri toplamı en fazla 100 olabilir, {total:g} verildi.")
    return sorted(levels, key=lambda k: k.price, reverse=side is Side.SHORT)


# Yüzdelerin toplamı yüzse son kademe kalanın tamamıdır ve qty None kalır.
# Böylece kademelerin yuvarlanmış miktarları toplamı pozisyondan bir adım az
# çıksa bile pozisyon son kademede tamamen kapanır.
def split_tp(spec: SymbolSpec, levels: list[TpLevel], qty: float) -> list[TpLevel]:
    """Yüzdeli kademeleri pozisyonun miktarına göre mutlak miktara çevirir.

    Raises:
        RuleError: Bir kademenin miktarı tek bir miktar adımından küçük kalıyorsa.
    """
    total = sum(k.pct for k in levels)
    out = []
    for i, k in enumerate(levels):
        if i == len(levels) - 1 and total >= 100 - TOLERANCE:
            out.append(TpLevel(k.price, k.pct, None))
            continue
        q = round_qty(spec, qty * k.pct / 100)
        if q == 0:
            raise RuleError(f"TP kademesi {k.price:g} için yüzde {k.pct:g}, en küçük miktar adımının altında kalıyor. "
                            f"Kademeyi büyüt ya da kademe sayısını azalt.")
        out.append(TpLevel(k.price, k.pct, q))
    return out


# --- Büyüklük ---

# Miktar marjin ile kaldıraçtan çıkarılır ve adıma yuvarlanır. Gerçek marjin
# yuvarlanmış miktardan yeniden hesaplanır, bu yüzden verilen marjinden biraz
# az olabilir. Nakit yetmiyorsa ama fark _CASH_SLACK'ten küçükse miktar nakde
# sığacak kadar küçültülür.
def size(spec: SymbolSpec, leverage: float, margin: float, entry: float, fee_rate: float, cash: float) -> Sizing:
    """Bir girişin miktarını, gerçek marjinini ve komisyonunu hesaplar.

    Args:
        spec: Paritenin kuralları.
        leverage: Kaldıraç. Sınırı ayrıca check_leverage kontrol eder.
        margin: Ayrılmak istenen marjin, dolar.
        entry: Kayma uygulanmış giriş fiyatı.
        fee_rate: Girişin komisyon oranı.
        cash: Hesapta kullanılabilir nakit.

    Returns:
        Yuvarlanmış miktar, ona düşen marjin ve giriş komisyonu.

    Raises:
        RuleError: Marjin sıfır ya da negatifse, nakit yetmiyorsa ya da büyüklük en küçük sınırın altındaysa.
    """
    if margin <= 0:
        raise RuleError("Marjin sıfırdan büyük olmalı.")
    qty = round_qty(spec, margin * leverage / entry)
    needed = lambda q: q * entry / leverage + q * entry * fee_rate
    if needed(qty) > cash + TOLERANCE:
        most = round_qty(spec, cash / (entry / leverage + entry * fee_rate))
        if needed(qty) > cash * (1 + _CASH_SLACK) or most <= 0:
            raise RuleError(f"Yetersiz nakit. Nakit {cash:.2f} dolar, bu işlem için {needed(qty):.2f} dolar gerekiyor.")
        qty = most
    smallest = min_notional(spec, entry)
    if qty * entry < smallest - TOLERANCE:
        raise RuleError(
            f"{spec.symbol} için en küçük işlem büyüklüğü yaklaşık {smallest:.2f} dolar. Bu marjin ve kaldıraçla "
            f"büyüklük {qty * entry:.2f} dolar oluyor. {leverage:g}x kaldıraçla en az "
            f"{smallest / leverage:.2f} dolar marjin gerekir.")
    return Sizing(qty, qty * entry / leverage, qty * entry * fee_rate)
