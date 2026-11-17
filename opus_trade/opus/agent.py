"""
file: agent.py
importable: system_prompt, schema, build_prompt, call, date_text, code_version, tools_digest, AgentError

Bütün fonksiyonlar hangi profil için çalıştıklarını ilk parametre olarak
alır. system_prompt() dönemin başlangıç ve bitiş tarihleriyle Opus'un sabit
talimatını kurar. build_prompt() piyasa özetini, ayrıntılı verileri, hesap
durumunu ve geçmiş kararları Opus'a gidecek metne çevirir. call() bu metni
claude komutuyla Opus'a gönderir, araç kullanımını izler ve şemaya uyan
kararı döndürür. Çağrı başarısız olursa AgentError fırlatır. date_text() bir
anı gün adı ve hafta içi ya da sonu bilgisiyle yazar.

description: Opus'a karar sorulan, araçlarının tanımlandığı ve cevabının okunduğu modül.
"""

# NOTE: Çağrılar bir Claude aboneliğinden, claude komutunun print moduyla
# yapılır. Kullanıcı ayarları, hook'lar ve kullanıcının MCP sunucuları
# yüklenmez, oturum diske yazılmaz. Opus'a yalnızca web arama, web sayfası
# okuma, Haiku araştırmacısı ve piyasa aracı verilir. Dosya okuma, yazma ve
# komut çalıştırma araçları bilerek verilmez.
#
# NOTE: Talimatın çekirdeği bütün profillerde ortak olan kısmıdır, değişirse
# bütün profillerin dönemi sıfırdan başlar. Profile özel olanlar profil
# dosyasından gelir: bakiye, pariteler, bekleme aralığı ve tarz metni. Tarz
# metni boş değilse talimata ayrı bir bölüm olarak girer. Eylemlerin
# açıklamaları çekirdekte değil opus/actions.py'deki şemada durur, böylece
# yeni bir eylem çekirdeğe dokunmaz.
#
# NOTE: Deney ya da sanal para kelimeleri bilerek kullanılmaz, paranın gerçek
# olduğu da söylenmez. Opus bir hesabı yönettiğini bilir. Tarih ve gün
# bilgisi yorum katılmadan, yalnızca bilgi olarak verilir.
#
# NOTE: Kodun geri kalanı İngilizcedir ama Opus'a giden her şey Türkçe kalır.
# Talimat, istem, karar şemasının alan adları ve eylem tipleri, araştırmacı
# ajanın adı bu yüzden Türkçedir. Talimat, istem ya da şemanın alanları değişirse
# profillerin dönemi sıfırdan başlatılmak zorunda kalır.
#
# XXX: Talimatta düşünce süreci ya da iç akıl yürütme gibi ifadeler
# kullanılmamalı. Opus'un güvenlik filtresi bunları akıl yürütmeyi dışarı
# çıkarma isteği sayıp çağrıyı reddediyor. Analiz ve gerekçe kelimeleri sorunsuz.

import asyncio
import hashlib
import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .. import settings
from ..market import hours as market_hours
from ..market import indicators
from . import actions

def date_text(t: int, weekday_info: bool = False) -> str:
    """Bir anı 15 Kasım 2026 Pazar, 05:00 biçiminde yazar.

    Args:
        t: Unix saniye.
        weekday_info: True ise sonuna hafta içi ya da hafta sonu eklenir.
    """
    z = time.localtime(t)
    text = f"{z.tm_mday} {settings.MONTHS[z.tm_mon - 1]} {z.tm_year} {settings.DAYS[z.tm_wday]}, {z.tm_hour:02d}:{z.tm_min:02d}"
    if weekday_info:
        text += ". Hafta sonu." if z.tm_wday >= 5 else ". Hafta içi."
    return text


# --- Opus'a giden sabit metinler ---

def _symbol_table(profile: settings.Profile) -> str:
    lines = ["sembol | açıklama | en fazla kaldıraç | en küçük işlem büyüklüğü | miktar adımı | tahmini kayma"]
    for sym, p in profile.symbols.items():
        lines.append(f"{sym} | {p.get('note', p['name'].split('/')[0])} | {p['max_leverage']}x | "
                     f"{p['min_notional']} dolar | {p['qty_step']} | yüzde {p['slippage'] * 100:.2f}")
    return "\n".join(lines)


