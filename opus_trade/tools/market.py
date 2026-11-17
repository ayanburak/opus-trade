"""
file: market.py
importable: yok

Opus'un her çağrıda kullanabildiği piyasa araçlarını sunan MCP sunucusu.
Sunucuyu claude komutu kendisi başlatır, stdio üzerinden konuşur ve çağrı
bitince kapatır. Elle çalıştırılması gerekmez. Araçlar istenen aralıkta ve
sayıda mum, sabit ve istenen ayarlı göstergeler, destek ve direnç seviyeleri,
hacim profili, korelasyon, emir defteri, fonlama, açık pozisyon, long short
oranı, likidasyonlar, NY borsası açılışı ve Opus'un kendi geçmiş karar ve
işlemlerini döndürür.

description: Opus'un ihtiyaç duyduğu piyasa ve geçmiş verisini kendisinin seçerek almasını sağlayan araçlar.
"""

# NOTE: Veritabanı salt okunur açılır. Opus bu araçlarla hesabı değiştiremez,
# hesap yalnızca kararındaki eylemlerle değişir.
#
# NOTE: Araç çıktıları JSON yerine sıkı metin olarak döner. Aynı bilgi bu
# biçimde daha az token tutar ve Opus için okuması aynı derecede kolaydır.
#
# XXX: Opus araçları sunucu adı, araç adı, parametre adları ve docstring ile
# görür. MCP girdi ve çıktı şemalarının başlığını da fonksiyonun adından
# üretir, örneğin mumlarArguments. Bu yüzden araç fonksiyonlarının adları,
# parametreleri ve docstring'leri kodun geri kalanından farklı olarak
# Türkçedir. Bunlardan biri değişirse Opus'un gördüğü araç tanımı da değişir. Araç
# değişikliği dönemi sıfırlatmaz.

import asyncio
import json
import math
import os
import sqlite3
import statistics
import time
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from ..market import analysis, indicators, liquidations, opening
from ..market import data as market_data

server = MCPServer("piyasa")

_INTERVALS = ("1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w", "1M")
_PERIODS = ("5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d")

# seviyeler aracının fiyatın üstünde ve altında en fazla kaç seviye gösterdiği.
_LEVELS_SHOWN = 8

# Kapanış nedenleri veritabanında İngilizce durur, Opus eskiden beri Türkçe
# karşılıklarını görür.
_EXIT_REASONS = {"agent": "opus", "tp": "tp", "sl": "sl", "liquidation": "likidasyon", "period_end": "donem_sonu",
                 "admin": "yonetici"}


def _clock(t: int) -> str:
    return time.strftime("%d.%m.%Y %H:%M", time.localtime(t))


# Hangi profilin veritabanının okunacağını claude komutunu başlatan agent.py
# TRADE_DB ortam değişkeniyle, ortak likidasyon kayıtlarının yerini de
# TRADE_MARKET_DB ile bildirir.
def _db() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{os.environ['TRADE_DB']}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _n(v: float | None) -> str:
    return "yok" if v is None else f"{indicators.rounded(v):g}"


# --- Piyasa ---

@server.tool()
async def mumlar(sembol: str, aralik: str = "1h", adet: int = 100) -> str:
    """Vadeli piyasadan geçmiş mumları getirir. Katalog dışındaki pariteler de sorulabilir.

    Args:
        aralik: 1m, 3m, 5m, 15m, 30m, 1h, 2h, 4h, 6h, 8h, 12h, 1d, 3d, 1w ya da 1M.
        adet: Bir ile bin beş yüz arası.
        sembol: Binance USDT vadeli sembolü.
    """
    if aralik not in _INTERVALS:
        return f"Geçersiz aralık. Geçerli olanlar: {', '.join(_INTERVALS)}"
    items = await market_data.candles(aralik, _clamp(adet, 1, 1500), sembol.upper())
    return "saat  açılış yüksek düşük kapanış  hacim\n" + "\n".join(
        f"{_clock(m['t'])}  {m['o']} {m['h']} {m['l']} {m['c']}  {m['v']:.1f}" for m in items
    )


