"""
file: market.py
importable: server

Opus'un her çağrıda kullanabildiği piyasa araçlarını sunan MCP sunucusu.
Sunucuyu claude komutu kendisi başlatır, stdio üzerinden konuşur ve çağrı
bitince kapatır. Araçlar mum, gösterge, seviye, hacim profili, korelasyon,
emir defteri, fonlama, açık pozisyon, long short oranı, likidasyon, NY
borsası açılışı ve Opus'un kendi kapanmış işlemlerini döndürür.

description: Opus'un ihtiyaç duyduğu piyasa ve geçmiş verisini kendisinin seçerek almasını sağlayan araçlar.
"""

# NOTE: Veritabanları salt okunur açılır, Opus bu araçlarla hesabı
# değiştiremez. Hangi dosyaların okunacağını LLM stratejisi ortam
# değişkenleriyle bildirir: OPUS_MARKET_DB toplayıcının deposu, OPUS_CORE_DB
# çekirdeğin deposu, OPUS_ACCOUNT profilin hesap kimliği, OPUS_PROFILE profilin
# kimliği.
#
# NOTE: Araç çıktıları JSON yerine sıkı metin olarak döner. Aynı bilgi bu
# biçimde daha az token tutar ve Opus için okuması aynı derecede kolaydır.
#
# XXX: Opus araçları sunucu adı, araç adı, parametre adları ve docstring ile
# görür. Bu yüzden araç fonksiyonlarının adları, parametreleri ve
# docstring'leri kodun geri kalanından farklı olarak Türkçedir.
#

import asyncio
import json
import logging
import math
import os
import sqlite3
import statistics
import time
from pathlib import Path

import httpx
from mcp.server.mcpserver import MCPServer

from ..market import analysis, indicators, liquidations, opening
from ..market.binance.rest import BASE_URL, BinanceRest
from ..market.source import MarketDataError, RatioKind
from ..storage.ledger import Ledger
from ..strategy.llm.cards import indicator_summary, LABELS
from ..strategy.llm.texts import REASONS, local

server = MCPServer("piyasa")

# httpx her isteği INFO seviyesinde yazar, araç sunucusunun çıktısını boğmasın.
logging.getLogger("httpx").setLevel(logging.WARNING)

_INTERVALS = ("1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w", "1M")
_PERIODS = ("5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d")
_LEVELS_SHOWN = 8

# Bir TP'ye bu yüzdeden az farkla varamayan işlem karnenin özetinde ayrıca
# sayılır. Eşik 17.11 analizinden gelir, kaçan altı hedefin altısı da bu farkın
# içindeydi.
_NEAR_MISS_PCT = 0.6

# Göstergelerin Opus'a görünen adları, varsayılan periyotları ve çizgi adları.
_SERIES = {"ema": (20,), "sma": (20,), "rsi": (14,), "atr": (14,), "bollinger": (20, 2), "macd": (12, 26, 9),
           "stokastik": (14, 3), "vwap": ()}
_LINE_NAMES = {"mid": "orta", "upper": "ust", "lower": "alt", "signal": "sinyal"}

_market: BinanceRest | None = None


def _rest() -> BinanceRest:
    global _market
    if _market is None:
        _market = BinanceRest(httpx.AsyncClient(base_url=BASE_URL, timeout=15))
    return _market


def _clock(t: int) -> str:
    return local(t).strftime("%d.%m.%Y %H:%M")


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _n(v: float | None) -> str:
    return "yok" if v is None else f"{indicators.rounded(v):g}"


def _bad_interval(aralik: str) -> str | None:
    return None if aralik in _INTERVALS else f"Geçersiz aralık. Geçerli olanlar: {', '.join(_INTERVALS)}"


def _bad_period(periyot: str) -> str | None:
    return None if periyot in _PERIODS else f"Geçersiz periyot. Geçerli olanlar: {', '.join(_PERIODS)}"


# --- Piyasa ---