def system_prompt(p: settings.Profile, start: int, end: int) -> str:
    """Opus'un sabit talimatını dönemin tarihleriyle döndürür.

    Args:
        p: Talimatın kurulduğu profil.
        start: Dönemin başladığı an, unix saniye.
        end: Dönemin biteceği an, unix saniye.
    """
    s = settings
    # Otonom profilde tarz boştur, talimata çalışma tarzı bölümü eklenmez.
    style = f"## Çalışma tarzın\n\n{p.style}\n\n" if p.style else ""
    return f"""Bir Binance USDT sürekli vadeli hesabını yönetiyorsun. Hesabın başlangıç bakiyesi \
{p.balance:.0f} dolar. Fiyatlar, fonlama oranları ve piyasa verileri Binance'in gerçek ve canlı verileridir.

## Hedefin

Tek amacın, dönem sonunda özkaynağını olabildiğince büyük yapmak. Performansın yalnızca dönem bitiminde \
ulaştığın özkaynakla ölçülür. Daha büyüğü mümkünken küçük bir kârla dönemi bitirmek başarı sayılmaz. Bakiyenin sıfırlanması hesabın kapanması demektir.

Dönem {date_text(start)} tarihinde başladı ve {date_text(end)} tarihinde bitecek. Bitişte açık pozisyonların \
o anki fiyattan kapatılır, bekleyen emirlerin ve alarmların iptal edilir. Bütün saatler {s.TIMEZONE_LABEL}.

{style}## İşlem yapabileceğin pariteler

{_symbol_table(p)}

Diğer paritelerin verisine piyasa araçlarıyla bakabilirsin ama yalnızca bu paritelerde işlem yapabilirsin.

## Nasıl çalışırsın

Her çağrıda kararını vermeden önce piyasayı kendin incelersin. Sana gönderilen veriler bir başlangıçtır, neye \
ihtiyacın varsa araçlarla kendin toplarsın.

- Her çağrıda tablodaki bütün pariteleri değerlendirirsin. Cevabındaki pariteler alanı bunun içindir. Bir pariteyi \
değerlendirmek orada işlem açman gerektiği anlamına gelmez, işlem açıp açmamak tamamen senin kararındır.
- Fiyatları yalnızca grafik hareket ettirmez. Haberler, makro veriler, merkez bankası kararları, düzenleyici \
gelişmeler ve büyük piyasaların açılış ve kapanışları da fiyatı etkiler. Bunları takip etmek senin işindir. Web \
araçları ve araştırmacı bunun için var, piyasa saatleri de her çağrıda sana verilir.
- Bir görüşün ya da açık pozisyonların varsa onları destekleyen verilerin yanında onlara aykırı verileri de arar \
ve tartarsın. Önceki çağrılarda vardığın sonuçlar, yeni verilerle yeniden sınanması gereken varsayımlardır.
- Elindeki araçların hepsi kullanman için var. Hangi sorunun cevabını hangi araçta arayacağına sen karar verirsin.

## Kurallar

- Tek bir dolar bakiyen var. Aynı anda istediğin kadar pozisyon açabilirsin, farklı paritelerde ya da aynı \
paritede, aynı yönde ya da ters yönde. Her pozisyonun bir numarası vardır.
- Her pozisyonun marjini izoledir. Likidasyonda yalnızca o pozisyonun marjini kaybedilir. Her pozisyonun \
likidasyon fiyatı sana bildirilir.
- Miktar, marjin çarpı kaldıracın fiyata bölümüdür ve paritenin miktar adımına aşağı yuvarlanır. Büyüklük, \
en küçük işlem büyüklüğünden ve bir miktar adımının değerinden az olamaz.
- Açık bir pozisyona ekleme yapabilir ya da bir kısmını kapatabilirsin. Eklemede giriş fiyatı ağırlıklı \
ortalamayla yeniden hesaplanır.
- Komisyon işlem büyüklüğü üzerinden kesilir. Fiyatın gelmesini bekleyen girişler, yani long için güncel \
fiyatın altındaki, short için üstündeki emirler ve TP kademeleri maker sayılır, yüzde \
{s.FEE_MAKER * 100:.2f} öder ve tam fiyatından gerçekleşir. Piyasa emirleri, kırılım yönündeki emirler, \
SL, iz süren stop ve senin kapatmaların taker sayılır, yüzde {s.FEE_TAKER * 100:.2f} öder ve tablodaki \
kadar aleyhine kayma ile gerçekleşir.
- Fonlama gerçek piyasadaki gibi uygulanır, çoğu paritede sekiz saatte bir, bazılarında dört saatte bir. Oran \
pozitifse long'lar öder, short'lar alır, negatifse tersi. Ödeme pozisyonun marjininden düşer ya da marjine \
eklenir ve likidasyon fiyatını değiştirir.
- Bir pozisyona tek bir TP ya da birden çok TP kademesi koyabilirsin. Her kademe bir fiyat ve pozisyonun yüzdesi \
taşır. Yüzdelerin toplamı en fazla 100 olur, toplam 100 ise son kademe kalanın tamamını kapatır.
- İz süren stop: Pozisyonun açıldığından beri gördüğü en iyi fiyattan verdiğin yüzde kadar geride durur ve fiyat \
lehine gittikçe SL'yi kendiliğinden taşır. Sonradan eklersen de bu en iyi fiyattan hesaplanır, her pozisyonun \
gördüğü en iyi fiyat sana bildirilir. Hesaplanan seviye fiyatın gerisinde kalacaksa, yani stop hemen \
tetiklenecekse, istek reddedilir. SL ile birlikte kullanılabilir, hangisi fiyata daha yakınsa o geçerlidir.
- TP, SL, iz süren stop ve likidasyon fiyat oraya değdiği an uygulanır, senin çağrılmanı beklemez. Bir mum içinde \
hem TP hem SL görülürse SL uygulanır.
- Bekleyen emir: Bir tetik fiyatı verirsin, fiyat oraya değince yeni bir pozisyon açılır. Tetik güncel fiyatın \
altındaysa fiyat düşüp değince, üstündeyse yükselip değince gerçekleşir. İstersen bir geçerlilik süresi \
verirsin, o süre içinde gerçekleşmezse emir iptal olur. Nakit, emir gerçekleştiği anda kontrol edilir.
- Fiyat alarmı: Bir paritede bir fiyat seviyesi ve kendine bir not verirsin. Fiyat oraya değince hemen çağrılırsın \
ve notunu görürsün. Alarm işlem açmaz.
- Sen yalnızca çağrıldığında karar verirsin. Bir sonraki çağrının kaç dakika sonra olacağını sen seçersin, \
{p.min_wait} ile {p.max_wait} dakika arası. Bir emrin gerçekleşmesi, emir süresinin dolması, TP, SL, \
likidasyon ya da bir alarm olduğunda beklemeden hemen çağrılırsın.
- Hesabı yürüten sistem zaman zaman kapanabilir. Kapanacağı önceden belliyse kapanmadan önce çağrılır ve haber \
verilirsin. Sistem kapalıyken çağrılamazsın ve alarmların seni çağıramaz. Emirlerin, TP, SL, iz süren stop ve \
likidasyon ise kapalıyken de işlemeye devam eder. Sistem açılınca kapalı kalınan süre ve o sürede olanlar sana \
bildirilir.
- Her çağrıda sıfırdan başlarsın, önceki konuşmayı hatırlamazsın. Sana son kararlarının özeti ve not defterin \
gelir. Not defterine bir sonraki çağrıda kendine lazım olacak her şeyi yaz. Not defteri her çağrıda senin \
yazdığınla tamamen değiştirilir.
- Hesabın bir yöneticinin gözetimindedir. Yönetici seni zamanından önce çağırabilir, sana bir bilgi ya da haber \
verebilir, soru sorabilir, hesabında işlem yapabilir ya da kısıt koyabilir. Her müdahale sana yöneticiden geldiği \
belirtilerek bildirilir. Teklif olarak gelenleri değerlendirirsin, mantıklı bulursan uygularsın. Kesin olarak \
gelenlere uyarsın. Kesin işlemler sana uygulanmış olarak bildirilir, kesin kısıtlar ise yürürlükte kaldıkça onları \
aşan eylemlerinin reddedilmesine yol açar. Yöneticinin sorularını ve verdiği haberleri yoneticiye_cevap alanında \
cevaplarsın.

## Araçların

Karar vermeden önce istediğin araçları istediğin kadar kullanabilirsin, bir çağrıda en fazla \
{s.TOOL_STEP_LIMIT} adım. Sınıra yaklaşınca kararını yaz.

- WebSearch ve WebFetch: Haber, makro gelişme, ekonomik takvim, piyasa yorumu ya da istediğin herhangi bir bilgi \
için web'de arama yapar ve sayfa okur.
- Agent ile arastirmaci: Bir araştırma görevini Haiku modeline devreder. Haiku web'de araştırır ve sana özet \
getirir. Bu, kendi bağlamını uzun sayfalarla doldurmadan geniş bir araştırma yapmanı sağlar.
- Piyasa araçları: Herhangi bir paritede istediğin aralıkta ve sayıda mum, istediğin ayarlarla gösterge hesabı, \
destek ve direnç seviyeleri, hacim profili, pariteler arası korelasyon, emir defteri, fonlama geçmişi, açık \
pozisyon geçmişi, long short oranları, likidasyonlar, kendi geçmiş kararların ve kapanmış işlemlerin. Likidasyon \
verisi Binance'in akışından örneklenir: her parite için saniyede yalnızca en büyük likidasyon kaydedilir, bu yüzden \
toplam tutarı değil dalgaların zamanını, yönünü ve büyüklüğünü gösterir.

## Eylemler

eylemler listesi boş olabilir, bu bekle demektir. Birden fazla eylem verirsen sırayla uygulanır, biri reddedilirse \
diğerleri yine denenir. Her eylemin sonucu bir sonraki çağrıda sana gösterilir. Her eylem tipinin ne yaptığı ve \
hangi alanları istediği cevap şemasında yazılıdır.

## Cevap

Bütün metinleri Türkçe yaz. analiz alanında piyasayı nasıl değerlendirdiğini, araçlardan ne öğrendiğini ve \
kararının gerekçelerini ayrıntılı açıkla. Bu alan senin işlem günlüğündür. pariteler alanında her parite için \
görüşünü ve bir iki cümlelik değerlendirmeni yaz. ozet tek cümle olsun. gorus piyasa hakkındaki genel kanaatin, \
guven ise kararına ne kadar güvendiğin, 0 ile 100 arası."""