@server.tool()
async def gostergeler_hesapla(sembol: str, aralik: str = "1h") -> str:
    """Seçilen aralıkta EMA20, EMA50, EMA200, RSI14, MACD, ATR14 ve Bollinger değerlerini hesaplar.

    Args:
        aralik: mumlar aracındaki aralıklardan biri.
        sembol: Binance USDT vadeli sembolü.
    """
    if aralik not in _INTERVALS:
        return f"Geçersiz aralık. Geçerli olanlar: {', '.join(_INTERVALS)}"
    return indicators.as_text(indicators.summary(await market_data.candles(aralik, 300, sembol.upper())))


@server.tool()
async def emir_defteri(sembol: str, derinlik: int = 20) -> str:
    """Vadeli emir defterinin en iyi alış ve satış kademelerini getirir.

    Args:
        derinlik: 5, 10, 20, 50, 100, 500 ya da 1000.
        sembol: Binance USDT vadeli sembolü.
    """
    valid = (5, 10, 20, 50, 100, 500, 1000)
    derinlik = min(valid, key=lambda v: abs(v - derinlik))
    d = await market_data.request("/fapi/v1/depth", symbol=sembol.upper(), limit=derinlik)
    asks = "\n".join(f"  {p} : {q}" for p, q in reversed(d["asks"]))
    bids = "\n".join(f"  {p} : {q}" for p, q in d["bids"])
    return f"Satış kademeleri, fiyat : miktar\n{asks}\n---\nAlış kademeleri\n{bids}"


@server.tool()
async def fonlama_gecmisi(sembol: str, adet: int = 21) -> str:
    """Son gerçekleşen fonlama oranlarını getirir. Fonlama çoğu paritede sekiz saatte bir, bazılarında dört saatte bir olur.

    Args:
        adet: Bir ile bin arası.
        sembol: Binance USDT vadeli sembolü.
    """
    d = await market_data.request("/fapi/v1/fundingRate", symbol=sembol.upper(), limit=_clamp(adet, 1, 1000))
    return "\n".join(f"{_clock(x['fundingTime'] // 1000)}  yüzde {float(x['fundingRate']) * 100:.4f}" for x in d)


