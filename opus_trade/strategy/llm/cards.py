"""
file: cards.py
importable: INTERVALS, LABELS, indicator_summary, summary_line, cards_text

Opus'a her çağrıda giden parite kartları. cards_text() profilin her paritesi
için aynı alanlardan oluşan bir kart kurar: fiyat ve değişim, hacim,
oynaklık, fonlama, trend, açık pozisyon, long short dağılımı,
likidasyonlar, yakın seviyeler, profilin yapı aralıklarındaki son tepe ve
dipler ve dört aralığın göstergeleri.
indicator_summary() bir mum listesinin sabit ayarlı gösterge özetini,
summary_line() onun Opus'un gördüğü tek satırını verir.

description: Opus'un istemindeki parite kartları.
"""

# NOTE: Her pariteye aynı veri gider. Pozisyonu ya da emri olan bir parite
# fazladan veri almaz, verilirse Opus'un dikkati bilgi düzeni yüzünden önceki
# seçimlerine kilitlenebilir. Daha uzun geçmişe Opus araçlarla kendisi bakar.
#
# NOTE: İstemde ham mum yoktur. 16.11'deki testte mumsuz istem aynı kararları
# yarı maliyetle verdi, Opus ihtiyaç duyduğu mumları araçla kendisi çekti.
#
# NOTE: Açık pozisyon ve long short verisi alınamazsa çağrı durmaz, kartta
# veri yok yazar. Altın gibi geleneksel varlıklarda Binance bu veriyi vermeyebilir.
#
# XXX: LABELS'taki adlar Opus'a görünür.

import asyncio
import logging
from pathlib import Path

from ...market import analysis, indicators, liquidations
from ...market.models import Candle
from ...market.source import MarketData, MarketDataError, RatioKind
from .texts import INTERVAL_NAMES, clock, price

log = logging.getLogger(__name__)

INTERVALS = ("15m", "1h", "4h", "1d")

# Kartta 1 saatlik grafikte fiyatın üstünde ve altında kaçar seviye gösterildiği.
_CARD_LEVELS = 2

# Yapı satırında her aralık için kaçar tepe ve dip yazıldığı. Opus şemaya son
# ikişerini yazar, üçüncüsü bir öncekiyle karşılaştırma için bağlamdır.
_SWINGS_SHOWN = 3

# Yapı satırındaki uçların iki yanında kaç mum olması gerektiği. seviyeler
# aracının varsayılanı beştir ve seviye bulmak içindir. Yapıda en yeni uç en
# az bu kadar mum önce oluşmuş olur, beş mum 1 saatlikte bir geri çekilmenin
# bittiğini beş saat geç gösterirdi.
_SWING_WINDOW = 3

LABELS = {
    "close": "kapanis", "ema20": "ema20", "ema50": "ema50", "ema200": "ema200", "rsi14": "rsi14", "macd": "macd",
    "macd_signal": "macd_sinyal", "macd_hist": "macd_histogram", "atr14": "atr14", "bb_upper": "bollinger_ust",
    "bb_lower": "bollinger_alt", "high_20": "son_20_mum_en_yuksek", "low_20": "son_20_mum_en_dusuk",
}


# EMA200 ancak yeterli mum varsa anlamlıdır, az mumla hesaplanan değer
# yanıltıcı olur ve verilmez.
def indicator_summary(candles: list[Candle]) -> dict[str, float]:
    """Mum listesinin sabit ayarlı gösterge özetini döndürür, değerler yuvarlanmıştır."""
    c = [m.close for m in candles]
    m = indicators.macd(c)
    bb = indicators.bollinger(c)
    out = {"close": c[-1], "ema20": indicators.ema(c, 20)[-1], "ema50": indicators.ema(c, 50)[-1],
           "rsi14": indicators.rsi(c)[-1], "macd": m["macd"][-1], "macd_signal": m["signal"][-1],
           "macd_hist": m["histogram"][-1], "atr14": indicators.atr(candles)[-1], "bb_upper": bb["upper"][-1],
           "bb_lower": bb["lower"][-1], "high_20": max(x.high for x in candles[-20:]),
           "low_20": min(x.low for x in candles[-20:])}
    if len(c) >= 200:
        out["ema200"] = indicators.ema(c, 200)[-1]
    return {k: indicators.rounded(v) for k, v in out.items() if v is not None}


def summary_line(s: dict[str, float]) -> str:
    return ", ".join(f"{LABELS[k]} {v}" for k, v in s.items())


async def _optional(job):
    try:
        return await job
    except MarketDataError as e:
        log.warning("Parite kartı için ek veri alınamadı: %s", e)
        return None


def _side(close: float, level: float) -> str:
    return "üstünde" if close > level else "altında"


def _levels_line(levels: list[analysis.Level]) -> str:
    return ", ".join(f"{price(x.price)} {x.touches} uç" for x in levels) or "yok"


def _swing_text(found: list[analysis.Swing], kind: str) -> str:
    picked = [x for x in found if x.kind == kind][-_SWINGS_SHOWN:]
    return ", ".join(f"{price(x.price)} {clock(x.time)}" for x in picked) or "yok"


# Eskiden yeniye yazılır, Opus şemaya da bu sırayla yazar.
def _structure_lines(lists: dict[str, list[Candle]], structure: tuple[str, ...]) -> list[str]:
    out = []
    for interval in structure:
        found = analysis.swings(lists[interval], _SWING_WINDOW)
        name = INTERVAL_NAMES[interval]
        name = name[0].upper() + name[1:]
        out.append(f"{name} son tepeler: {_swing_text(found, 'high')}. {name} son dipler: {_swing_text(found, 'low')}.")
    return out