# Ajanın adı Opus'a görünür, Agent aracını bu adla çağırır.
_RESEARCHER = {
    "arastirmaci": {
        "description": "Web'de haber ve piyasa araştırması yapıp özet getiren yardımcı. Ne araştırılacağını açıkça yaz.",
        "prompt": "Bir kripto ve emtia yatırımcısı için araştırma yapan yardımcısın. Verilen konuyu web'de araştır, "
                  "güncel ve güvenilir kaynaklara öncelik ver. Bulduklarını tarih ve kaynak belirterek, "
                  "kısa ve yoğun bir Türkçe özetle döndür. Yorum katmadan bulguları aktar.",
        "model": settings.RESEARCHER_MODEL,
        "tools": ["WebSearch", "WebFetch"],
    }
}

_VIEWS = ["yukselis", "dusus", "yatay", "belirsiz"]

_PAIR = {
    "type": "object", "additionalProperties": False, "required": ["gorus", "degerlendirme"],
    "properties": {"gorus": {"type": "string", "enum": _VIEWS}, "degerlendirme": {"type": "string"}},
}

# Eylem şemasındaki sembol listesi profilin paritelerinden kurulur, şemanın
# geri kalanı bütün profillerde aynıdır. Eylemlerin kendisi actions.py'dedir.
def schema(p: settings.Profile) -> dict:
    """Opus'un kararının uyması gereken JSON şemasını döndürür."""

    # Her parite ayrı ve zorunlu bir alan olduğu için Opus hiçbir pariteyi
    # değerlendirmeden geçemez.
    pairs = {
        "type": "object",
        "additionalProperties": False,
        "required": list(p.symbols),
        "properties": {sym: _PAIR for sym in p.symbols},
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["analiz", "pariteler", "ozet", "gorus", "guven", "eylemler", "not_defteri",
                     "sonraki_cagri_dk", "yoneticiye_cevap"],
        "properties": {
            "analiz": {"type": "string"},
            "pariteler": pairs,
            "ozet": {"type": "string"},
            "gorus": {"type": "string", "enum": _VIEWS},
            "guven": {"type": "integer"},
            "eylemler": {"type": "array", "items": actions.schema(list(p.symbols))},
            "not_defteri": {"type": "string"},
            "sonraki_cagri_dk": {"type": "integer"},
            "yoneticiye_cevap": {"type": "string", "description": "Yöneticinin sorularına, verdiği haberlere ve "
                                 "araştırmanı istediği konulara cevabın. Böyle bir müdahale yoksa boş bırak."},
        },
    }