@server.tool()
async def acik_pozisyon_gecmisi(sembol: str, periyot: str = "1h", adet: int = 48) -> str:
    """Vadelideki toplam açık pozisyon miktarının geçmişini getirir. Binance yalnızca son otuz günü verir.

    Args:
        periyot: 5m, 15m, 30m, 1h, 2h, 4h, 6h, 12h ya da 1d.
        adet: Bir ile beş yüz arası.
        sembol: Binance USDT vadeli sembolü.
    """
    if periyot not in _PERIODS:
        return f"Geçersiz periyot. Geçerli olanlar: {', '.join(_PERIODS)}"
    d = await market_data.request("/futures/data/openInterestHist", symbol=sembol.upper(), period=periyot,
                                  limit=_clamp(adet, 1, 500))
    return "\n".join(f"{_clock(x['timestamp'] // 1000)}  {float(x['sumOpenInterest']):.2f} birim, {float(x['sumOpenInterestValue']) / 1e6:.1f} milyon dolar" for x in d)


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
    if periyot not in _PERIODS:
        return f"Geçersiz periyot. Geçerli olanlar: {', '.join(_PERIODS)}"
    adet = _clamp(adet, 1, 500)
    p = {"symbol": sembol.upper(), "period": periyot, "limit": adet}
    everyone = await market_data.request("/futures/data/globalLongShortAccountRatio", **p)
    top = await market_data.request("/futures/data/topLongShortPositionRatio", **p)
    taker = await market_data.request("/futures/data/takerlongshortRatio", **p)
    if not everyone:
        return f"{sembol} için long short verisi yok."
    lines = ["saat  tüm_hesaplar_long_yüzde  büyük_hesaplar_long_yüzde  taker_alış_satış"]
    for a, b, c in zip(everyone, top, taker):
        lines.append(f"{_clock(a['timestamp'] // 1000)}  {float(a['longAccount']) * 100:.1f}  "
                     f"{float(b['longAccount']) * 100:.1f}  {float(c['buySellRatio']):.3f}")
    return "\n".join(lines)


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
    if aralik not in _INTERVALS:
        return f"Geçersiz aralık. Geçerli olanlar: {', '.join(_INTERVALS)}"
    adet = _clamp(adet, 1, 500)
    # vwap baştan birikir, diğer göstergeler ısınmak için önceden yeterli mum ister.
    count = adet if ad == "vwap" else min(1500, adet + 500)
    candles = await market_data.candles(aralik, count, sembol.upper())
    try:
        found = indicators.series(ad, candles, tuple(periyotlar or ()))
    except ValueError as e:
        return str(e)
    names = list(found)
    lines = ["saat  kapanış  " + "  ".join(names)]
    for i in range(max(0, len(candles) - adet), len(candles)):
        lines.append(f"{_clock(candles[i]['t'])}  {_n(candles[i]['c'])}  "
                     + "  ".join(_n(found[k][i]) for k in names))
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
    if aralik not in _INTERVALS:
        return f"Geçersiz aralık. Geçerli olanlar: {', '.join(_INTERVALS)}"
    candles = await market_data.candles(aralik, _clamp(adet, 50, 1500), sembol.upper())
    found = analysis.levels(candles, _clamp(pencere, 1, 20))
    if not found:
        return "Bu aralıkta belirgin bir tepe ya da dip bulunamadı."
    # Fiyata en yakın seviyeler gösterilir, uzaktakiler o anki kararla ilgisiz
    # kalıp çıktıyı şişiriyor.
    price = candles[-1]["c"]
    above = [lv for lv in found if lv["price"] > price][:_LEVELS_SHOWN]
    below = [lv for lv in found if lv["price"] <= price][-_LEVELS_SHOWN:]
    lines = [f"güncel fiyat {_n(price)}. {len(found)} seviye bulundu, fiyata en yakın {len(above)} üst ve "
             f"{len(below)} alt seviye gösteriliyor.", "seviye  konum  uç_sayısı  ilk  son"]
    for lv in sorted(above + below, key=lambda x: x["price"], reverse=True):
        side = "üstte" if lv["price"] > price else "altta"
        lines.append(f"{_n(lv['price'])}  {side}  {lv['touches']}  {_clock(lv['first'])}  {_clock(lv['last'])}")
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
    if aralik not in _INTERVALS:
        return f"Geçersiz aralık. Geçerli olanlar: {', '.join(_INTERVALS)}"
    candles = await market_data.candles(aralik, _clamp(adet, 10, 1500), sembol.upper())
    vp = analysis.volume_profile(candles, _clamp(kademe, 5, 100))
    total = sum(r["volume"] for r in vp["rows"]) or 1
    lines = [f"{_clock(candles[0]['t'])} ile {_clock(candles[-1]['t'])} arası, güncel fiyat {_n(candles[-1]['c'])}",
             f"en yoğun kademe {_n(vp['poc'])}, değer alanı {_n(vp['value_low'])} ile {_n(vp['value_high'])}",
             "kademe  hacmin_yüzdesi"]
    for r in reversed(vp["rows"]):
        lines.append(f"{_n(r['low'])}-{_n(r['high'])}  {r['volume'] / total * 100:.1f}")
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
    if aralik not in _INTERVALS:
        return f"Geçersiz aralık. Geçerli olanlar: {', '.join(_INTERVALS)}"
    names = list(dict.fromkeys(x.upper() for x in semboller))
    if not 2 <= len(names) <= 10:
        return "İki ile on arası sembol verilmeli."
    count = _clamp(adet, 10, 1500)
    found = await asyncio.gather(*(market_data.candles(aralik, count, s) for s in names))
    pairs = analysis.correlation(dict(zip(names, found)))
    lines = ["çift  korelasyon  ortak_mum"]
    for (a, b), (r, n) in pairs.items():
        lines.append(f"{a} {b}  {'hesaplanamadı' if math.isnan(r) else f'{r:.2f}'}  {n}")
    return "\n".join(lines)


