"""
file: rules.py
importable: TOLERANCE, DEFAULT_LEVERAGE, slipped, round_qty, to_tick, min_notional, bracket, liq_price, liq_fee,
            unrealized_pnl, funding_payment, is_buy, crosses, merge_entry, qty_for_margin, order_margin, open_loss,
            check_leverage, check_notional, check_leverage_change, check_qty, check_ticks, check_price_band,
            check_trigger, check_trail, trail_level, limit_reached, triggered, trigger_fill, close_fill

Binance USDⓈ-M vadelinin hedge modunda izole marjinli hesap kuralları, saf
fonksiyonlar olarak. Hiçbiri durum tutmaz, veritabanına ya da ağa dokunmaz.
Kâğıt broker bu kurallarla hesabı simüle eder, canlı broker ise emri
Binance'e göndermeden önce aynı kurallarla kontrol eder. Kurala aykırı bir
girdi RuleError fırlatır.

description: Kaldıraç, miktar, fiyat adımı, tetik, maliyet, kayma, likidasyon, iz ve eşleştirme kuralları.
"""

# NOTE: Kayma aleyhe işler. Alışta fiyat yükselir, satışta düşer. Long giriş ve
# short çıkış alıştır, short giriş ve long çıkış satıştır, is_buy bunu söyler.
# Maker sayılan işlemler kaymasız ve tam seviyesinden gerçekleşir.
#
# NOTE: Fonksiyonlar fiyatı ve miktarı float olarak alır. Kuruşun altındaki
# kayan nokta farkları geçerli bir işlemin reddedilmesine yol açmasın diye
# karşılaştırmalarda TOLERANCE payı kullanılır.
#
# NOTE: Komutlar miktar taşır. Eski hesapta nakit birkaç sent eksik kalınca
# miktar kendiliğinden küçülüyordu. Artık eksik bakiye reddedilir, bakiyenin
# tamamıyla işlem açmak isteyen tarafın miktarı küçültmesi Opus'un ve panelin
# katmanının işidir, D aşaması.

import math

from . import numbers
from .errors import RuleError
from .models import Bracket, Side, SymbolSpec

TOLERANCE = 1e-9

# Hiç ayarlanmamış bir paritenin kaldıracı, Binance'in varsayılanı.
DEFAULT_LEVERAGE = 20

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


# Hedge modunda long'u açan ve short'u kapatan emir alıştır.
def is_buy(side: Side, reduce: bool) -> bool:
    return (side is Side.LONG) != reduce


# Limit emir fiyatın yanlış tarafındaysa karşı tarafla hemen eşleşir ve taker
# dolar: alış limiti güncel fiyatın üstünde ya da satış limiti altında.
def crosses(buy: bool, limit: float, price: float) -> bool:
    return limit >= price if buy else limit <= price


# Aynı yöndeki yeni giriş pozisyonla birleşir, giriş fiyatı miktarla ağırlıklı
# ortalanır.
def merge_entry(qty: float, entry: float, add_qty: float, add_price: float) -> float:
    return (qty * entry + add_qty * add_price) / (qty + add_qty)


# Marjinle düşünen taraf, yani Opus ve panel, komutun miktarını bununla bulur.
# Miktar adıma aşağı yuvarlanır, marjin hiçbir zaman verilenden büyük çıkmaz.
def qty_for_margin(spec: SymbolSpec, leverage: int, margin: float, price: float) -> float:
    return round_qty(spec, margin * leverage / price)


# --- Marjin ve maliyet ---

def order_margin(qty: float, price: float, leverage: int) -> float:
    return qty * price / leverage


# Binance bir emrin maliyetini başlangıç marjini ile açık zararın toplamı
# sayar. Açık zarar, emrin fiyatı mark fiyatının aleyhindeyse aradaki farktır:
# mark'ın üstünde bir alış ya da altında bir satış daha dolarken zararda
# başlar. Emir verilirken bu toplam kullanılabilir bakiyeden ayrılır.
def open_loss(buy: bool, qty: float, price: float, mark: float) -> float:
    return qty * max(0.0, price - mark if buy else mark - price)


# --- Likidasyon ve fonlama ---

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


# Binance likidasyonda büyüklüğün bu oranını ücret olarak alır. Ücret
# pozisyonun kalan marjininden fazla olamaz, izole marjinde kayıp marjinle
# sınırlıdır. Sınırı ücreti kullanan taraf uygular.
def liq_fee(spec: SymbolSpec, qty: float, price: float) -> float:
    return qty * price * spec.liq_fee


def unrealized_pnl(side: Side, entry: float, qty: float, price: float) -> float:
    return (price - entry) * qty * side.sign


# Oran pozitifse long öder short alır, negatifse tersi. Pozitif sonuç
# pozisyonun ödediği, negatif sonuç aldığı tutardır. Ödeme o anki mark
# fiyatıyla pozisyonun büyüklüğü üzerinden hesaplanır.
def funding_payment(side: Side, qty: float, mark: float, rate: float) -> float:
    return qty * mark * rate * side.sign