@server.tool()
async def mumlar(sembol: str, aralik: str = "1h", adet: int = 100) -> str:
    """Vadeli piyasadan geçmiş mumları getirir. Katalog dışındaki pariteler de sorulabilir.

    Args:
        aralik: 1m, 3m, 5m, 15m, 30m, 1h, 2h, 4h, 6h, 8h, 12h, 1d, 3d, 1w ya da 1M.
        adet: Bir ile bin beş yüz arası.
        sembol: Binance USDT vadeli sembolü.
    """
    if bad := _bad_interval(aralik):
        return bad
    items = await _rest().candles(sembol.upper(), aralik, _clamp(adet, 1, 1500))
    return "saat  açılış yüksek düşük kapanış  hacim\n" + "\n".join(
        f"{_clock(m.time)}  {m.open:g} {m.high:g} {m.low:g} {m.close:g}  {m.volume:.1f}" for m in items)


@server.tool()
async def gostergeler_hesapla(sembol: str, aralik: str = "1h") -> str:
    """Seçilen aralıkta EMA20, EMA50, EMA200, RSI14, MACD, ATR14 ve Bollinger değerlerini hesaplar.

    Args:
        aralik: mumlar aracındaki aralıklardan biri.
        sembol: Binance USDT vadeli sembolü.
    """
    if bad := _bad_interval(aralik):
        return bad
    s = indicator_summary(await _rest().candles(sembol.upper(), aralik, 300))
    return "\n".join(f"{LABELS[k]}: {v}" for k, v in s.items())


@server.tool()
async def emir_defteri(sembol: str, derinlik: int = 20) -> str:
    """Vadeli emir defterinin en iyi alış ve satış kademelerini getirir.

    Args:
        derinlik: 5, 10, 20, 50, 100, 500 ya da 1000.
        sembol: Binance USDT vadeli sembolü.
    """
    depth = min((5, 10, 20, 50, 100, 500, 1000), key=lambda v: abs(v - derinlik))
    book = await _rest().order_book(sembol.upper(), depth)
    asks = "\n".join(f"  {p:g} : {q:g}" for p, q in reversed(book.asks))
    bids = "\n".join(f"  {p:g} : {q:g}" for p, q in book.bids)
    return f"Satış kademeleri, fiyat : miktar\n{asks}\n---\nAlış kademeleri\n{bids}"


@server.tool()
async def fonlama_gecmisi(sembol: str, adet: int = 21) -> str:
    """Son gerçekleşen fonlama oranlarını getirir. Fonlama çoğu paritede sekiz saatte bir, bazılarında dört saatte bir olur.

    Args:
        adet: Bir ile bin arası.
        sembol: Binance USDT vadeli sembolü.
    """
    found = await _rest().funding_history(sembol.upper(), limit=_clamp(adet, 1, 1000))
    return "\n".join(f"{_clock(x.time)}  yüzde {x.rate * 100:.4f}" for x in found)


@server.tool()
async def acik_pozisyon_gecmisi(sembol: str, periyot: str = "1h", adet: int = 48) -> str:
    """Vadelideki toplam açık pozisyon miktarının geçmişini getirir. Binance yalnızca son otuz günü verir.

    Args:
        periyot: 5m, 15m, 30m, 1h, 2h, 4h, 6h, 12h ya da 1d.
        adet: Bir ile beş yüz arası.
        sembol: Binance USDT vadeli sembolü.
    """
    if bad := _bad_period(periyot):
        return bad
    found = await _rest().open_interest_history(sembol.upper(), periyot, _clamp(adet, 1, 500))
    return "\n".join(f"{_clock(x.time)}  {x.qty:.2f} birim, {x.usd / 1e6:.1f} milyon dolar" for x in found)


@server.tool()
async def long_short_oranlari(sembol: str, periyot: str = "1h", adet: int = 24) -> str:
    """Binance kullanıcılarının long short dağılımını ve agresif alış satış oranını getirir.

    Üç seri döner. Tüm hesaplar oranı, hesapların ne kadarının long olduğunu
    gösterir. Büyük hesaplar oranı, en büyük yatırımcıların pozisyon
    dağılımıdır. Taker oranı, piyasa emriyle yapılan alışların satışlara oranıdır.

    Args:
        periyot: 5m, 15m, 30m, 1h, 2h, 4h, 6h, 12h ya da 1d.
        adet: Bir ile beş yüz arası.
        sembol: Binance USDT vadeli sembolü. Altın gibi geleneksel varlıklarda bu veri olmayabilir.
    """
    if bad := _bad_period(periyot):
        return bad
    m, s, n = _rest(), sembol.upper(), _clamp(adet, 1, 500)
    try:
        everyone, top, taker = await asyncio.gather(m.long_short(RatioKind.ACCOUNTS, s, periyot, n),
                                                    m.long_short(RatioKind.TOP_POSITIONS, s, periyot, n),
                                                    m.taker_volume(s, periyot, n))
    except MarketDataError:
        everyone = []
    if not everyone:
        return f"{s} için long short verisi yok."
    lines = ["saat  tüm_hesaplar_long_yüzde  büyük_hesaplar_long_yüzde  taker_alış_satış"]
    for a, b, c in zip(everyone, top, taker):
        lines.append(f"{_clock(a.time)}  {a.long_pct:.1f}  {b.long_pct:.1f}  {c.buy_sell_ratio:.3f}")
    return "\n".join(lines)