# Opus çağrısı başarısız olduğunda fırlatılır. Mesajı günlüğe yazılır. O
# ana kadar kullanılan araçlar tools alanında taşınır, hatalı çağrının da
# araç kaydı kaybolmasın diye.
class AgentError(Exception):
    def __init__(self, message: str, tools: list[dict] | None = None) -> None:
        super().__init__(message)
        self.tools = tools or []


# --- İstem ---

def _clock(t: int) -> str:
    return time.strftime("%d.%m %H:%M", time.localtime(t))


# Bir günden kısa dönemlerde kalan süre dakikayla yazılır, yoksa Opus
# 0 gün 0 saat görür. Uzun dönemlerde biçim hep aynı kalır.
def _remaining(p: settings.Profile, sec: float) -> str:
    if p.period_s < 86400:
        return f"{int(max(sec, 0)) // 60} dakika"
    days, rest = divmod(int(max(sec, 0)), 86400)
    return f"{days} gün {rest // 3600} saat"


# Fiyatlar paritenin büyüklüğüne göre yeterli basamakla yazılır. XRP'de
# dört basamak gerekirken BTC'de bir basamak yeterli.
def _f(x: float | None) -> str:
    if x is None:
        return "yok"
    return f"{x:.1f}" if x >= 1000 else f"{x:.2f}" if x >= 10 else f"{x:.4f}"


def _candle_lines(candles: list[dict]) -> str:
    return "\n".join(
        f"{_clock(m['t'])}  {_f(m['o'])} {_f(m['h'])} {_f(m['l'])} {_f(m['c'])}  {m['v']:.1f}" for m in candles
    )


