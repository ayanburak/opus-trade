"""
file: prompt.py
importable: TEMPLATE_VERSION, VIEWS, system_prompt, schema, wake_text, build_prompt

Opus'un talimatı, karar şeması ve her çağrının istemi. system_prompt()
profilin bakiyesi, pariteleri, bekleme aralığı ve tarz metniyle talimatı
kurar. schema() kararın JSON şemasını profilin paritelerine göre kurar.
build_prompt() uyandırma nedenlerini, yönetici bölümünü, piyasa saatlerini,
parite kartlarını, hesabı, son kararları ve not defterini tek bir metinde
toplar. TEMPLATE_VERSION talimatın ortak çekirdeğinin sürümüdür.

description: Opus'un talimatı, karar şeması ve istemi.
"""

# NOTE: Talimatın çekirdeği bütün LLM profillerinde ortaktır. Değişince
# TEMPLATE_VERSION artırılır, günlük her çağrının hangi sürümle yapıldığını
# kaydeder ve sonuçlar sürüme göre ayrılarak incelenir. Hiçbir profil
# sıfırlanmaz.
#
# NOTE: Deney ya da sanal para kelimeleri bilerek kullanılmaz, paranın gerçek
# olduğu da söylenmez. Opus bir hesabı yönettiğini bilir. Tarih ve gün bilgisi
# yorum katılmadan verilir. Dönem ve bitiş tarihi yoktur, hedef özkaynağı
# olabildiğince büyütmektir.
#
# NOTE: Eylemlerin açıklamaları talimatta değil actions.py'deki şemadadır, yeni
# bir eylem çekirdeğe dokunmaz.
#
# XXX: Talimatta düşünce süreci ya da iç akıl yürütme gibi ifadeler
# kullanılmamalı. Opus'un güvenlik filtresi bunları akıl yürütmeyi dışarı
# çıkarma isteği sayıp çağrıyı reddediyor. Analiz ve gerekçe kelimeleri sorunsuz.

from datetime import datetime, timezone

from ...domain.fees import FeeSchedule
from ...domain.models import SymbolSpec
from ...domain.trading import AccountEvent
from ...triggers.base import Fire
from ..base import ActionResult, Context, Wake
from . import actions
from .hours import hours_text
from .texts import TZ_LABEL, clock, date_text, event_text, fire_text, levels_text, price

TEMPLATE_VERSION = 4

VIEWS = ["yukselis", "dusus", "yatay", "belirsiz"]


def _symbol_table(specs: list[SymbolSpec]) -> str:
    lines = ["sembol | açıklama | en fazla kaldıraç | en küçük işlem büyüklüğü | miktar adımı | fiyat adımı | "
             "tahmini kayma"]
    for s in specs:
        lines.append(f"{s.symbol} | {s.note or s.name.split('/')[0]} | {s.brackets[0].max_leverage}x | "
                     f"{s.min_notional:g} dolar | {s.qty_step:g} | {s.tick:g} | yüzde {s.slippage * 100:.2f}")
    return "\n".join(lines)