def _series(ad: str, candles, periods: tuple) -> dict[str, list]:
    if ad not in _SERIES:
        raise ValueError(f"Bilinmeyen gösterge {ad}. Geçerli olanlar: {', '.join(_SERIES)}")
    p = tuple(periods) + _SERIES[ad][len(periods):]
    if any(x <= 0 for x in p):
        raise ValueError("Periyotlar sıfırdan büyük olmalı.")
    closes = [m.close for m in candles]
    if ad in ("ema", "sma", "rsi"):
        fn = {"ema": indicators.ema, "sma": indicators.sma, "rsi": indicators.rsi}[ad]
        return {f"{ad}{int(p[0])}": fn(closes, int(p[0]))}
    if ad == "atr":
        return {f"atr{int(p[0])}": indicators.atr(candles, int(p[0]))}
    if ad == "bollinger":
        found = indicators.bollinger(closes, int(p[0]), float(p[1]))
    elif ad == "macd":
        found = indicators.macd(closes, int(p[0]), int(p[1]), int(p[2]))
    elif ad == "stokastik":
        found = indicators.stochastic(candles, int(p[0]), int(p[1]))
    else:
        found = {"vwap": indicators.vwap(candles)}
    return {_LINE_NAMES.get(k, k): v for k, v in found.items()}


@server.tool()
async def gosterge(sembol: str, ad: str, aralik: str = "1h", periyotlar: list[float] | None = None,
                   adet: int = 30) -> str:
    """Bir göstergeyi istediğin periyotlarla hesaplar ve son değerlerini mum zamanlarıyla birlikte getirir.

    Göstergeler ve varsayılan periyotları: ema 20, sma 20, rsi 14, atr 14,
    bollinger 20 ve 2 standart sapma, macd 12 26 9, stokastik 14 ve 3, vwap.
    vwap periyot almaz, getirilen mumların ilkinden başlayarak birikir.

    Args:
        sembol: Binance USDT vadeli sembolü.
        ad: ema, sma, rsi, atr, bollinger, macd, stokastik ya da vwap.
        aralik: mumlar aracındaki aralıklardan biri.
        periyotlar: Göstergenin periyotları sırasıyla. Verilmeyenler varsayılan kalır.
        adet: Kaç son değerin getirileceği, bir ile beş yüz arası. vwap'ta birikimin başladığı mum sayısıdır.
    """
    if bad := _bad_interval(aralik):
        return bad
    adet = _clamp(adet, 1, 500)
    # vwap baştan birikir, diğer göstergeler ısınmak için önceden yeterli mum ister.
    candles = await _rest().candles(sembol.upper(), aralik, adet if ad == "vwap" else min(1500, adet + 500))
    try:
        found = _series(ad, candles, tuple(periyotlar or ()))
    except ValueError as e:
        return str(e)
    names = list(found)
    lines = ["saat  kapanış  " + "  ".join(names)]
    for i in range(max(0, len(candles) - adet), len(candles)):
        lines.append(f"{_clock(candles[i].time)}  {_n(candles[i].close)}  " + "  ".join(_n(found[k][i]) for k in names))
    return "\n".join(lines)


