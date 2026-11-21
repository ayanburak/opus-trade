"""
file: rules.py
importable: TOLERANCE, slipped, round_qty, to_tick, min_notional, bracket, liq_price, unrealized_pnl,
            funding_payment, order_is_maker, check_leverage, check_ticks, check_sl, check_trail, trail_level,
            trailed_sl,
            check_tp_levels, split_tp, tp_shares, size, close_fill

Binance USDⓈ-M vadelinin izole marjinli hesap kuralları, saf fonksiyonlar
olarak. Hiçbiri durum tutmaz, veritabanına ya da ağa dokunmaz. Kâğıt broker
bu kurallarla hesabı simüle eder, canlı broker ise emri Binance'e göndermeden
önce aynı kurallarla kontrol eder. Kurala aykırı bir girdi RuleError
fırlatır.

description: Kaldıraç, fiyat adımı, büyüklük, kayma, likidasyon, TP kademesi ve iz süren stop kuralları.
"""

# NOTE: Kayma aleyhe işler. Alışta fiyat yükselir, satışta düşer. Long giriş ve
# short çıkış alıştır, short giriş ve long çıkış satıştır. Maker sayılan
# işlemler kaymasız ve tam seviyesinden gerçekleşir.
#
# NOTE: Fonksiyonlar fiyatı ve miktarı float olarak alır. Kuruşun altındaki
# kayan nokta farkları geçerli bir işlemin reddedilmesine yol açmasın diye
# karşılaştırmalarda TOLERANCE payı kullanılır.

import math

from . import numbers
from .errors import RuleError
from .models import Bracket, Side, SymbolSpec, TpLevel, Sizing

TOLERANCE = 1e-9

# Nakit bir işleme bu orandan daha az eksik kalıyorsa işlem reddedilmez, miktar
# nakde sığacak kadar küçültülür. Bakiyenin tamamıyla işlem açmak isteyen biri
# genelde yuvarlak bir marjin verir ve komisyon yüzünden birkaç sent eksik
# kalır. Bunu reddetmek anlamsız olurdu.
_CASH_SLACK = 0.02

# İz süren stopun en iyi fiyattan uzaklığı Binance'teki gibi bu aralıktadır,
# yüzde.
_MIN_TRAIL_PCT, _MAX_TRAIL_PCT = 0.1, 10


# --- Fiyat ve miktar ---

def slipped(spec: SymbolSpec, price: float, buy: bool) -> float:
    return price * (1 + spec.slippage) if buy else price * (1 - spec.slippage)


# Miktar adımın katına aşağı yuvarlanır, Binance adımın katı olmayan miktarı
# kabul etmez. Küçük pay, 0.03 gibi bir değerin kayan noktada 0.029999 olup
# bir adım eksik yuvarlanmasını önler. Sondaki round aynı nedenle oluşan
# 0.30000000000000004 gibi kuyrukları siler.
def round_qty(spec: SymbolSpec, qty: float) -> float:
    return round(math.floor(qty / spec.qty_step + 1e-6) * spec.qty_step, 10)


# Fiyat adımın katına en yakın yönde yuvarlanır. Kurallar yuvarlamaz, adıma
# uymayan fiyatı reddeder. Bunu kapı kullanır, gelen fiyatı broker'a
# göndermeden önce adıma oturtur.
def to_tick(spec: SymbolSpec, price: float) -> float:
    return round(round(price / spec.tick) * spec.tick, 10)


# En küçük büyüklük, Binance'in dolar sınırı ile tek bir miktar adımının
# değerinden büyük olanıdır. BTC'de 0.001 BTC çoğu zaman elli dolardan fazla
# ettiği için belirleyici olan adımdır.
def min_notional(spec: SymbolSpec, price: float) -> float:
    step = spec.qty_step
    return max(math.ceil(spec.min_notional / price / step - 1e-9) * step, step) * price


# Büyüklüğü kapsayan ilk kademe. Tablonun dışına taşan büyüklük son kademede
# sayılır, kaldıraç kontrolü onu ayrıca reddeder.
def bracket(spec: SymbolSpec, notional: float) -> Bracket:
    return next((b for b in spec.brackets if notional <= b.cap), spec.brackets[-1])


# Likidasyon, marjin ile açık zararın toplamının bakım marjına düştüğü
# fiyattır. Bakım marjı Binance'teki gibi kademelidir: büyüklük × oran − sabit.
# Formül marjin üzerinden kurulduğu için ekleme, kısmi kapanış ve fonlamayla
# marjin değiştiğinde de aynı formül doğru sonucu verir.
#
# Kademe giriş büyüklüğünden seçilir. Binance likidasyon anındaki büyüklüğe
# bakar ve fark yalnızca kademe sınırına çok yakın bir pozisyonda görülür. Bu
# hesabın büyüklükleri hep ilk kademede kalır.
def liq_price(spec: SymbolSpec, side: Side, entry: float, qty: float, margin: float) -> float:
    b = bracket(spec, entry * qty)
    if side is Side.LONG:
        return max(0.0, (entry * qty - margin - b.mm_amount) / (qty * (1 - b.mm_rate)))
    return (entry * qty + margin + b.mm_amount) / (qty * (1 + b.mm_rate))


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