# --- Doğrulama ---

# Binance'te kaldıraç tam sayıdır, üst sınırı paritenin ilk kademesidir.
def check_leverage(spec: SymbolSpec, leverage: float) -> None:
    top = spec.brackets[0].max_leverage
    if leverage != int(leverage) or not 1 <= leverage <= top:
        raise RuleError(f"{spec.symbol} için kaldıraç 1 ile {top} arasında bir tam sayı olmalı, "
                        f"{numbers.num(leverage)} verildi.")


# Kaldıraç büyüdükçe izin verilen en büyük pozisyon küçülür: verilen kaldıracı
# taşıyan kademelerin en büyüğü sınırdır. notional pozisyonun işlemden sonraki
# büyüklüğüdür.
def check_notional(spec: SymbolSpec, leverage: int, notional: float) -> None:
    cap = max(b.cap for b in spec.brackets if b.max_leverage >= leverage)
    if notional > cap + TOLERANCE:
        raise RuleError(f"{spec.symbol} için {leverage}x kaldıraçla pozisyon en fazla {numbers.num(cap)} "
                        f"dolar olabilir, bu işlemle {numbers.num(notional, 2)} dolar oluyor.")


# Açık pozisyon varken kaldıraç düşürülemez, Binance izole marjinde -4161 ile
# reddeder. Yükseltme kabul edilir ama paritenin açık büyüklüğü yeni
# kaldıracın kademe sınırına sığmalıdır. notional paritenin iki yönündeki açık
# büyüklüğün büyük olanıdır.
def check_leverage_change(spec: SymbolSpec, old: int, new: float, notional: float) -> None:
    """Paritenin kaldıracının değişebileceğini doğrular.

    Args:
        spec: Paritenin kuralları.
        old: Paritenin bugünkü kaldıracı.
        new: İstenen kaldıraç.
        notional: Paritede açık pozisyonun büyüklüğü, yoksa sıfır.

    Raises:
        RuleError: Kaldıraç geçersizse, açık pozisyonda düşürülüyorsa ya da açık büyüklük yeni kaldıraca sığmıyorsa.
    """
    check_leverage(spec, new)
    if notional <= 0:
        return
    if new < old:
        raise RuleError(f"{spec.symbol} için açık pozisyon varken kaldıraç düşürülemez, bugünkü kaldıraç {old}x.")
    check_notional(spec, int(new), notional)


# Binance miktarı adımın katı, en küçük büyüklüğün üstünde ve emrin türünün
# sınırının altında ister. Piyasa emrinin sınırı limit ve tetikli emirlerinkinden
# dardır, tetikli emir de tetiklenince piyasa emri olur ama Binance onu
# kurulurken limit sınırıyla kontrol eder.
def check_qty(spec: SymbolSpec, qty: float, price: float, market: bool) -> None:
    """Bir emrin miktarını doğrular.

    Args:
        spec: Paritenin kuralları.
        qty: Emrin miktarı.
        price: Büyüklüğün hesaplandığı fiyat, emrin fiyatı ya da güncel fiyat.
        market: Emir anında piyasadan dolacaksa True.

    Raises:
        RuleError: Miktar sıfırsa, adımın katı değilse, sınırı aşıyorsa ya da büyüklük en küçük sınırın altındaysa.
    """
    unit = numbers.qty(spec.qty_step, spec.symbol)
    if qty <= 0:
        raise RuleError("Miktar sıfırdan büyük olmalı.")
    if abs(qty / spec.qty_step - round(qty / spec.qty_step)) > 1e-6:
        raise RuleError(f"{spec.symbol} için miktar {numbers.num(qty)}, miktar adımı {unit} değerinin katı olmalı.")
    top = spec.max_market_qty if market else spec.max_qty
    if qty > top + TOLERANCE:
        kind = "piyasa emrinin" if market else "emrin"
        raise RuleError(f"{spec.symbol} için tek {kind} miktarı en fazla {numbers.qty(top, spec.symbol)} olabilir.")
    smallest = min_notional(spec, price)
    if qty * price < smallest - TOLERANCE:
        raise RuleError(f"{spec.symbol} için en küçük işlem büyüklüğü yaklaşık {numbers.num(smallest, 2)} dolar, "
                        f"bu emirle {numbers.num(qty * price, 2)} dolar oluyor.")


# Fiyat ve tetik Binance'te fiyat adımının katı olmalıdır.
def check_ticks(spec: SymbolSpec, *prices: float | None) -> None:
    for x in prices:
        if x and abs(x / spec.tick - round(x / spec.tick)) > 1e-6:
            raise RuleError(f"{spec.symbol} fiyatı {numbers.num(x)}, fiyat adımı {numbers.num(spec.tick)} "
                            f"değerinin katı olmalı.")