@server.tool()
async def seviyeler(sembol: str, aralik: str = "4h", adet: int = 300, pencere: int = 5) -> str:
    """Mumlardaki tepe ve dipleri bulur, birbirine yakın olanları destek ve direnç seviyelerinde toplar.

    Bir mum, iki yanındaki pencere kadar mumun hepsinden yüksekse tepe,
    hepsinden düşükse dip sayılır. Aralarındaki fark o aralığın ATR'sinin
    yarısından az olan uçlar tek seviye olur. Her seviye için kaç kez
    tepe ya da dip olduğu ve ilk ve son ne zaman olduğu verilir.

    Args:
        sembol: Binance USDT vadeli sembolü.
        aralik: mumlar aracındaki aralıklardan biri.
        adet: Kaç mumun inceleneceği, elli ile bin beş yüz arası.
        pencere: Bir ile yirmi arası. Büyüdükçe yalnızca daha belirgin uçlar kalır.
    """
    if bad := _bad_interval(aralik):
        return bad
    candles = await _rest().candles(sembol.upper(), aralik, _clamp(adet, 50, 1500))
    found = analysis.levels(candles, _clamp(pencere, 1, 20))
    if not found:
        return "Bu aralıkta belirgin bir tepe ya da dip bulunamadı."
    last = candles[-1].close
    above = [x for x in found if x.price > last][:_LEVELS_SHOWN]
    below = [x for x in found if x.price <= last][-_LEVELS_SHOWN:]
    lines = [f"güncel fiyat {_n(last)}. {len(found)} seviye bulundu, fiyata en yakın {len(above)} üst ve "
             f"{len(below)} alt seviye gösteriliyor.", "seviye  konum  uç_sayısı  ilk  son"]
    for x in sorted(above + below, key=lambda x: x.price, reverse=True):
        lines.append(f"{_n(x.price)}  {'üstte' if x.price > last else 'altta'}  {x.touches}  {_clock(x.first)}  "
                     f"{_clock(x.last)}")
    return "\n".join(lines)


@server.tool()
async def hacim_profili(sembol: str, aralik: str = "1h", adet: int = 168, kademe: int = 24) -> str:
    """Seçilen mumlarda hacmin hangi fiyat aralıklarında yoğunlaştığını hesaplar.

    Fiyat aralığı eşit kademelere bölünür, her mumun hacmi kendi düşüğü ile
    yükseği arasındaki kademelere eşit paylaştırılır. En yoğun kademe ve
    hacmin yüzde yetmişinin toplandığı değer alanı ayrıca verilir.

    Args:
        sembol: Binance USDT vadeli sembolü.
        aralik: mumlar aracındaki aralıklardan biri.
        adet: Kaç mumun kullanılacağı, on ile bin beş yüz arası.
        kademe: Kademe sayısı, beş ile yüz arası.
    """
    if bad := _bad_interval(aralik):
        return bad
    candles = await _rest().candles(sembol.upper(), aralik, _clamp(adet, 10, 1500))
    vp = analysis.volume_profile(candles, _clamp(kademe, 5, 100))
    total = sum(v for _, _, v in vp.rows) or 1
    lines = [f"{_clock(candles[0].time)} ile {_clock(candles[-1].time)} arası, güncel fiyat {_n(candles[-1].close)}",
             f"en yoğun kademe {_n(vp.poc)}, değer alanı {_n(vp.value_low)} ile {_n(vp.value_high)}",
             "kademe  hacmin_yüzdesi"]
    lines += [f"{_n(lo)}-{_n(hi)}  {v / total * 100:.1f}" for lo, hi, v in reversed(vp.rows)]
    return "\n".join(lines)


@server.tool()
async def korelasyon(semboller: list[str], aralik: str = "1h", adet: int = 168) -> str:
    """Pariteler arasındaki getiri korelasyonunu hesaplar.

    Her paritenin mumdan muma logaritmik getirisi alınır, aynı zamana düşen
    getiriler eşleştirilir ve her çift için Pearson korelasyonu verilir.
    Değer bir ise birlikte, eksi bir ise ters, sıfıra yakınsa bağımsız hareket ederler.

    Args:
        semboller: İki ile on arası Binance USDT vadeli sembolü.
        aralik: mumlar aracındaki aralıklardan biri.
        adet: Kaç mumun kullanılacağı, on ile bin beş yüz arası.
    """
    if bad := _bad_interval(aralik):
        return bad
    names = list(dict.fromkeys(x.upper() for x in semboller))
    if not 2 <= len(names) <= 10:
        return "İki ile on arası sembol verilmeli."
    count = _clamp(adet, 10, 1500)
    found = await asyncio.gather(*(_rest().candles(s, aralik, count) for s in names))
    lines = ["çift  korelasyon  ortak_mum"]
    for (a, b), (r, n) in analysis.correlation(dict(zip(names, found))).items():
        lines.append(f"{a} {b}  {'hesaplanamadı' if math.isnan(r) else f'{r:.2f}'}  {n}")
    return "\n".join(lines)