def system_prompt(balance: float, specs: list[SymbolSpec], fees: FeeSchedule, style: str, min_wait: int,
                  max_wait: int, step_limit: int) -> str:
    """Opus'un talimatını döndürür.

    Args:
        balance: Hesabın başlangıç bakiyesi, dolar.
        specs: Profilin işlem yapabildiği paritelerin kuralları.
        fees: Hesabın komisyon oranları.
        style: Profilin tarz metni, boşsa çalışma tarzı bölümü eklenmez.
        min_wait: Opus'un seçebileceği en kısa bekleme, dakika.
        max_wait: En uzun bekleme, dakika.
        step_limit: Opus'a bildirilen araç adımı sınırı.
    """
    style_part = f"## Çalışma tarzın\n\n{style}\n\n" if style else ""
    return f"""Bir Binance USDT sürekli vadeli hesabını yönetiyorsun. Hesabın başlangıç bakiyesi {balance:g} dolar. \
Fiyatlar, fonlama oranları ve piyasa verileri Binance'in gerçek ve canlı verileridir.

## Hedefin

Tek amacın özkaynağını olabildiğince büyütmek. Küçük bir kârla yetinmek başarı sayılmaz. Bakiyenin sıfırlanması \
hesabın kapanması demektir. Bütün saatler {TZ_LABEL}.

{style_part}## İşlem yapabileceğin pariteler

{_symbol_table(specs)}

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
paritede, aynı yönde ya da ters yönde.
- Pozisyonların ve bekleyen emirlerin aynı numaralandırmayı paylaşır. Numara, o an açık olan pozisyon ve emirlerin \
kullanmadığı en küçük sayıdır. Kapanan bir kaydın numarası sonra yeni bir kayda verilebilir. Bir emir dolunca \
açılan pozisyon emrin numarasını alır. Bu yüzden numarayla bir kayda dokunan her eylemde sembolü ve yönü de \
yazarsın. Numara artık o sembol ve yöndeki kaydı göstermiyorsa eylem reddedilir.
- Her pozisyonun marjini izoledir. Likidasyonda yalnızca o pozisyonun marjini kaybedilir. Her pozisyonun \
likidasyon fiyatı sana bildirilir.
- Miktar, marjin çarpı kaldıracın fiyata bölümüdür ve paritenin miktar adımına aşağı yuvarlanır. Büyüklük, \
en küçük işlem büyüklüğünden ve bir miktar adımının değerinden az olamaz. Verdiğin fiyatlar, yani tetik, TP ve \
SL, paritenin fiyat adımına en yakın yönde yuvarlanır. Kaldıraç tam sayıdır.
- Açık bir pozisyona ekleme yapabilir ya da bir kısmını kapatabilirsin. Eklemede giriş fiyatı ağırlıklı \
ortalamayla yeniden hesaplanır.
- Komisyon işlem büyüklüğü üzerinden kesilir. Fiyatın gelmesini bekleyen girişler, yani long için güncel \
fiyatın altındaki, short için üstündeki emirler ve TP kademeleri maker sayılır, yüzde {fees.maker * 100:.2f} öder \
ve tam fiyatından gerçekleşir. Piyasa emirleri, kırılım yönündeki emirler, SL, iz süren stop ve senin kapatmaların \
taker sayılır, yüzde {fees.taker * 100:.2f} öder ve tablodaki kadar aleyhine kayma ile gerçekleşir.
- Fonlama gerçek piyasadaki gibi uygulanır, çoğu paritede sekiz saatte bir, bazılarında dört saatte bir. Oran \
pozitifse long'lar öder, short'lar alır, negatifse tersi. Ödeme pozisyonun marjininden düşer ya da marjine \
eklenir ve likidasyon fiyatını değiştirir.
- Bir pozisyona tek bir TP ya da birden çok TP kademesi koyabilirsin. Her kademe bir fiyat ve pozisyonun yüzdesi \
taşır. Yüzdelerin toplamı en fazla 100 olur, toplam 100 ise son kademe kalanın tamamını kapatır. Bir kademenin \
miktarı pozisyon miktarının o yüzdesidir, miktar adımına aşağı yuvarlanır ve en az bir miktar adımı olmalıdır. \
Küçük bir pozisyonda kademe sayısını azaltırsın ya da tek TP kullanırsın, yoksa istek reddedilir. Kademenin \
miktarı kurulurken sabitlenir. Pozisyon kısmen kapanınca, ekleme yapılınca ya da bir kademe dolunca hesap \
bölümündeki yüzdeler kademenin kalan pozisyondaki güncel payını gösterir.
- İz süren stop: Pozisyonun açıldığından beri gördüğü en iyi mark fiyatından verdiğin yüzde kadar geride durur \
ve fiyat lehine gittikçe SL'yi kendiliğinden taşır. Yüzde 0.1 ile 10 arasında olur. Sonradan eklersen de bu en \
iyi fiyattan hesaplanır, her pozisyonun gördüğü en iyi mark fiyatı sana bildirilir. Hesaplanan seviye fiyatın \
gerisinde kalacaksa, yani stop hemen tetiklenecekse, istek reddedilir. SL ile birlikte kullanılabilir, hangisi \
fiyata daha yakınsa o geçerlidir.
- TP son işlem fiyatı oraya değdiği an uygulanır. SL, iz süren stop ve likidasyon ise mark fiyatına bakar, mark \
fiyatı oraya değdiği an uygulanır. Mark fiyatı Binance'in birçok borsanın fiyatından hesapladığı adil fiyattır, \
yalnızca bu borsada görülen kısa iğneler onu oynatmaz. Grafikteki mumlar son işlem fiyatıdır, bir iğnenin SL'ye \
değmesi SL'yi çalıştırmayabilir. SL tetiklendiği andaki son işlem fiyatından gerçekleşir, fiyat sıçradıysa \
seviyesinden kötü olabilir. Hiçbiri senin çağrılmanı beklemez. Bir mum içinde hem TP hem SL görülürse SL uygulanır.
- Bekleyen emir: Bir tetik fiyatı verirsin, fiyat oraya değince yeni bir pozisyon açılır. Tetik güncel fiyatın \
altındaysa fiyat düşüp değince, üstündeyse yükselip değince gerçekleşir. İstersen bir geçerlilik süresi \
verirsin, o süre içinde gerçekleşmezse emir iptal olur. Nakit, emir gerçekleştiği anda kontrol edilir.
- Fiyat alarmı: Bir paritede bir fiyat seviyesi ve kendine bir not verirsin. Fiyat oraya değince hemen çağrılırsın \
ve notunu görürsün. Alarm işlem açmaz. Alarmların numarası pozisyon ve emirlerden ayrıdır.
- Sen yalnızca çağrıldığında karar verirsin. Bir sonraki çağrının kaç dakika sonra olacağını sen seçersin, \
{min_wait} ile {max_wait} dakika arası. Bir emrin gerçekleşmesi, emir süresinin dolması, TP, SL, \
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
cevaplarsın. Yöneticiden ve müdahalelerinden yalnızca yoneticiye_cevap alanında söz edersin, analizinde, özetinde, \
değerlendirmelerinde ve not defterinde söz etmezsin.

## Araçların

Karar vermeden önce istediğin araçları istediğin kadar kullanabilirsin, bir çağrıda en fazla {step_limit} adım. \
Sınıra yaklaşınca kararını yaz.

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
diğerleri yine denenir. Bir eylemin reddedilirse hemen yeniden çağrılırsın, reddin nedenini görür ve eylemini \
düzeltirsin. Her eylem tipinin ne yaptığı ve hangi alanları istediği cevap şemasında yazılıdır.

## Cevap

Bütün metinleri Türkçe yaz. analiz alanında piyasayı nasıl değerlendirdiğini, araçlardan ne öğrendiğini ve \
kararının gerekçelerini ayrıntılı açıkla. Bu alan senin işlem günlüğündür. pariteler alanında her parite için \
görüşünü ve bir iki cümlelik değerlendirmeni yaz. ozet tek cümle olsun. gorus piyasa hakkındaki genel kanaatin, \
guven ise kararına ne kadar güvendiğin, 0 ile 100 arası."""