# Binance'te kaldıraç tam sayıdır. Kaldıraç büyüdükçe izin verilen en büyük
# pozisyon küçülür: verilen kaldıracı taşıyan kademelerin en büyüğü sınırdır.
def check_leverage(spec: SymbolSpec, leverage: float, notional: float) -> None:
    top = spec.brackets[0].max_leverage
    if leverage != int(leverage) or not 1 <= leverage <= top:
        raise RuleError(f"{spec.symbol} için kaldıraç 1 ile {top} arasında bir tam sayı olmalı, "
                        f"{numbers.num(leverage)} verildi.")
    cap = max(b.cap for b in spec.brackets if b.max_leverage >= leverage)
    if notional > cap + TOLERANCE:
        raise RuleError(f"{spec.symbol} için {numbers.num(leverage)}x kaldıraçla pozisyon en fazla {numbers.num(cap)} "
                        f"dolar olabilir, bu işlemle {numbers.num(notional, 2)} dolar oluyor.")


# Fiyat, tetik, TP ve SL Binance'te fiyat adımının katı olmalıdır. Sıfır SL
# korumayı kaldırmak demektir, kontrol edilmez.
def check_ticks(spec: SymbolSpec, *prices: float | None) -> None:
    for x in prices:
        if x and abs(x / spec.tick - round(x / spec.tick)) > 1e-6:
            raise RuleError(f"{spec.symbol} fiyatı {numbers.num(x)}, fiyat adımı {numbers.num(spec.tick)} "
                            f"değerinin katı olmalı.")


# ref bir pozisyonda güncel fiyat ya da giriş fiyatı, bir emirde tetik
# fiyatıdır. SL'nin ref'in zarar tarafında olması gerekir.
def check_sl(side: Side, ref: float, sl: float | None) -> None:
    if sl is None:
        return
    if side is Side.LONG and sl >= ref:
        raise RuleError(f"Long için SL {numbers.num(sl)}, referans fiyat {numbers.num(ref)} altında olmalı.")
    if side is Side.SHORT and sl <= ref:
        raise RuleError(f"Short için SL {numbers.num(sl)}, referans fiyat {numbers.num(ref)} üstünde olmalı.")


def check_trail(pct: float | None) -> None:
    if pct is not None and not _MIN_TRAIL_PCT <= pct <= _MAX_TRAIL_PCT:
        raise RuleError(f"İz süren stop mesafesi yüzde {numbers.num(_MIN_TRAIL_PCT)} ile "
                        f"{numbers.num(_MAX_TRAIL_PCT)} arasında olmalı, {numbers.num(pct)} verildi.")


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
            f"İz süren stop yüzde {numbers.num(pct)} verildi. Pozisyonun gördüğü en iyi fiyat {numbers.num(best)}, "
            f"stop {numbers.num(level)} seviyesine düşüyor ve güncel fiyat {numbers.num(price)} bunu zaten geçmiş. "
            f"Bu pozisyonda iz yüzde {numbers.num(least, 2)} değerinden büyük olmalı.")
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
            raise RuleError(f"{side} için TP {numbers.num(k.price)}, referans fiyat {numbers.num(ref)} {where} olmalı.")
        total += k.pct
    if total > 100 + TOLERANCE:
        raise RuleError(f"TP kademelerinin yüzdeleri toplamı en fazla 100 olabilir, {numbers.num(total)} verildi.")
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
            raise RuleError(f"TP kademesi {numbers.num(k.price)} için yüzde {numbers.num(k.pct)}, en küçük miktar "
                            f"adımının altında kalıyor. "
                            f"Kademeyi büyüt ya da kademe sayısını azalt.")
        out.append(TpLevel(k.price, k.pct, q))
    return out