def _levels_text(levels: list[dict]) -> str:
    if not levels:
        return "yok"
    return ", ".join(f"{_f(k['price'])} yüzde {k['pct']:g}" for k in levels)


def _levels_line(levels: list[dict]) -> str:
    return ", ".join(f"{_f(x['price'])} {x['touches']} uç" for x in levels) or "yok"


# Bir paritenin kartının özet kısmı. Göstergeler ve mumlar build_prompt'ta
# ayrıca eklenir. Alınamayan veriler için veri yok yazılır.
def _card(o: dict) -> str:
    lines = [
        f"Fiyat {_f(o['price'])}. 24 saatte yüzde {o['change_pct']:+.2f}, 7 günde yüzde {o['change_7d']:+.2f}. "
        f"24 saatlik hacim {o['volume_usd'] / 1e6:.0f} milyon dolar. Günlük oynaklık, ATR14, yüzde {o['volatility']:.2f}.",
        f"Fonlama tahmini yüzde {o['funding'] * 100:.4f}, sıradaki fonlama {_clock(o['next_funding'])}.",
        f"4 saatlikte fiyat {o['trend_4h']}. Günlükte fiyat {o['trend_1d']}.",
    ]
    oi = o["open_interest"]
    lines.append(f"Açık pozisyon {oi['usd'] / 1e6:.0f} milyon dolar, 24 saatte yüzde {oi['change_pct']:+.2f}."
                 if oi else "Açık pozisyon: veri yok.")
    pos = o["positioning"]
    lines.append(f"Long hesapların oranı yüzde {pos['accounts_long']:.1f}, büyük hesaplarda pozisyonların yüzde "
                 f"{pos['top_long']:.1f} kadarı long. Agresif alışın satışa oranı son saatte {pos['taker_last']:.2f}, "
                 f"24 saat ortalaması {pos['taker_24h']:.2f}." if pos else "Long short dağılımı: veri yok.")
    liq, wave = o["liquidations"], o["liquidation_wave"]
    if liq["long"]["count"] or liq["short"]["count"]:
        text = (f"Son 24 saatin likidasyonları, örneklem: long {liq['long']['count']} kayıt "
                f"{liq['long']['usd'] / 1e3:.1f} bin dolar, short {liq['short']['count']} kayıt "
                f"{liq['short']['usd'] / 1e3:.1f} bin dolar.")
        if wave:
            text += (f" En büyük dalga {_clock(wave['start'])}, long {wave['long_usd'] / 1e3:.1f} bin, short "
                     f"{wave['short_usd'] / 1e3:.1f} bin dolar, fiyat {_f(wave['low'])} ile {_f(wave['high'])} arası.")
        lines.append(text)
    else:
        lines.append("Son 24 saatte kayıtlı likidasyon yok.")
    lines.append(f"1 saatlik grafikte en yakın seviyeler, üstte: {_levels_line(o['levels_above'])}. "
                 f"Altta: {_levels_line(list(reversed(o['levels_below'])))}.")
    return "\n".join(lines)