# Her parite ayrı ve zorunlu bir alan olduğu için Opus hiçbir pariteyi
# değerlendirmeden geçemez. Bu alan olmadan Opus dikkatini açık pozisyonlarına
# kilitliyordu.
def schema(symbols: list[str]) -> dict:
    """Opus'un kararının uyması gereken JSON şemasını döndürür."""
    pair = {"type": "object", "additionalProperties": False, "required": ["gorus", "degerlendirme"],
            "properties": {"gorus": {"type": "string", "enum": VIEWS}, "degerlendirme": {"type": "string"}}}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["analiz", "pariteler", "ozet", "gorus", "guven", "eylemler", "not_defteri", "sonraki_cagri_dk",
                     "yoneticiye_cevap"],
        "properties": {
            "analiz": {"type": "string"},
            "pariteler": {"type": "object", "additionalProperties": False, "required": list(symbols),
                          "properties": {s: pair for s in symbols}},
            "ozet": {"type": "string"},
            "gorus": {"type": "string", "enum": VIEWS},
            "guven": {"type": "integer"},
            "eylemler": {"type": "array", "items": actions.schema(list(symbols))},
            "not_defteri": {"type": "string"},
            "sonraki_cagri_dk": {"type": "integer"},
            "yoneticiye_cevap": {"type": "string", "description": "Yöneticinin sorularına, verdiği haberlere ve "
                                 "araştırmanı istediği konulara cevabın. Böyle bir müdahale yoksa boş bırak."},
        },
    }


# --- İstem ---

_FIXED = {"timer": "İstediğin bekleme süresi doldu.", "admin": "Yönetici seni çağırdı.",
          "start": "Hesap yeni açıldı, bu ilk çağrın."}


def wake_text(w: Wake, now: int) -> str:
    """Bir uyandırma nedenini Opus'un okuyacağı cümleye çevirir."""
    if w.kind in _FIXED:
        return _FIXED[w.kind]
    if isinstance(w.data, AccountEvent):
        return event_text(w.data)
    if isinstance(w.data, Fire):
        return fire_text(w.data, now)
    if isinstance(w.data, str):
        return w.data
    return ""


def _correction(results: list[ActionResult]) -> str:
    lines = ["# Düzeltme", "Bir önceki kararındaki eylemlerden bazıları reddedildi. Kabul edilenler uygulandı, onları "
             "tekrar verme. Reddedilenlerin nedenine bakıp gerekiyorsa düzeltilmiş halini ver, gerekmiyorsa hiçbir "
             "eylem vermeden kararını yaz.", ""]
    for r in results:
        lines.append(f"- {r.label}: {'uygulandı' if r.ok else 'reddedildi'}. {r.message}")
    return "\n".join(lines)