@server.tool()
def likidasyonlar(sembol: str, saat: int = 24) -> str:
    """Bir paritede son saatlerde zorla kapatılan pozisyonları dalgalar halinde getirir.

    Kayıtlar Binance'in canlı likidasyon akışından tutulur. Binance bu akışta
    her parite için saniyede yalnızca en büyük likidasyonu gönderir, bu yüzden
    tutarlar toplamın tamamı değil bir örneklemidir. Dalgaların zamanı, yönü
    ve büyüklük sırası doğrudur. Aralarında iki dakikadan az boşluk olan
    kayıtlar bir dalga sayılır. En fazla yedi gün geriye bakılabilir, sistemin kapalı
    kaldığı aralıklarda kayıt yoktur. Yalnızca işlem yapabildiğin paritelerde tutulur.

    Args:
        sembol: İşlem yapabildiğin paritelerden biri.
        saat: Kaç saat geriye bakılacağı, bir ile yüz altmış sekiz arası.
    """
    since = int(time.time()) - _clamp(saat, 1, 168) * 3600
    con = liquidations.connect(Path(os.environ["TRADE_MARKET_DB"]), readonly=True)
    try:
        tot = liquidations.totals(con, sembol.upper(), since)
        found = liquidations.waves(con, sembol.upper(), since)
    finally:
        con.close()
    if not found:
        return f"Son {saat} saatte {sembol.upper()} için kayıtlı likidasyon yok."
    lines = [f"long likidasyonları {tot['long']['count']} kayıt, {tot['long']['usd'] / 1e3:.1f} bin dolar. "
             f"short likidasyonları {tot['short']['count']} kayıt, {tot['short']['usd'] / 1e3:.1f} bin dolar.",
             "başlangıç  bitiş  kayıt  long_bin_dolar  short_bin_dolar  fiyat_aralığı  en_büyük"]
    for w in found:
        big = w["largest"]
        lines.append(f"{_clock(w['start'])}  {_clock(w['end'])[-5:]}  {w['count']}  {w['long_usd'] / 1e3:.1f}  "
                     f"{w['short_usd'] / 1e3:.1f}  {_n(w['low'])}-{_n(w['high'])}  "
                     f"{big['side']} {big['usd'] / 1e3:.1f} bin dolar @ {_n(big['price'])}")
    return "\n".join(lines)


# --- NY açılışı ---

def _pct(x: float | None) -> str:
    return "alınamadı" if x is None else f"yüzde {x * 100:+.2f}"