# Kademenin miktarı kurulurken sabitlenir, Binance'teki yalnızca kapatan emir
# gibi. Pozisyon kısmen kapanınca, bir kademe dolunca ya da ekleme yapılınca
# miktar aynı kalır ama pozisyonun başka bir payına denk gelir. Yüzde burada o
# güne çekilir, panel ve Opus eski yüzdeyi görmesin.
#
# Yüzde yukarı yuvarlanır. Panel ya da Opus bu yüzdeyle kademeyi yeniden
# kurarsa split_tp aşağı yuvarladığı için aynı miktar çıkar, aşağı yuvarlanmış
# bir yüzde bir adım eksik miktar verirdi. Toplam yine de yüzü geçmez.
# Pozisyon küçülüp kapsamadığı kademeler düşer, onlar hiç dolamaz.
def tp_shares(levels: tuple[TpLevel, ...], qty: float) -> tuple[TpLevel, ...]:
    """Açık pozisyonun kademelerinin yüzdesini pozisyonun bugünkü miktarına göre yeniden yazar.

    Args:
        levels: Miktarı belli kademeler, son kademe kalanın tamamıysa qty None.
        qty: Pozisyonun bugünkü miktarı.

    Returns:
        Fiyatı ve miktarı aynı, yüzdesi güncel kademeler.
    """
    out, left, used = [], qty, 0.0
    for k in levels:
        if left <= TOLERANCE:
            break
        if k.qty is None:
            out.append(TpLevel(k.price, round(100 - used, 2), None))
            break
        take = min(k.qty, left)
        pct = min(math.ceil(take / qty * 10000 - 1e-6) / 100, round(100 - used, 2))
        out.append(TpLevel(k.price, pct, k.qty))
        used += pct
        left -= take
    return tuple(out)


# --- Büyüklük ---

# Miktar marjin ile kaldıraçtan çıkarılır ve adıma yuvarlanır. Gerçek marjin
# yuvarlanmış miktardan yeniden hesaplanır, bu yüzden verilen marjinden biraz
# az olabilir. Nakit yetmiyorsa ama fark _CASH_SLACK'ten küçükse miktar nakde
# sığacak kadar küçültülür.
#
# TODO: Miktar sınırı Binance'in piyasa emri sınırıdır. Limit emrin sınırı daha
# geniştir, emir türleri ayrılınca ikisi ayrı kontrol edilmeli.
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
        RuleError: Marjin sıfır ya da negatifse, nakit yetmiyorsa, miktar tek emrin sınırını aşıyorsa ya da
            büyüklük en küçük sınırın altındaysa.
    """
    if margin <= 0:
        raise RuleError("Marjin sıfırdan büyük olmalı.")
    qty = round_qty(spec, margin * leverage / entry)
    needed = lambda q: q * entry / leverage + q * entry * fee_rate
    if needed(qty) > cash + TOLERANCE:
        most = round_qty(spec, cash / (entry / leverage + entry * fee_rate))
        if needed(qty) > cash * (1 + _CASH_SLACK) or most <= 0:
            raise RuleError(f"Yetersiz nakit. Nakit {numbers.num(cash, 2)} dolar, bu işlem için "
                            f"{numbers.num(needed(qty), 2)} dolar gerekiyor.")
        qty = most
    if qty > spec.max_market_qty:
        raise RuleError(f"{spec.symbol} için tek emrin miktarı en fazla "
                        f"{numbers.qty(spec.max_market_qty, spec.symbol)} olabilir.")
    smallest = min_notional(spec, entry)
    if qty * entry < smallest - TOLERANCE:
        got = (f"miktar sıfıra yuvarlanıyor, en küçük miktar {numbers.qty(spec.qty_step, spec.symbol)}" if qty == 0
               else f"büyüklük {numbers.num(qty * entry, 2)} dolar oluyor")
        raise RuleError(
            f"{spec.symbol} için en küçük işlem büyüklüğü yaklaşık {numbers.num(smallest, 2)} dolar. Bu marjin ve "
            f"kaldıraçla {got}. {numbers.num(leverage)}x kaldıraçla en az {numbers.num(smallest / leverage, 2)} "
            f"dolar marjin gerekir.")
    return Sizing(qty, qty * entry / leverage, qty * entry * fee_rate)


# --- Kapanış ---

# Maker kapanış, yani TP kademesi, tam seviyesinden ve kaymasız gerçekleşir.
# Diğer kapanışlar kayma yer: long'un kapanışı satıştır, short'unki alış.
def close_fill(spec: SymbolSpec, side: Side, entry: float, qty: float, price: float, maker: bool,
               fee_rate: float) -> tuple[float, float, float]:
    """Bir kapanışın gerçekleşen fiyatını, fiyat kârını ve kapanış komisyonunu döndürür.

    Args:
        spec: Paritenin kuralları.
        side: Pozisyonun yönü.
        entry: Pozisyonun ortalama giriş fiyatı.
        qty: Kapanan miktar.
        price: Kapanış seviyesi ya da piyasa fiyatı.
        maker: Kapanış maker ise True.
        fee_rate: Kapanışın komisyon oranı.

    Returns:
        Gerçekleşen fiyat, fonlama hariç fiyat kârı ve komisyon.
    """
    exit_ = price if maker else slipped(spec, price, buy=side is Side.SHORT)
    return exit_, (exit_ - entry) * qty * side.sign, qty * exit_ * fee_rate