def _account(ctx: Context) -> str:
    s, st = ctx.state, ctx.stats
    ret = (s.equity / ctx.start_balance - 1) * 100 if ctx.start_balance else 0.0
    a = [f"# Hesap\n\nNakit: {s.cash:.2f} dolar\nÖzkaynak: {s.equity:.2f} dolar, başlangıç {ctx.start_balance:g}, "
         f"getiri yüzde {ret:+.2f}"]
    if s.positions:
        a.append("\nAçık pozisyonlar:")
        for x in s.positions:
            last = ctx.prices.get(x.symbol, x.entry_price)
            mark = ctx.marks.get(x.symbol, last)
            upnl = (mark - x.entry_price) * x.qty * x.side.sign
            trail = f", iz süren stop yüzde {x.trail_pct:g}" if x.trail_pct else ""
            a.append(f"- #{x.number} {x.symbol} {x.side} {x.leverage:g}x, miktar {x.qty:g}, marjin {x.margin:.2f}, "
                     f"büyüklük {x.qty * x.entry_price:.2f}, giriş {price(x.entry_price)}, güncel {price(last)}, "
                     f"mark {price(mark)}, gördüğü en iyi mark fiyatı {price(x.best_price)}, "
                     f"açılış {clock(x.entry_time)}, "
                     f"TP {levels_text(x.tp_levels)}, SL {price(x.sl)}{trail}, likidasyon {price(x.liq_price)}, "
                     f"açık kâr/zarar {upnl:+.2f}, fonlama {-x.funding:+.4f}")
    else:
        a.append("\nAçık pozisyon yok.")
    if s.orders:
        a.append("\nBekleyen emirler:")
        for o in s.orders:
            valid = f", geçerlilik {clock(o.expires_at)}" if o.expires_at else ""
            trail = f", iz yüzde {o.trail_pct:g}" if o.trail_pct else ""
            a.append(f"- #{o.number} {o.symbol} {o.side} tetik {price(o.trigger)} {o.leverage:g}x marjin "
                     f"{o.margin:.2f}, {'maker' if o.maker else 'taker'}, TP {levels_text(o.tp_levels)}, "
                     f"SL {price(o.sl)}{trail}{valid}")
    else:
        a.append("\nBekleyen emir yok.")
    if ctx.alerts:
        a.append("\nAlarmlar:")
        for al in ctx.alerts:
            who = "" if al.actor == "strategy" else ", yönetici kurdu"
            a.append(f"- alarm #{al.number} {al.symbol} {price(al.price)}, not: {al.note or 'yok'}{who}")
    else:
        a.append("\nAlarm yok.")
    a.append(f"\nKapanan işlemler: {st.get('count', 0)}, kazanan {st.get('wins', 0)}, toplam net "
             f"{st.get('net', 0.0):+.2f} dolar, ödenen toplam komisyon {st.get('fee', 0.0):.2f}")
    return "\n".join(a)


def build_prompt(ctx: Context, cards: str) -> str:
    """Opus'a gönderilecek istemi kurar.

    Args:
        ctx: Uyanmanın bağlamı.
        cards: cards.cards_text() çıktısı.

    Returns:
        Markdown biçiminde tek bir metin.
    """
    reasons = " ".join(t for w in ctx.wakes if w.kind != "correction" and (t := wake_text(w, ctx.now)))
    out = [f"# Çağrı\n\nŞu an {date_text(ctx.now, weekday_info=True)} {TZ_LABEL}.\n\n"
           f"Çağrılma nedeni: {reasons or 'Önceki kararındaki eylemlerin sonuçları.'}"]
    for w in ctx.wakes:
        if w.kind == "correction":
            out.append(_correction(w.data))
    # Yöneticinin bölümü yalnızca söylenecek bir şey varsa girer.
    if ctx.admin.get("items") or ctx.admin.get("restrictions"):
        block = ["# Yöneticiden"]
        if ctx.admin.get("items"):
            block.append("\n".join(f"- {x}" for x in ctx.admin["items"]))
        if ctx.admin.get("restrictions"):
            block.append("## Yürürlükteki kısıtlar\n\n" + "\n".join(f"- {x}" for x in ctx.admin["restrictions"]))
        out.append("\n\n".join(block))
    out.append(hours_text(datetime.fromtimestamp(ctx.now, timezone.utc)))
    out.append(cards)
    out.append(_account(ctx))
    if ctx.history:
        lines = []
        for d in ctx.history:
            lines.append(f"- {clock(d['time'])}: {d['summary']}")
            lines.extend(f"  - {r}" for r in d.get("results", []))
        out.append("# Son kararların\n\n" + "\n".join(lines))
    out.append("# Not defterin\n\n" + (ctx.memory.get("notebook") or "Boş. Bu ilk çağrın olabilir."))
    return "\n\n".join(out)