@server.tool()
def likidasyonlar(sembol: str, saat: int = 24) -> str:
    """Bir paritede son saatlerde zorla kapatılan pozisyonları dalgalar halinde getirir.

    Kayıtlar Binance'in canlı likidasyon akışından bütün piyasa için tutulur.
    Binance bu akışta her parite için saniyede yalnızca en büyük likidasyonu
    gönderir, bu yüzden tutarlar toplamın tamamı değil bir örneklemidir.
    Dalgaların zamanı, yönü ve büyüklük sırası doğrudur. Aralarında iki
    dakikadan az boşluk olan kayıtlar bir dalga sayılır. Toplayıcının kapalı
    kaldığı kısa aralıklarda kayıt yoktur.

    Args:
        sembol: Binance USDT vadeli sembolü.
        saat: Kaç saat geriye bakılacağı, bir ile yedi yüz yirmi arası.
    """
    s = sembol.upper()
    hours = _clamp(saat, 1, 720)
    since = int(time.time()) - hours * 3600
    con = liquidations.open_readonly(Path(os.environ["OPUS_MARKET_DB"]))
    try:
        tot, found = liquidations.totals(con, s, since), liquidations.waves(con, s, since)
    finally:
        con.close()
    if not found:
        return f"Son {hours} saatte {s} için kayıtlı likidasyon yok."
    (ln, lu), (sn, su) = tot["long"], tot["short"]
    lines = [f"long likidasyonları {ln} kayıt, {lu / 1e3:.1f} bin dolar. short likidasyonları {sn} kayıt, "
             f"{su / 1e3:.1f} bin dolar.", "başlangıç  bitiş  kayıt  long_bin_dolar  short_bin_dolar  fiyat_aralığı  "
             "en_büyük"]
    for w in found:
        b = w.largest
        lines.append(f"{_clock(w.start)}  {_clock(w.end)[-5:]}  {w.count}  {w.long_usd / 1e3:.1f}  "
                     f"{w.short_usd / 1e3:.1f}  {_n(w.low)}-{_n(w.high)}  {b['side']} {b['usd'] / 1e3:.1f} bin dolar "
                     f"@ {_n(b['price'])}")
    return "\n".join(lines)


# --- NY açılışı ---

def _pct(x: float | None) -> str:
    return "alınamadı" if x is None else f"yüzde {x * 100:+.2f}"


def _hunt_row(x: opening.Hunt) -> str:
    return (f"{x.day:%d.%m.%Y}  {_pct(x.nasdaq_6h)}  {'yukarı' if x.side > 0 else 'aşağı'}  yüzde {x.size * 100:.2f}  "
            f"{x.peak_min}  yüzde {min(x.giveback, 9.99) * 100:.0f}")


@server.tool()
async def ny_acilisi(sembol: str) -> str:
    """NY borsasının bir sonraki açılışını ve açılış öncesi durumu getirir.

    Bir sonraki açılış anı, Nasdaq ve S&P vadelilerinin, QQQUSDT ve SPYUSDT,
    son altı saatteki ve önceki NY kapanışından bu yana hareketi, paritenin
    önceki 24 saatteki 1 dakikalık ortalama aralığı ve son açılışlardaki avlar
    verilir. Av, açılıştan sonraki ilk 15 dakikada fiyatın açılış fiyatından
    en uzağa gittiği noktadır. Beklenen av, son 20 açılışta avın oynaklığa
    oranının medyanı ile bugünkü oynaklıktan hesaplanır.

    Args:
        sembol: Binance USDT vadeli sembolü.
    """
    s, now, m = sembol.upper(), int(time.time()), _rest()
    o = opening.next_open(now)
    moves, past, vol = await asyncio.gather(opening.index_moves(m, now), opening.history(m, s, 20, now),
                                            opening.volatility(m, s))
    lines = [f"NY borsasının bir sonraki açılışı {_clock(o)}, {(o - now) // 60} dakika sonra."]
    for x in moves:
        lines.append(f"{x['name']}, {x['symbol']}: son 6 saatte {_pct(x['six_hours'])}, önceki NY kapanışından bu "
                     f"yana {_pct(x['since_close'])}.")
    if not past:
        lines.append(f"{s}: geçmiş açılış verisi alınamadı.")
        return "\n".join(lines)
    ratio = statistics.median(x.size / x.vol for x in past)
    avg_vol = statistics.mean(x.vol for x in past)
    lines.append(f"{s}: önceki 24 saatin 1 dakikalık ortalama aralığı yüzde {vol * 100:.3f}, son {len(past)} açılışın "
                 f"ortalamasının {vol / avg_vol:.2f} katı. Son {len(past)} açılışta av büyüklüğü medyan yüzde "
                 f"{statistics.median(x.size for x in past) * 100:.2f}. Beklenen av yaklaşık yüzde "
                 f"{ratio * vol * 100:.2f}.")
    lines.append("Son 10 açılış, yeniden eskiye:\ntarih  nasdaq_03-09  av_yönü  av  tepeye_dk  ilk_saatte_geri_verilen")
    lines += [_hunt_row(x) for x in past[:10]]
    return "\n".join(lines)