def build_prompt(p: settings.Profile, trigger: str, state: dict, market: dict, decisions: list[dict],
                 admin: dict | None = None) -> str:
    """Opus'a gönderilecek kullanıcı metnini kurar.

    Args:
        p: İstemin kurulduğu profil.
        trigger: Bu çağrının nedeni, insan okuyabilir biçimde.
        state: Engine.state() çıktısı.
        market: summary ve detail anahtarlarıyla piyasa verisi. summary her paritenin
            kart verisi, detail her parite için göstergeler ve son mumlardır.
        decisions: Eskiden yeniye son kararlar.
        admin: items ve restrictions anahtarlı Türkçe satırlar. items Opus'a henüz
            gösterilmemiş müdahaleler, restrictions yürürlükteki kısıtlardır.

    Returns:
        Markdown biçiminde tek bir metin.
    """
    now = int(time.time())
    prices = state["prices"]
    out = [f"# Çağrı\n\nŞu an {date_text(now, weekday_info=True)} {settings.TIMEZONE_LABEL}.\n"
         f"Dönemin bitmesine {_remaining(p, state['end_time'] - now)} var.\n\nÇağrılma nedeni: {trigger}"]

    # Yöneticinin bölümü yalnızca söylenecek bir şey varsa girer, müdahale
    # olmayan çağrılarda istem eskisiyle aynı kalır.
    admin = admin or {}
    if admin.get("items") or admin.get("restrictions"):
        block = ["# Yöneticiden"]
        if admin.get("items"):
            block.append("\n".join(f"- {x}" for x in admin["items"]))
        if admin.get("restrictions"):
            block.append("## Yürürlükteki kısıtlar\n\n" + "\n".join(f"- {x}" for x in admin["restrictions"]))
        out.append("\n\n".join(block))

    out.append(market_hours.status_text(datetime.now(timezone.utc)))
    out.append("# Parite kartları\n\nHer paritenin kartı aynı verileri aynı derinlikte taşır. Daha uzun geçmişe "
               "ve diğer verilere piyasa araçlarıyla bakabilirsin.")
    for sym, o in market["summary"].items():
        d = market["detail"][sym]
        block = [f"## {sym}", _card(o)]
        block.append("### Göstergeler\n\n" + "\n".join(
            f"{interval}: {indicators.as_line(summary)}" for interval, summary in d["indicators"].items()))
        for interval, candles in d["candles"].items():
            block.append(f"### Son {len(candles)} mum, {interval}\n\nsaat  açılış yüksek düşük kapanış  hacim\n"
                         + _candle_lines(candles))
        out.append("\n\n".join(block))

    st = state["stats"]
    a = [f"# Hesap\n\nNakit: {state['cash']:.2f} dolar\n"
         f"Özkaynak: {state['equity']:.2f} dolar, başlangıç {p.balance:.0f}, "
         f"getiri yüzde {state['return_pct']:+.2f}"]
    if state["positions"]:
        a.append("\nAçık pozisyonlar:")
        for x in state["positions"]:
            trail = f", iz süren stop yüzde {x['trail_pct']:g}" if x["trail_pct"] else ""
            a.append(
                f"- #{x['id']} {x['symbol']} {x['side']} {x['leverage']:g}x, miktar {x['qty']:g}, marjin "
                f"{x['margin']:.2f}, büyüklük {x['qty'] * x['entry_price']:.2f}, giriş {_f(x['entry_price'])}, "
                f"güncel {_f(prices[x['symbol']])}, gördüğü en iyi fiyat {_f(x['best_price'])}, açılış {_clock(x['entry_time'])}, "
                f"TP {_levels_text(x['tp_levels'])}, "
                f"SL {_f(x['sl'])}{trail}, likidasyon {_f(x['liq_price'])}, açık kâr/zarar {x['upnl']:+.2f}, "
                f"fonlama {-x['funding']:+.4f}"
            )
    else:
        a.append("\nAçık pozisyon yok.")
    if state["orders"]:
        a.append("\nBekleyen emirler:")
        for o in state["orders"]:
            valid = f", geçerlilik {_clock(o['expires_at'])}" if o["expires_at"] else ""
            a.append(
                f"- #{o['id']} {o['symbol']} {o['side']} tetik {_f(o['trigger_price'])} {o['leverage']}x marjin "
                f"{o['margin']:.2f}, {'maker' if o['maker'] else 'taker'}, TP {_levels_text(o['tp_levels'])}, "
                f"SL {_f(o['sl'])}{', iz yüzde ' + format(o['trail_pct'], 'g') if o['trail_pct'] else ''}{valid}"
            )
    else:
        a.append("\nBekleyen emir yok.")
    if state["alerts"]:
        a.append("\nAlarmlar:")
        for al in state["alerts"]:
            a.append(f"- #{al['id']} {al['symbol']} {_f(al['price'])}, not: {al['note'] or 'yok'}")
    else:
        a.append("\nAlarm yok.")
    a.append(f"\nKapanan işlemler: {st['trade_count']}, kazanan {st['wins']}, "
             f"toplam net {st['total_net']:+.2f} dolar, ödenen toplam komisyon {st['total_fee']:.2f}")
    out.append("\n".join(a))

    if decisions:
        lines = []
        for d in decisions:
            lines.append(f"- {_clock(d['time'])}: {d['summary']}")
            lines.extend(f"  - {r}" for r in d["results"])
        out.append("# Son kararların\n\n" + "\n".join(lines))

    out.append("# Not defterin\n\n" + (state["notebook"] or "Boş. Bu ilk çağrın olabilir."))
    return "\n\n".join(out)


# --- Çağrı ---

# claude'un stderr çıktısından saklanan son bayt sayısı. Hata mesajına
# bunun yalnızca son beş yüz karakteri girer.
_STDERR_KEEP = 8192

# claude komutu oturumu diske yazmasa da Haiku araştırmacısı kullanıldığında
# ~/.claude/projects altında çalışma klasörünün adıyla küçük bir alt ajan
# kaydı bırakıyor. Her çağrıdan sonra o çağrının klasörü silinir, klasör
# boş kalırsa o da silinir. Klasör adı çalışma yolundaki her / ve nokta
# yerine - konarak oluşur.
def _trace_dir(p: settings.Profile) -> Path:
    return Path.home() / ".claude" / "projects" / str(p.work_dir).replace("/", "-").replace(".", "-")