def _hunt_row(x: dict) -> str:
    return (f"{x['date']:%d.%m.%Y}  {_pct(x['nasdaq_6h'])}  {'yukarı' if x['side'] > 0 else 'aşağı'}  "
            f"yüzde {x['size'] * 100:.2f}  {x['peak_min']}  yüzde {min(x['giveback'], 9.99) * 100:.0f}")


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
    sembol = sembol.upper()
    now = time.time()
    o = opening.next_open(now)
    moves, past, vol = await asyncio.gather(opening.index_moves(now), opening.history(sembol, 20),
                                            opening.volatility(sembol))
    lines = [f"NY borsasının bir sonraki açılışı {_clock(o)}, {int((o - now) // 60)} dakika sonra."]
    for m in moves:
        lines.append(f"{m['name']}, {m['symbol']}: son 6 saatte {_pct(m['six_hours'])}, "
                     f"önceki NY kapanışından bu yana {_pct(m['since_close'])}.")
    if not past:
        lines.append(f"{sembol}: geçmiş açılış verisi alınamadı.")
        return "\n".join(lines)
    ratio = statistics.median(x["size"] / x["vol"] for x in past)
    avg_vol = statistics.mean(x["vol"] for x in past)
    lines.append(
        f"{sembol}: önceki 24 saatin 1 dakikalık ortalama aralığı yüzde {vol * 100:.3f}, son {len(past)} açılışın "
        f"ortalamasının {vol / avg_vol:.2f} katı. Son {len(past)} açılışta av büyüklüğü medyan yüzde "
        f"{statistics.median(x['size'] for x in past) * 100:.2f}. Beklenen av yaklaşık yüzde {ratio * vol * 100:.2f}.")
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
    found = await opening.history(sembol.upper(), _clamp(gun, 1, 60))
    if not found:
        return f"{sembol.upper()} için açılış verisi bulunamadı."
    return "tarih  nasdaq_03-09  av_yönü  av  tepeye_dk  ilk_saatte_geri_verilen  oynaklık_24s\n" + "\n".join(
        f"{_hunt_row(x)}  yüzde {x['vol'] * 100:.3f}" for x in found)


# --- Geçmiş ---

@server.tool()
def gecmis_kararlarim(adet: int = 20, atla: int = 0, analiz_dahil: bool = False) -> str:
    """Bu dönemdeki kendi geçmiş kararlarını yeniden eskiye getirir.

    Args:
        adet: Bir ile yüz arası.
        atla: En yeni kaç kararın atlanacağı. Daha eski kararlara sayfa sayfa ulaşmak için.
        analiz_dahil: True ise her kararın tam analiz metni de gelir.
    """
    with _db() as con:
        found = con.execute(
            "SELECT time, decision, action_results FROM journal WHERE decision IS NOT NULL "
            "ORDER BY id DESC LIMIT ? OFFSET ?", (_clamp(adet, 1, 100), max(0, atla)),
        ).fetchall()
    if not found:
        return "Kayıtlı karar yok."
    parts = []
    for r in found:
        k = json.loads(r["decision"])
        result = "; ".join(x["message"] for x in json.loads(r["action_results"])) or "eylem yok"
        text = f"{_clock(r['time'])}, görüş {k['gorus']}, güven {k['guven']}\n" \
               f"  özet: {k['ozet']}\n  sonuç: {result}"
        if analiz_dahil:
            text += f"\n  analiz: {k['analiz']}"
        parts.append(text)
    return "\n\n".join(parts)


@server.tool()
def gecmis_islemlerim(adet: int = 50) -> str:
    """Bu dönemde kapanmış işlemlerini yeniden eskiye getirir. Net sonuç komisyon ve fonlama dahildir.

    Args:
        adet: Bir ile beş yüz arası.
    """
    with _db() as con:
        found = con.execute(
            "SELECT * FROM trades WHERE status = 'closed' ORDER BY exit_time DESC LIMIT ?",
            (_clamp(adet, 1, 500),),
        ).fetchall()
    if not found:
        return "Kapanmış işlem yok."
    return "\n".join(
        f"#{r['id']}{' kısmi, ana #' + str(r['parent_id']) if r['parent_id'] else ''} {r['symbol']} {r['side']} "
        f"{r['leverage']}x marjin {r['margin']:.2f}, giriş {r['entry_price']:.2f} {_clock(r['entry_time'])}, "
        f"çıkış {r['exit_price']:.2f} {_clock(r['exit_time'])}, neden {_EXIT_REASONS[r['exit_reason']]}, "
        f"net {r['pnl'] - r['fee']:+.2f} dolar"
        for r in found
    )