@server.tool()
async def acilis_gecmisi(sembol: str, gun: int = 20) -> str:
    """NY borsasının geçmiş işlem günlerindeki açılış avlarını gün gün getirir.

    Av, açılıştan sonraki ilk 15 dakikada fiyatın açılış fiyatından en uzağa
    gittiği noktadır. Her gün için Nasdaq vadelisinin, QQQUSDT, 03:00-09:00 NY
    arasındaki hareketi, avın yönü ve büyüklüğü, tepeye kaç dakikada
    varıldığı, avın açılıştan sonraki ilk saat içinde ne kadarının geri
    verildiği ve önceki 24 saatin 1 dakikalık ortalama aralığı verilir.

    Args:
        sembol: Binance USDT vadeli sembolü.
        gun: Kaç işlem günü geriye bakılacağı, bir ile altmış arası.
    """
    found = await opening.history(_rest(), sembol.upper(), _clamp(gun, 1, 60), int(time.time()))
    if not found:
        return f"{sembol.upper()} için açılış verisi bulunamadı."
    return "tarih  nasdaq_03-09  av_yönü  av  tepeye_dk  ilk_saatte_geri_verilen  oynaklık_24s\n" + "\n".join(
        f"{_hunt_row(x)}  yüzde {x.vol * 100:.3f}" for x in found)


# --- Geçmiş ---