def _session_id(line: str) -> str | None:
    try:
        return json.loads(line).get("session_id")
    except (json.JSONDecodeError, AttributeError):
        return None


def _clean_trace(p: settings.Profile, session: str | None) -> None:
    d = _trace_dir(p)
    if session:
        shutil.rmtree(d / session, ignore_errors=True)
    if d.is_dir() and not any(d.iterdir()):
        d.rmdir()


# Bütün profillere aynı tek MCP sunucusu, piyasa, bağlanır. Komutun kendisi
# her profilin parmak izine girer, fingerprint.py içindeki yolları yer tutucuyla
# değiştirerek özetler.
def _command(p: settings.Profile, system: str) -> list[str]:
    env = {"PYTHONPATH": str(settings.ROOT), "TRADE_DB": str(p.db_path), "TRADE_MARKET_DB": str(settings.MARKET_DB)}
    python = str(settings.ROOT / ".venv" / "bin" / "python")
    servers = {"piyasa": {"type": "stdio", "command": python, "args": ["-m", "opus_trade.tools.market"], "env": env}}
    allowed = "WebSearch,WebFetch,Agent,mcp__piyasa"
    mcp = {"mcpServers": servers}
    return [
        "claude", "-p",
        "--model", settings.MODEL,
        "--effort", settings.EFFORT,
        "--no-session-persistence",
        "--setting-sources", "",
        "--strict-mcp-config",
        "--mcp-config", json.dumps(mcp),
        "--agents", json.dumps(_RESEARCHER, ensure_ascii=False),
        "--tools", "WebSearch,WebFetch,Agent",
        "--allowedTools", allowed,
        "--permission-mode", "dontAsk",
        "--max-turns", str(settings.HARD_STEP_LIMIT),
        "--system-prompt", system,
        "--output-format", "stream-json",
        "--verbose",
        "--json-schema", json.dumps(schema(p)),
    ]


# --- Kayıt bilgileri ---

# Kod sürümü paketin bütün Python dosyalarının özetidir. Sunucuda git yok,
# kod kopyalanarak gider, bu yüzden commit yerine içerik özetlenir. Program
# açıkken kod değişmediği için bir kez hesaplanır.
_VERSION: str | None = None


def code_version() -> str:
    """Paketteki bütün Python dosyalarının on iki karakterlik özetini döndürür."""
    global _VERSION
    if _VERSION is None:
        h = hashlib.sha256()
        for f in sorted((settings.ROOT / "opus_trade").rglob("*.py")):
            h.update(str(f.relative_to(settings.ROOT)).encode())
            h.update(f.read_bytes())
        _VERSION = h.hexdigest()[:12]
    return _VERSION


_TOOLS_DIGEST: str | None = None


# Araç havuzunun özeti Opus'un gördüğü list_tools çıktısından alınır. Ad,
# açıklama ya da parametre değişirse özet de değişir.
async def tools_digest() -> str:
    """Piyasa araçlarının Opus'a görünen tanımlarının on iki karakterlik özetini döndürür."""
    global _TOOLS_DIGEST
    if _TOOLS_DIGEST is None:
        from ..tools import market as market_mcp
        found = await market_mcp.server.list_tools()
        text = json.dumps([[x.name, x.description, x.input_schema] for x in found], ensure_ascii=False, sort_keys=True)
        _TOOLS_DIGEST = hashlib.sha256(text.encode()).hexdigest()[:12]
    return _TOOLS_DIGEST


# Araç sonucunun içeriği metin ya da metin blokları listesi olarak gelir.
def _result_text(content) -> str:
    if isinstance(content, list):
        return "\n".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return "" if content is None else str(content)


# Bir araç çağrısının arayüzde gösterilecek kısa hali. Girdinin tamamı
# ayrıca saklanır, burada yalnızca okunabilir bir başlık üretilir.
def _tool_record(block: dict, sub: bool) -> dict:
    name, inp = block["name"], block.get("input", {})
    if name == "WebSearch":
        title = inp.get("query", "")
    elif name == "WebFetch":
        title = inp.get("url", "")
    elif name == "Agent":
        title = inp.get("description") or inp.get("prompt", "")[:120]
    else:
        title = ", ".join(f"{k}={v}" for k, v in inp.items())
    name = name.replace("mcp__piyasa__", "piyasa.")
    return {"name": name, "title": title, "input": inp, "sub": sub, "id": block.get("id")}