async def _card(market: MarketData, liq_db: Path, symbol: str, last: float, now: int,
                structure: tuple[str, ...]) -> str:
    lists = dict(zip(INTERVALS, await asyncio.gather(*(market.candles(symbol, i, 250) for i in INTERVALS))))
    day, prem, oi, accounts, top, taker = await asyncio.gather(
        market.daily_stats(symbol), market.premium(symbol),
        _optional(market.open_interest_history(symbol, "1h", 25)),
        _optional(market.long_short(RatioKind.ACCOUNTS, symbol, "1h", 1)),
        _optional(market.long_short(RatioKind.TOP_POSITIONS, symbol, "1h", 1)),
        _optional(market.taker_volume(symbol, "1h", 24)))
    ind = {i: indicator_summary(m) for i, m in lists.items()}
    g4, g1d = ind["4h"], ind["1d"]
    days = lists["1d"]
    change_7d = (days[-1].close / days[-8].close - 1) * 100 if len(days) >= 8 else 0.0
    trend_1d = f"EMA50 {_side(g1d['close'], g1d['ema50'])}" + (
        f", EMA200 {_side(g1d['close'], g1d['ema200'])}" if "ema200" in g1d else "")
    p = prem[0]
    lines = [
        f"## {symbol}",
        f"Fiyat {price(last)}, mark fiyatı {price(p.mark)}. 24 saatte yüzde {day.change_pct:+.2f}, 7 günde "
        f"yüzde {change_7d:+.2f}. "
        f"24 saatlik hacim {day.quote_volume / 1e6:.0f} milyon dolar. Günlük oynaklık, ATR14, yüzde "
        f"{g1d['atr14'] / g1d['close'] * 100:.2f}.",
        f"Fonlama tahmini yüzde {p.est_funding * 100:.4f}, sıradaki fonlama {clock(p.next_funding)}.",
        f"4 saatlikte fiyat EMA20 {_side(g4['close'], g4['ema20'])}, EMA50 {_side(g4['close'], g4['ema50'])}. "
        f"Günlükte fiyat {trend_1d}.",
    ]
    if oi and len(oi) >= 2 and oi[0].qty:
        lines.append(f"Açık pozisyon {oi[-1].usd / 1e6:.0f} milyon dolar, 24 saatte yüzde "
                     f"{(oi[-1].qty / oi[0].qty - 1) * 100:+.2f}.")
    else:
        lines.append("Açık pozisyon: veri yok.")
    if accounts and top and taker:
        ratios = [x.buy_sell_ratio for x in taker]
        lines.append(f"Long hesapların oranı yüzde {accounts[-1].long_pct:.1f}, büyük hesaplarda pozisyonların yüzde "
                     f"{top[-1].long_pct:.1f} kadarı long. Agresif alışın satışa oranı son saatte {ratios[-1]:.2f}, "
                     f"24 saat ortalaması {sum(ratios) / len(ratios):.2f}.")
    else:
        lines.append("Long short dağılımı: veri yok.")
    con = liquidations.open_readonly(liq_db)
    try:
        tot = liquidations.totals(con, symbol, now - 86400, now)
        waves = liquidations.waves(con, symbol, now - 86400, now)
    finally:
        con.close()
    (ln, lu), (sn, su) = tot["long"], tot["short"]
    if ln or sn:
        text = (f"Son 24 saatin likidasyonları, örneklem: long {ln} kayıt {lu / 1e3:.1f} bin dolar, short {sn} kayıt "
                f"{su / 1e3:.1f} bin dolar.")
        w = max(waves, key=lambda x: x.long_usd + x.short_usd)
        text += (f" En büyük dalga {clock(w.start)}, long {w.long_usd / 1e3:.1f} bin, short {w.short_usd / 1e3:.1f} "
                 f"bin dolar, fiyat {price(w.low)} ile {price(w.high)} arası.")
        lines.append(text)
    else:
        lines.append("Son 24 saatte kayıtlı likidasyon yok.")
    found = analysis.levels(lists["1h"])
    above = [x for x in found if x.price > last][:_CARD_LEVELS]
    below = [x for x in found if x.price <= last][-_CARD_LEVELS:]
    lines.append(f"1 saatlik grafikte en yakın seviyeler, üstte: {_levels_line(above)}. "
                 f"Altta: {_levels_line(list(reversed(below)))}.")
    lines += _structure_lines(lists, structure)
    lines.append("### Göstergeler\n\n" + "\n".join(f"{i}: {summary_line(s)}" for i, s in ind.items()))
    return "\n".join(lines[:1]) + "\n\n" + "\n".join(lines[1:-1]) + "\n\n" + lines[-1]


async def cards_text(market: MarketData, liq_db: Path, symbols: tuple[str, ...], prices: dict[str, float],
                     now: int, structure: tuple[str, ...] = ("4h", "1h")) -> str:
    """Profilin bütün paritelerinin kartlarını tek bir Markdown bölümü olarak döndürür.

    structure profilin yapı aralıklarıdır, üstten alta. Her biri INTERVALS'tan biri olmalı.

    Raises:
        MarketDataError: Bir paritenin mumları, günlük özeti ya da premium verisi alınamazsa.
    """
    blocks = await asyncio.gather(*(_card(market, liq_db, s, prices[s], now, structure) for s in symbols))
    return ("# Parite kartları\n\nHer paritenin kartı aynı verileri aynı derinlikte taşır. Son tepe ve dipler "
            f"iki yanında {_SWING_WINDOW} mumdan uç olan mumlardır, eskiden yeniye. Daha uzun geçmişe ve diğer "
            "verilere piyasa araçlarıyla bakabilirsin.\n\n" + "\n\n".join(blocks))