def _trades(limit: int):
    con = sqlite3.connect(f"file:{os.environ['OPUS_CORE_DB']}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        found = Ledger(con).trades(int(os.environ["OPUS_ACCOUNT"]))
    finally:
        con.close()
    return list(reversed(found))[:limit]


@server.tool()
def gecmis_kararlarim(adet: int = 20, atla: int = 0, analiz_dahil: bool = False) -> str:
    """Kendi geçmiş kararlarını yeniden eskiye getirir.

    Args:
        adet: Bir ile yüz arası.
        atla: En yeni kaç kararın atlanacağı. Daha eski kararlara sayfa sayfa ulaşmak için.
        analiz_dahil: True ise her kararın tam analiz metni de gelir.
    """
    con = sqlite3.connect(f"file:{os.environ['OPUS_CORE_DB']}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT time, results, record FROM journal WHERE profile_id = ? AND error IS NULL "
                           "ORDER BY id DESC LIMIT ? OFFSET ?",
                           (int(os.environ["OPUS_PROFILE"]), _clamp(adet, 1, 100), max(0, atla))).fetchall()
    finally:
        con.close()
    parts = []
    for t, results, record in rows:
        k = json.loads(record).get("decision")
        if not k:
            continue
        done = "; ".join(x["message"] for x in json.loads(results)) or "eylem yok"
        text = f"{_clock(t)}, görüş {k['gorus']}, güven {k['guven']}\n  özet: {k['ozet']}\n  sonuç: {done}"
        if analiz_dahil:
            text += f"\n  analiz: {k['analiz']}"
        parts.append(text)
    return "\n\n".join(parts) or "Kayıtlı karar yok."


@server.tool()
def gecmis_islemlerim(adet: int = 50) -> str:
    """Kapanmış işlemlerini yeniden eskiye getirir. Bir pozisyonun kısmi kapanışları ayrı satırdır. Net sonuç komisyon ve fonlama dahildir.

    Args:
        adet: Bir ile beş yüz arası.
    """
    found = _trades(_clamp(adet, 1, 500))
    if not found:
        return "Kapanmış işlem yok."
    return "\n".join(
        f"#{t.number} {t.symbol} {t.side} {t.leverage:g}x miktar {t.qty:g} marjin {t.margin:.2f}, giriş "
        f"{t.entry_price:g} {_clock(t.entry_time)}, çıkış {t.exit_price:g} {_clock(t.exit_time)}, "
        f"{REASONS.get(t.reason, t.reason)}, net {t.net:+.2f} dolar" for t in found)


# Her kapanan parça kendi miktarıyla değerlendirilir. En iyi andaki açık kâr
# komisyonsuzdur, net sonuç komisyon ve fonlama dahildir, yakalama oranı bu
# ikisinin oranıdır. SL kapanıştaki son değerdir, iz süren stop taşıdıysa
# taşınmış halidir.
def _exit_report(t) -> tuple[str, float | None, float | None]:
    sign = t.side.sign
    peak = sign * (t.best_price - t.entry_price) * t.qty
    minutes = (t.exit_time - t.entry_time) // 60
    text = (f"#{t.number} {t.symbol} {t.side}, giriş {t.entry_price:g}, çıkış {t.exit_price:g}, "
            f"{REASONS.get(t.reason, t.reason)}, süre {minutes // 60} sa {minutes % 60} dk, net {t.net:+.2f} dolar. ")
    capture = t.net / peak * 100 if peak > 0 else None
    text += (f"En iyi fiyat {t.best_price:g}, o anda açık kâr {peak:+.2f} dolar, yakalama yüzde {capture:.0f}. "
             if capture is not None else f"En iyi fiyat {t.best_price:g}, en iyi anda da kârda değildi. ")
    missed = [k.price for k in t.tp_levels if sign * (k.price - t.best_price) > 0]
    gap = None
    if missed:
        nearest = min(missed) if sign > 0 else max(missed)
        gap = abs(nearest - t.best_price) / t.entry_price * 100
        text += f"Değmeyen en yakın TP {nearest:g}, fiyat yüzde {gap:.2f} kala döndü. "
    elif t.reason != "tp":
        text += "Değmeyen TP yoktu. "
    if t.sl:
        room = sign * (t.worst_price - t.sl) / t.entry_price * 100
        text += f"En kötü fiyat {t.worst_price:g}, SL'ye yüzde {max(room, 0):.2f} kaldı."
    else:
        text += f"En kötü fiyat {t.worst_price:g}, SL yoktu."
    return text, capture, gap


@server.tool()
def cikis_karnesi(adet: int = 20) -> str:
    """Kapanmış işlemlerinin çıkışlarını yeniden eskiye değerlendirir. Her işlem için en iyi fiyatı, o anda
    görülen açık kârı, net sonucun bu kârın yüzde kaçı olduğunu, yani yakalama oranını, değmeyen en yakın TP'ye
    ne kadar kala dönüldüğünü ve en kötü fiyatın SL'ye uzaklığını verir. En altta ortalama yakalama oranı ve
    TP'ye yüzde 0.6'dan az farkla varamayan işlem sayısı bulunur. Yüzdeler giriş fiyatına göredir.

    Args:
        adet: Değerlendirilecek son kapanmış işlem sayısı, bir ile yüz arası.
    """
    found = _trades(_clamp(adet, 1, 100))
    if not found:
        return "Kapanmış işlem yok."
    rows = [_exit_report(t) for t in found]
    captures = [c for _, c, _ in rows if c is not None]
    near = sum(1 for _, _, g in rows if g is not None and g < _NEAR_MISS_PCT)
    summary = (f"Özet: {len(rows)} işlem, "
               + (f"ortalama yakalama yüzde {sum(captures) / len(captures):.0f}, " if captures else "")
               + f"TP'ye yüzde {_NEAR_MISS_PCT}'dan az farkla varamayan işlem {near}.")
    return "\n".join(t for t, _, _ in rows) + "\n" + summary


if __name__ == "__main__":
    server.run("stdio")