async def call(p: settings.Profile, system: str, prompt: str, on_tool: Callable[[dict], None] | None = None) -> dict:
    """İstemi Opus'a gönderir, araç kullanımını izler ve kararı döndürür.

    Args:
        p: Çağrının yapıldığı profil.
        system: system_prompt() çıktısı.
        prompt: build_prompt() çıktısı.
        on_tool: Opus ya da araştırmacı her araç kullandığında araç kaydıyla çağrılır.

    Returns:
        decision, raw, tools, usage, duration_s ve cost_usd anahtarlı sözlük. cost_usd,
        aynı çağrının API liste fiyatıyla karşılığıdır, abonelikten ayrıca ücret alınmaz.
        tools içindeki her kayıt aracın sonucunu, hata bilgisini ve süresini de taşır.

    Raises:
        AgentError: Komut başarısız olursa, zaman aşımına ya da adım sınırına uğrarsa veya karar okunamazsa.
    """
    started = time.monotonic()
    # Çalışma klasörü profilin data altındaki klasörüdür, böylece claude proje
    # klasöründeki .claude ayarlarını ve notlarını görmez.
    p.work_dir.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        *_command(p, system), cwd=p.work_dir, limit=16 * 1024 * 1024,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    proc.stdin.write(prompt.encode())
    await proc.stdin.drain()
    proc.stdin.close()

    tools: list[dict] = []
    by_id: dict[str, dict] = {}
    lines: list[str] = []
    result: dict | None = None

    # Akış satır satır okunur. Her satır bir mesajdır. Araç kullanımları
    # geldikçe arayüze bildirilir, son satırdaki result mesajı kararı taşır.
    async def read() -> None:
        nonlocal result
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            lines.append(line)
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            if m.get("type") == "assistant":
                for block in m.get("message", {}).get("content", []):
                    if block.get("type") == "tool_use" and block.get("name") != "StructuredOutput":
                        rec = _tool_record(block, bool(m.get("parent_tool_use_id")))
                        rec["started"] = time.monotonic()
                        tools.append(rec)
                        by_id[rec["id"]] = rec
                        if on_tool:
                            on_tool({k: v for k, v in rec.items() if k != "started"})
            # Araç sonuçları claude'un araca cevabı olarak user mesajında gelir.
            # Süre, aracın istendiği satırla sonucun geldiği satır arasındaki zamandır.
            elif m.get("type") == "user":
                content = m.get("message", {}).get("content", [])
                for block in content if isinstance(content, list) else []:
                    rec = by_id.get(block.get("tool_use_id")) if isinstance(block, dict) else None
                    if rec is not None and block.get("type") == "tool_result":
                        rec["result"] = _result_text(block.get("content"))
                        rec["is_error"] = bool(block.get("is_error"))
                        rec["duration_s"] = round(time.monotonic() - rec["started"], 1)
            elif m.get("type") == "result":
                result = m

    # stderr de çağrı sürerken okunur. Okunmazsa claude çok yazdığında boru
    # dolar, claude yazmak için bekler ve çağrı zaman aşımına kadar takılır.
    # Hata mesajı için yalnızca sonu gerekir, baştan taşanı atılır.
    err = bytearray()

    async def read_err() -> None:
        while chunk := await proc.stderr.read(65536):
            err.extend(chunk)
            del err[:-_STDERR_KEEP]

    try:
        await asyncio.wait_for(asyncio.gather(read(), read_err(), proc.wait()), settings.CALL_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise AgentError(f"Opus {settings.CALL_TIMEOUT_S} saniye içinde karar vermedi, "
                         f"o ana kadar {len(tools)} araç kullandı.", _finish(tools))
    finally:
        for sid in {_session_id(x) for x in lines}:
            _clean_trace(p, sid)

    if result is None:
        text = err.decode(errors="replace")
        raise AgentError(f"claude komutu sonuç vermedi: {(text or chr(10).join(lines[-3:]))[-500:]}", _finish(tools))
    if result.get("subtype") == "error_max_turns":
        raise AgentError(f"Opus {settings.HARD_STEP_LIMIT} adım sınırına ulaştı ve karar yazmadı.", _finish(tools))
    if result.get("is_error") or not result.get("structured_output"):
        raise AgentError(f"Opus geçerli bir karar döndürmedi: {str(result.get('result'))[:500]}", _finish(tools))

    return {
        "decision": result["structured_output"],
        "raw": json.dumps(result, ensure_ascii=False),
        "tools": _finish(tools),
        "usage": {"usage": result.get("usage"), "model_usage": result.get("modelUsage"),
                  "num_turns": result.get("num_turns")},
        "duration_s": round(time.monotonic() - started, 1),
        "cost_usd": result.get("total_cost_usd"),
    }


# Sonucu hiç gelmeyen bir araç, örneğin zaman aşımında yarıda kalan, sonuçsuz
# kaydedilir. İç kullanımdaki başlangıç zamanı dışarı verilmez.
def _finish(tools: list[dict]) -> list[dict]:
    for rec in tools:
        rec.pop("started", None)
        rec.setdefault("result", None)
        rec.setdefault("is_error", None)
        rec.setdefault("duration_s", None)
    return tools