# Bir TP'ye bu yüzdeden az farkla varamayan işlem karnenin özetinde ayrıca
# sayılır. Eşik 17.11 analizinden gelir, orada kaçan altı hedefin altısı da
# bu farkın içindeydi.
_NEAR_MISS_PCT = 0.6


# Her kapanmış kayıt, kısmi kapanışlarla ayrılan parçalar da, kendi miktarıyla
# ayrı ayrı değerlendirilir. Parçalar ana pozisyonun en iyi ve en kötü
# fiyatını kopyalayarak taşır. En iyi andaki açık kâr komisyonsuzdur, net
# sonuç komisyon ve fonlama dahildir, yakalama oranı bu ikisinin oranıdır. SL
# alanı kapanıştaki son değerdir, iz süren stop taşıdıysa taşınmış halidir.
# Hesap kapanışı tetikleyen aralığı en iyi ve en kötü fiyata yazmadan kapatır.
# Bu yüzden çıkış fiyatı da hesaba katılır, yoksa TP'yle kapanan bir işlemin
# en iyi fiyatı TP'nin altında, SL'yle kapananın en kötüsü SL'nin üstünde görünürdü.
def _exit_report(r) -> tuple[str, float | None, float | None]:
    long = r["side"] == "long"
    sign = 1 if long else -1
    best = max(r["best_price"], r["exit_price"]) if long else min(r["best_price"], r["exit_price"])
    worst = min(r["worst_price"], r["exit_price"]) if long else max(r["worst_price"], r["exit_price"])
    net = r["pnl"] - r["fee"]
    peak = sign * (best - r["entry_price"]) * r["qty"]
    minutes = (r["exit_time"] - r["entry_time"]) // 60
    text = (f"#{r['id']} {r['symbol']} {r['side']}, giriş {r['entry_price']:g}, çıkış {r['exit_price']:g}, "
            f"neden {_EXIT_REASONS[r['exit_reason']]}, süre {minutes // 60} sa {minutes % 60} dk, net {net:+.2f} dolar. ")
    capture = net / peak * 100 if peak > 0 else None
    text += (f"En iyi fiyat {best:g}, o anda açık kâr {peak:+.2f} dolar, yakalama yüzde {capture:.0f}. "
             if capture is not None else f"En iyi fiyat {best:g}, en iyi anda da kârda değildi. ")
    missed = [lv["price"] for lv in json.loads(r["tp_levels"]) if sign * (lv["price"] - best) > 0]
    gap = None
    if missed:
        nearest = min(missed) if long else max(missed)
        gap = abs(nearest - best) / r["entry_price"] * 100
        text += f"Değmeyen en yakın TP {nearest:g}, fiyat yüzde {gap:.2f} kala döndü. "
    elif r["exit_reason"] != "tp":
        text += "Değmeyen TP yoktu. "
    if r["sl"]:
        room = sign * (worst - r["sl"]) / r["entry_price"] * 100
        text += f"En kötü fiyat {worst:g}, SL'ye yüzde {max(room, 0):.2f} kaldı."
    else:
        text += f"En kötü fiyat {worst:g}, SL yoktu."
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
    with _db() as con:
        found = con.execute(
            "SELECT * FROM trades WHERE status = 'closed' ORDER BY exit_time DESC LIMIT ?",
            (_clamp(adet, 1, 100),),
        ).fetchall()
    if not found:
        return "Kapanmış işlem yok."
    rows = [_exit_report(r) for r in found]
    captures = [c for _, c, _ in rows if c is not None]
    near = sum(1 for _, _, g in rows if g is not None and g < _NEAR_MISS_PCT)
    summary = (f"Özet: {len(rows)} işlem, "
               + (f"ortalama yakalama yüzde {sum(captures) / len(captures):.0f}, " if captures else "")
               + f"TP'ye yüzde {_NEAR_MISS_PCT}'dan az farkla varamayan işlem {near}.")
    return "\n".join(t for t, _, _ in rows) + "\n" + summary

if __name__ == "__main__":
    server.run("stdio")