# Limit fiyatı mark fiyatından fazla uzak olamaz, Binance'in PERCENT_PRICE
# kuralı. Alış limiti mark × price_up'ı aşamaz, satış limiti mark ×
# price_down'ın altına inemez.
def check_price_band(spec: SymbolSpec, buy: bool, price: float, mark: float) -> None:
    if buy and price > mark * spec.price_up + TOLERANCE:
        raise RuleError(f"{spec.symbol} için alış limiti en fazla {numbers.price(mark * spec.price_up)} olabilir, "
                        f"mark fiyatı {numbers.price(mark)}.")
    if not buy and price < mark * spec.price_down - TOLERANCE:
        raise RuleError(f"{spec.symbol} için satış limiti en az {numbers.price(mark * spec.price_down)} olabilir, "
                        f"mark fiyatı {numbers.price(mark)}.")


# Stop alışta fiyat tetiğin üstüne çıkınca, satışta altına inince tetiklenir.
# Take-profit tersidir. Tetik daha kurulurken geçilmişse Binance emri hemen
# tetiklenirdi diyerek -2021 hatasıyla reddeder. price tetiğin baktığı
# fiyattır: SL ve izde mark, giriş ve TP'de son fiyat, dolumda kurulacak
# korumada girişin fiyatı. Mesaj emri Binance'in alış satış diliyle değil
# kırılım, geri çekilme, SL ve TP diye anar, Opus ve panel böyle düşünür.
def check_trigger(stop: bool, buy: bool, reduce: bool, trigger: float, price: float, basis: str) -> None:
    """Tetiğin güncel fiyata göre doğru tarafta olduğunu doğrular.

    Args:
        stop: Stop emriyse True, take-profit emriyse False.
        buy: Emir alışsa True.
        reduce: Emir pozisyonu kapatıyorsa True.
        trigger: Tetik fiyatı.
        price: Tetiğin baktığı fiyat.
        basis: O fiyatın mesajdaki adı, örneğin mark fiyatı ya da son fiyat.

    Raises:
        RuleError: Tetik fiyatın yanlış tarafındaysa.
    """
    above = buy == stop
    if (trigger <= price) if above else (trigger >= price):
        if reduce:
            name = "SL" if stop else "TP"
        else:
            name = "Kırılım girişinin" if stop else "Geri çekilme girişinin"
        where = "üstünde" if above else "altında"
        raise RuleError(f"{name} tetiği {numbers.price(trigger)}, {basis} {numbers.price(price)} olduğu için bu "
                        f"fiyatın {where} olmalı, yoksa emir hemen tetiklenirdi.")


def check_trail(pct: float | None) -> None:
    if pct is not None and not _MIN_TRAIL_PCT <= pct <= _MAX_TRAIL_PCT:
        raise RuleError(f"İz süren stop mesafesi yüzde {numbers.num(_MIN_TRAIL_PCT)} ile "
                        f"{numbers.num(_MAX_TRAIL_PCT)} arasında olmalı, {numbers.num(pct)} verildi.")


# --- İz süren stop ---

# side korunan pozisyonun yönüdür. best emrin konduğu andan beri görülen en iyi
# mark fiyatıdır: long'da en yüksek, short'ta en düşük.
def trail_level(side: Side, best: float, pct: float) -> float:
    return best * (1 - pct / 100) if side is Side.LONG else best * (1 + pct / 100)


# --- Kâğıt eşleştirme ---

# Defterdeki limit emir fiyat seviyeyi en az bir tick geçince tam seviyesinden
# maker dolar. Seviyeye yalnızca dokunan fiyat Binance'te sıradaki emirlere
# yetmeyebilir, dolduğu varsayılmaz.
def limit_reached(spec: SymbolSpec, buy: bool, price: float, high: float, low: float) -> bool:
    return low <= price - spec.tick + TOLERANCE if buy else high >= price + spec.tick - TOLERANCE


# above True ise emir fiyat yükselip seviyeye gelince tetiklenir: alış stopu
# ve satış take-profit'i. False ise düşüp gelince.
def triggered(above: bool, level: float, high: float, low: float) -> bool:
    return high >= level if above else low <= level


# Tetiklenen piyasa emrinin kaymasız dolum fiyatı. Binance'te son fiyat
# seviyeye vardığı an piyasa emri gider ve o anın fiyatından dolar. Aralık
# seviyenin ötesinde açıldıysa o an aralığın başıdır, dolum açılıştandır.
# Değilse fiyat seviyeyi aralığın içinde geçmiştir, dolum seviyedendir.
# Tetikten sonra olan, örneğin iğnenin geri dönmesi, dolumu değiştirmez.
# open_ akışta bir önceki son fiyat, kapalı geçen dakikalar işlenirken mumun
# açılışıdır.
def trigger_fill(above: bool, level: float, open_: float) -> float:
    beyond = open_ >= level if above else open_ <= level
    return open_ if beyond else level


# --- Kapanış ---

# Maker kapanış tam seviyesinden ve kaymasız gerçekleşir. Diğer kapanışlar
# kayma yer: long'un kapanışı satıştır, short'unki alış.
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
