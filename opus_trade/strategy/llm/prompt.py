"""
file: prompt.py
importable: system_prompt, wake_text, build_prompt

Opus'un talimatı ve her çağrının istemi. system_prompt() profilin bakiyesi,
pariteleri, bekleme aralığı, tarz metni ve strateji türünün hedef metni ile
karar adımlarıyla talimatı kurar. build_prompt() bir çağrının istemini kurar:
önce dünün düşünceleri, yani yönetici, not defteri ve son kararlar, sonra
bugünün gerçekleri, yani hesap, piyasa saatleri ve parite kartları. Karar
şeması schema.py'de, talimatın sürümü strateji türünün kaydındadır.

description: Opus'un talimatı ve istemi.
"""

# NOTE: Talimatın çekirdeği bütün LLM profillerinde ortaktır, talimat/
# cekirdek.md. Türe özgü olan yalnızca hedef metni ve karar adımlarıdır.
# Talimat değişince değişen türün sürümü artırılır, günlük her çağrının
# hangi sürümle yapıldığını kaydeder ve sonuçlar sürüme göre ayrılarak
# incelenir. Hiçbir profil sıfırlanmaz.
#
# NOTE: Deney ya da sanal para kelimeleri bilerek kullanılmaz, paranın gerçek
# olduğu da söylenmez. Opus bir hesabı yönettiğini bilir. Tarih ve gün bilgisi
# yorum katılmadan verilir. Dönem ve bitiş tarihi yoktur.
#
# NOTE: Hesabın kurallarının anlatımı yalnızca talimattadır. actions.py'deki
# eylem açıklamaları alanların ne olduğunu söyler, kuralları anlatmaz. İkisi
# ayrı yazılınca biri değişip öteki eskiyordu.
#
# NOTE: İstemde önce dün ne düşünüldüğü gelir, en son ve en taze bilgi olarak
# piyasa. Opus eski defterini okur, sonra piyasayı okur ve sınamayı bu veriyle
# yapar, talimat/istem.md.
#
# XXX: Talimatta düşünce süreci ya da iç akıl yürütme gibi ifadeler
# kullanılmamalı. Opus'un güvenlik filtresi bunları akıl yürütmeyi dışarı
# çıkarma isteği sayıp çağrıyı reddediyor. Kontrol, gerekçe ve sınama
# kelimeleri sorunsuz.

from datetime import datetime, timezone

from ...domain import numbers, rules
from ...domain.fees import FeeSchedule
from ...domain.models import SymbolSpec
from ...domain.trading import AccountEvent, AccountState, Order, OrderType, Position
from ...triggers.base import Fire
from ..base import Context, Thesis, Wake
from . import notebook as nbk
from .hours import hours_text, releases_text
from ...team.roles import ROLE_KEYS, ROLES
from .steps.base import Steps
from .steps.structure import REGIMES
from .texts import (ORDER_NAMES, REASONS, TZ_LABEL, clock, date_text, event_text, fire_text,
                    guard_text, price)

# İstemde emrin ve pozisyonun yanında yazılan risk gerekçesinin en uzun hali.
_REASON_SHOWN = 160


# Limit fiyatının mark fiyatına uzaklık sınırı iki yönde çoğu zaman aynıdır,
# farklıysa ikisi de yazılır.
def _band(s: SymbolSpec) -> str:
    up, down = (s.price_up - 1) * 100, (1 - s.price_down) * 100
    return f"yüzde {up:g}" if abs(up - down) < 1e-9 else f"yukarı yüzde {up:g}, aşağı yüzde {down:g}"


def _symbol_table(specs: list[SymbolSpec]) -> str:
    lines = ["sembol | açıklama | en fazla kaldıraç | en küçük işlem büyüklüğü | miktar adımı | fiyat adımı | "
             "tahmini kayma | limit fiyatının mark fiyatına en fazla uzaklığı | likidasyon ücreti"]
    for s in specs:
        lines.append(f"{s.symbol} | {s.note or s.name.split('/')[0]} | {s.brackets[0].max_leverage}x | "
                     f"{s.min_notional:g} dolar | {s.qty_step:g} | {s.tick:g} | yüzde {s.slippage * 100:.2f} | "
                     f"{_band(s)} | yüzde {s.liq_fee * 100:g}")
    return "\n".join(lines)


# Nasıl karar verirsin bölümü adımlardan kurulur: giriş paragrafı, numaralı
# adımlar, girişlerin denetimi, ortak paragraf ve adımların ek bölümleri.
def _steps_text(steps: Steps, releases: str) -> str:
    rules = steps.entry_rules()
    parts = [f"""Cevabını şemanın sırasıyla yazarsın, bu sıra kararının sırasıdır. İlk {steps.count_word()} adım \
atlanamayan kontrollerdir. Bir kontrolde yazdığın seni bağlar: eylemin onunla çelişirse sistem eylemini reddeder. Bu \
adımların içinde yön, seviye, zamanlama, hedef, araştırma ve bekleme süresi tamamen senin kararındır.""",
             "\n".join(steps.numbered())]
    if rules:
        parts.append(rules)
    parts.append(f"""Adımlardan sonra analizini, eylemlerini, not defterini ve özetini yazarsın. Her çağrıda bütün \
pariteleri değerlendirirsin, bir pariteyi değerlendirmek orada işlem açman gerektiği anlamına gelmez. Fiyatı yalnızca \
grafik hareket ettirmez: haberler, makro veriler, merkez bankası kararları ve büyük piyasaların açılış ve kapanışları \
da etkiler. ABD'nin ekonomik verileri ve Fed kararları açıklandığı günlerde şu saatlerde gelir: {releases}.""")
    parts += steps.sections()
    return "\n\n".join(parts)


# Ekip aracının satırı yalnızca kotası olan rolleri sayar. Günlük sınırı
# araç her cevabında kalan hak olarak söyler, talimatta rakamı yoktur.
def _team_text(team: dict[str, int]) -> str:
    roles = [ROLES[r] for r in ROLE_KEYS if team.get(r, 0) > 0]
    if not roles:
        return ""
    lines = "\n".join(f"  - {r.key}, {r.name}, en fazla {team[r.key]}: {r.description}" for r in roles)
    return f"""- ekip aracıyla gorev_ver: Araştırma ve analiz görevlerini alt ajanlara devreder. Görevler aynı anda \
çalışır, her alt ajan web'de arar, sayfa okur ve piyasa araçlarını kullanır, sana bir rapor getirir. Hesabına \
dokunamazlar. Bu, kendi bağlamını uzun sayfalarla doldurmadan geniş bir araştırma yapmanı sağlar. Alt ajan istemini \
görmez, görevi açık ve kendi içinde yeterli yazarsın. Bir çağrıda verebileceğin görevler, role göre:
{lines}
  Günlük bir üst sınır da vardır, araç kalan hakkını her cevabında söyler. Görevler sürerken çağrının süresi işler, \
kararını yazmaya zaman bırakırsın.
"""


def system_prompt(balance: float, specs: list[SymbolSpec], fees: FeeSchedule, style: str, min_wait: int,
                  max_wait: int, step_limit: int, steps: Steps, goal: str, team: dict[str, int], now: int) -> str:
    """Opus'un talimatını döndürür.

    Args:
        balance: Hesabın başlangıç bakiyesi, dolar.
        specs: Profilin işlem yapabildiği paritelerin kuralları.
        fees: Hesabın komisyon oranları.
        style: Profilin tarz metni, boşsa çalışma tarzı bölümü eklenmez.
        min_wait: Opus'un seçebileceği en kısa bekleme, dakika.
        max_wait: En uzun bekleme, dakika.
        step_limit: Opus'a bildirilen araç adımı sınırı.
        steps: Strateji türünün karar adımları.
        goal: Hedefin bölümünün metni, strateji türünündür.
        team: Rolden bir çağrıdaki ekip görevi kotasına. Hepsi sıfırsa ekip aracı anlatılmaz.
        now: Şu an, ABD verilerinin Türkiye saatiyle açıklanma saatleri için.
    """
    style_part = f"## Çalışma tarzın\n\n{style}\n\n" if style else ""
    releases = releases_text(datetime.fromtimestamp(now, timezone.utc))
    carried = steps.notebook_note()[0]
    carried = f" {carried}" if carried else ""
    alarm_rule = f" {steps.alarm_rule()}" if steps.alarm_rule() else ""
    return f"""Bir Binance USDT sürekli vadeli hesabını yönetiyorsun. Hesabın başlangıç bakiyesi {balance:g} dolar. \
Fiyatlar, fonlama oranları ve piyasa verileri Binance'in gerçek ve canlı verileridir. Bütün saatler {TZ_LABEL}.

## Hedefin

{goal}

## Öncelik sırası

İki şey çelişirse yukarıdaki kazanır:
1. Yöneticinin kesin talimatı ve kısıtları
2. Kontrol adımları ve onlarda yazdığın beyanlar
3. Çalışma tarzın
4. Defterindeki plan
5. Son kararların

## Nasıl karar verirsin

{_steps_text(steps, releases)}

{style_part}## Not defteri

Her çağrıda sıfırdan başlarsın, önceki konuşmayı hatırlamazsın. Sana son kararlarının özeti ve not defterin gelir. \
Not defteri üç alandır:
- plan: İki üç cümlelik metin, planı bozan koşul ve planın geçerli olduğu son an. En fazla \
{nbk.PLAN_TEXT_MAX} ve {nbk.PLAN_BREAK_MAX} karakter.
- seviyeler_notu: Kilit seviyeler ve neden önemli oldukları, en fazla {nbk.NOTE_MAX} karakter.{carried}
- gundem: Etkisi süren haberler ve yaklaşan olaylar, tarihleriyle, en fazla {nbk.AGENDA_MAX} karakter.

Planı geçerli dersen metni ve bozulma koşulu olduğu gibi taşınır, yalnızca geçerlilik saatini değiştirebilirsin. \
Zayıfladı dersen düzeltebilirsin. Planın beklediği kurulum oluştuysa gerçekleşti dersin. Gerçekleşti ya da bozuldu \
dersen ya da planın geçerlilik saati geçtiyse yeni bir plan yazmak zorundasın. Süresi dolan plan istemde öyle \
işaretlenir, geçerlilik saati gelince ayrıca çağrılmazsın. Deftere işlem geçmişini, hesabı ve pozisyonların tezini \
yazmazsın, onlar zaten sana gelir. Deftere ders ya da kural da yazmazsın. İşlemlerinde bir örüntü görürsen, \
örneğin girişlerinin çoğu erken kalıyorsa, bunu yoneticiye_oneri alanına yazarsın.

## Yönetici

Hesabın bir yöneticinin gözetimindedir. Yönetici seni zamanından önce çağırabilir, sana bir bilgi ya da haber \
verebilir, soru sorabilir, hesabında işlem yapabilir ya da kısıt koyabilir. Her müdahale sana yöneticiden geldiği \
belirtilerek bildirilir, yöneticinin son 24 saatte hesabında yaptığı işlemler de her çağrıda gösterilir. Teklif \
olarak gelenleri değerlendirirsin, mantıklı bulursan uygularsın, o zaman bütün kontrollerden geçer. Kesin olarak \
gelenlere uyarsın. Kesin işlemler sana uygulanmış olarak bildirilir, kesin kısıtlar ise yürürlükte kaldıkça onları \
aşan eylemlerinin reddedilmesine yol açar. Yöneticinin açtığı pozisyon hesap bölümünde yönetici açtı diye \
işaretlenir. Yöneticinin sorularını ve verdiği haberleri yoneticiye_cevap alanında cevaplarsın. Yöneticiden ve \
müdahalelerinden yalnızca yoneticiye_cevap ve yoneticiye_oneri alanlarında söz edersin, analizinde, özetinde, \
kontrollerinde ve not defterinde söz etmezsin.

## Araçların

Karar vermeden önce istediğin araçları istediğin kadar kullanabilirsin, bir çağrıda en fazla {step_limit} adım. \
Sınıra yaklaşınca kararını yaz. Araçların hepsi senin için var, hangi sorunun cevabını hangi araçta arayacağına sen \
karar verirsin.

- WebSearch ve WebFetch: Haber, makro gelişme, ekonomik takvim, piyasa yorumu ya da istediğin herhangi bir bilgi \
için web'de arama yapar ve sayfa okur.
{_team_text(team)}- Piyasa araçları: Herhangi bir paritede istediğin aralıkta ve sayıda mum, istediğin ayarlarla gösterge hesabı, \
destek ve direnç seviyeleri, hacim profili, pariteler arası korelasyon, emir defteri, fonlama geçmişi, açık \
pozisyon geçmişi, long short oranları, likidasyonlar, kendi geçmiş kararların ve kapanmış işlemlerin. Likidasyon \
verisi Binance'in akışından örneklenir: her parite için saniyede yalnızca en büyük likidasyon kaydedilir, bu yüzden \
toplam tutarı değil dalgaların zamanını, yönünü ve büyüklüğünü gösterir.

## Hesabın kuralları

{_symbol_table(specs)}

Diğer paritelerin verisine piyasa araçlarıyla bakabilirsin ama yalnızca bu paritelerde işlem yapabilirsin.

- Hesabının tek bir bakiyesi vardır, dolar cinsindendir. Hesap Binance'in hedge modundadır: her paritede bir long \
ve bir short pozisyonun olabilir, ikisi ayrı tutulur. Pozisyon BTCUSDT long gibi parite ve yönüyle anılır. Aynı \
yönde yeni bir giriş açık pozisyonla birleşir, giriş fiyatı miktarla ağırlıklı ortalanır, marjinler toplanır.
- Her pozisyonun marjini izoledir. Likidasyonda yalnızca o pozisyonun marjini kaybedilir ve büyüklükten tablodaki \
likidasyon ücreti alınır. Her pozisyonun likidasyon fiyatı sana bildirilir.
- Kaldıraç paritenin ayarıdır, o paritedeki long ve short için ortaktır ve yeni girişlere uygulanır. Tam sayıdır, \
en fazla tablodaki değer olur. Hiç ayarlamadığın paritenin kaldıracı 20'dir. Paritede açık pozisyon varken \
kaldıraç düşürülemez, yükseltilebilir. Yükseltme açık pozisyonun marjinini ve likidasyon fiyatını değiştirmez. \
Paritelerin ayarlı kaldıracı hesap bölümünde yazar. Bir girişte kaldirac verirsen ve paritenin kaldıracından \
farklıysa önce parite o kaldıraca ayarlanır, ayar reddedilirse giriş de gönderilmez.
- Girişte marjini dolar olarak verirsin. Miktar, marjin çarpı kaldıracın fiyata bölümüdür ve paritenin miktar \
adımına aşağı yuvarlanır. Büyüklük, en küçük işlem büyüklüğünden ve bir miktar adımının değerinden az olamaz. \
Verdiğin fiyatlar paritenin fiyat adımına en yakın yönde yuvarlanır.
- Kullanılabilir bakiye, nakitten bekleyen limit emirlerin ayırdığı tutar düşülerek bulunur. Bir giriş marjinini \
ve komisyonunu kullanılabilir bakiyeden karşılar. Marjin kullanılabilir bakiyeyi yüzde 2'ye kadar aşarsa miktar \
bakiyeye sığacak kadar küçültülür ve bu sana bildirilir, daha fazla aşarsa giriş reddedilir.
- Piyasa girişi, piyasa_giris, hemen, güncel fiyattan gerçekleşir.
- Limit emir, emir_ver ile tur limit: Fiyatı long için güncel fiyatın altında, short için üstündeyse defterde \
bekler. Fiyat seviyeni bir fiyat adımı geçince tam seviyenden gerçekleşir. Fiyatı yanlış taraftaysa hemen \
piyasadan gerçekleşir. Limit emir verildiği anda marjinini kullanılabilir bakiyeden ayırır, fiyatı mark fiyatının \
aleyhindeyse aradaki açık zararı da ayırır. Limit fiyatı mark fiyatından tablodaki uzaklıktan daha uzak olamaz. \
Yalnızca limit emrin fiyatı değiştirilebilir, diğer emirler iptal edilip yeniden verilir.
- Kırılım emri, tur kirilim, Binance'in STOP_MARKET emridir: long için güncel fiyatın üstünde, short için altında \
bir tetiktir. Geri çekilme emri, tur geri_cekilme, Binance'in TAKE_PROFIT_MARKET emridir: long için güncel fiyatın \
altında, short için üstünde bir tetiktir. İkisi de son işlem fiyatı tetiğe değince piyasa emri olur ve tetik anının \
fiyatından gerçekleşir: fiyat tetiğin ötesine sıçrayarak geldiyse oradan, gelmediyse tetikten. Fiyatın sonradan \
iğne gibi geri dönmesi gerçekleşmeyi değiştirmez. Tetiği yanlış tarafta olan emir reddedilir. Tetikli emir \
gerçekleşene kadar bakiye ayırmaz, bakiye gerçekleştiği anda kontrol edilir. Yetmezse emir açılamaz ve bu sana \
bildirilir.
- Bekleyen bir emre gecerlilik_dk ile geçerlilik süresi verebilirsin, o süre içinde gerçekleşmezse emir iptal olur.
- Emirlerin numarası vardır. Numara, o an açık emirlerin kullanmadığı en küçük sayıdır. İptal edilen ya da \
gerçekleşen bir emrin numarası sonra yeni bir emre verilebilir. Bu yüzden numarayla bir emre dokunan her eylemde \
sembolü ve yönü de yazarsın. Numara artık o sembol ve yöndeki emri göstermiyorsa eylem reddedilir.
- Koruma emirleri: TP, SL ve iz süren stop pozisyonu kapatan ayrı emirlerdir, her birinin numarası ve miktarı \
vardır. Miktarsız bir koruma tetiklendiği an pozisyonda kalan miktarın tamamını kapatır. Bir girişe tp ya da \
tp_kademeleri, sl ve iz_yuzde verirsen giriş gerçekleşince o girişin miktarıyla kurulur, böylece aynı pozisyondaki \
farklı girişlerin ayrı korumaları olabilir. Bir giriş en fazla dört TP kademesi taşır, her kademenin yuzde alanı \
girişin yüzde kaçını kapatacağıdır. tp ile tp_kademeleri birlikte verilmez. Açık bir pozisyona sonradan da \
koruma ekleyebilirsin, yuzde verirsen pozisyonun o kadarını korur. Pozisyon tamamen kapanınca ona bağlı bütün \
koruma emirleri iptal olur. Tetiklenen bir korumanın miktarı pozisyonda kalandan büyükse kalanı kapatır. Fiyat, \
giriş gerçekleşirken bir korumanın seviyesini çoktan geçtiyse o koruma kurulmaz ve bu sana bildirilir. Bir \
korumanın seviyesini değiştirince önce yeni emir kurulur, sonra eskisi iptal edilir.
- TP son işlem fiyatı oraya değdiği an tetiklenir ve tetik anının fiyatından piyasadan gerçekleşir. SL, iz süren \
stop ve likidasyon ise mark fiyatına bakar, mark fiyatı oraya değdiği an uygulanır. Mark fiyatı Binance'in \
birçok borsanın fiyatından hesapladığı adil fiyattır, yalnızca bu borsada görülen kısa iğneler onu oynatmaz. \
Grafikteki mumlar son işlem fiyatıdır, bir iğnenin SL'ye değmesi SL'yi çalıştırmayabilir. SL ve iz süren stop \
tetiklendiği andaki son işlem fiyatından gerçekleşir, fiyat sıçradıysa seviyesinden kötü olabilir. Hiçbiri senin \
çağrılmanı beklemez. Bir mum içinde hem TP hem SL görülürse SL uygulanır.
- İz süren stop: Konduğu andan beri görülen en iyi mark fiyatından verdiğin yüzde kadar geride durur ve fiyat \
lehine gittikçe kendiliğinden ilerler. Yüzde 0.1 ile 10 arasında olur. Her iz emrinin gördüğü en iyi mark fiyatı \
ve bugünkü seviyesi hesap bölümünde yazar. SL ile birlikte kullanılabilir, mark fiyatı hangisine önce değerse o \
uygulanır.
- Komisyon işlem büyüklüğü üzerinden kesilir. Defterde bekleyip seviyesinden gerçekleşen limit emirler maker \
sayılır, yüzde {fees.maker * 100:.2f} öder ve tam fiyatından gerçekleşir. Piyasa girişleri, hemen gerçekleşen \
limit emirler, kırılım ve geri çekilme emirleri, TP, SL, iz süren stop ve senin kapatmaların taker sayılır, yüzde \
{fees.taker * 100:.2f} öder ve tablodaki kadar aleyhine kayma ile gerçekleşir.
- Fonlama gerçek piyasadaki gibi uygulanır, çoğu paritede sekiz saatte bir, bazılarında dört saatte bir. Oran \
pozitifse long'lar öder, short'lar alır, negatifse tersi. Ödeme pozisyonun marjininden düşer ya da marjine \
eklenir ve likidasyon fiyatını değiştirir.
- Fiyat alarmı: Bir paritede bir fiyat seviyesi ve kendine bir not verirsin. Fiyat oraya değince hemen çağrılırsın \
ve notunu görürsün. Alarm işlem açmaz. Alarmların numarası emirlerden ayrıdır.{alarm_rule}
- Sen yalnızca çağrıldığında karar verirsin. Bir sonraki çağrının kaç dakika sonra olacağını sen seçersin, \
{min_wait} ile {max_wait} dakika arası. Bir giriş emrinin gerçekleşmesi ya da açılamaması, emir süresinin \
dolması, TP, SL, iz süren stop, likidasyon, bir alarm ya da açık pozisyonun geçersizlik fiyatı olduğunda beklemeden \
hemen çağrılırsın.
- Hesabı yürüten sistem zaman zaman kapanabilir. Kapanacağı önceden belliyse kapanmadan önce çağrılır ve haber \
verilirsin. Sistem kapalıyken çağrılamazsın ve alarmların seni çağıramaz. Emirlerin, TP, SL, iz süren stop ve \
likidasyon ise kapalıyken de işlemeye devam eder. Sistem açılınca kapalı kalınan süre ve o sürede olanlar sana \
bildirilir.

## Cevap

eylemler listesi boş olabilir, bu bekle demektir. Birden fazla eylem verirsen sırayla uygulanır. Bir kontrol hatası \
bütün kararını reddeder: hiçbir eylemin uygulanmaz, defterin değişmez, hangi kontrolün neden tutmadığı sana \
söylenir ve kararını düzeltip yeniden yazarsın. Bir eylem hatası yalnızca o eylemi reddeder, diğerleri yine \
uygulanır ve reddin nedenini görüp eylemini düzeltirsin. En fazla iki kez düzeltirsin. Yine olmazsa o çağrıda \
hiçbir eylemin uygulanmaz, defterin değişmez ve yöneticiye bildirilir.

Bütün metinleri Türkçe yaz. analiz alanına adımlara sığmayan değerlendirmeni ve kararının gerekçesini yazarsın. \
ozet tek cümle olsun, herkese açık sayfada görünür."""


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


def _blind(ctx: Context) -> str:
    f = ctx.facts
    if f.late_min is None or f.last_call is None:
        return ""
    hours, minutes = divmod((ctx.now - f.last_call) // 60, 60)
    ago = f"{hours} saat {minutes} dakika" if hours else f"{minutes} dakika"
    return (f"\n\nSon çağrın {ago} önceydi, istediğin uyanmadan {f.late_min} dakika geç çağrıldın. Arada "
            f"çağrılamadın, o sürede olanlar hesap bölümünde.")


# Kontrol hatasında kararın hiç uygulanmadı. Eylem hatasında kabul edilenler
# uygulandı. İkisinde de kalan düzeltme hakkı yazar.
def _correction(data: dict) -> str:
    left = data["left"]
    rights = f"{left} düzeltme hakkın kaldı." if left else "Bu son düzeltme hakkın."
    if data["rejected"]:
        lines = ["# Düzeltme", "Kararın reddedildi, hiçbir eylemin uygulanmadı ve defterin değişmedi. Reddin nedenleri "
                 "aşağıda. Kararını düzeltip baştan yeniden yaz. " + rights, ""]
        lines += [f"- {r.message}" for r in data["results"]]
        return "\n".join(lines)
    lines = ["# Düzeltme", "Bir önceki kararındaki eylemlerden bazıları reddedildi. Kabul edilenler uygulandı, onları "
             "tekrar verme. Reddedilenlerin nedenine bakıp gerekiyorsa düzeltilmiş halini ver, gerekmiyorsa hiçbir "
             "eylem vermeden kararını yaz. " + rights, ""]
    for r in data["results"]:
        lines.append(f"- {r.label}: {'uygulandı' if r.ok else 'reddedildi'}. {r.message}")
    return "\n".join(lines)


def _admin(ctx: Context) -> str:
    items, limits, recent = ctx.admin.get("items"), ctx.admin.get("restrictions"), ctx.facts.admin_24h
    if not (items or limits or recent):
        return ""
    block = ["# Yöneticiden"]
    if items:
        block.append("\n".join(f"- {x}" for x in items))
    if limits:
        block.append("## Yürürlükteki kısıtlar\n\n" + "\n".join(f"- {x}" for x in limits))
    if recent:
        block.append("## Yöneticinin son 24 saatte hesabında yaptıkları\n\n" + "\n".join(f"- {x}" for x in recent))
    return "\n\n".join(block)


def _history(ctx: Context) -> str:
    if not ctx.history:
        return ""
    lines = []
    for d in ctx.history:
        regimes = " · ".join(f"{s} {REGIMES.get(r, r)} · {'uygun' if fit == 'evet' else 'uygun değil'}"
                             for s, (r, fit) in d.get("regimes", {}).items())
        head = f"{clock(d['time'])} · {regimes} · " if regimes else f"{clock(d['time'])}: "
        tail = "kontrol hatasıyla reddedildi." if d.get("rejected") else d["summary"]
        lines.append(f"- {head}{tail}")
        lines.extend(f"  - {r}" for r in d.get("results", []))
    return "# Son kararların\n\n" + "\n".join(lines)


# --- Hesap ---

# İz emrinin satırı konduğundan beri gördüğü en iyi mark fiyatını ve bugünkü
# seviyesini de taşır, Opus izin nereye geldiğini hesaplamadan görür.
def _exit_line(o: Order, side) -> str:
    text = guard_text(o, "kalanın tamamı")
    if o.type is OrderType.TRAILING_STOP and o.best:
        text += (f", gördüğü en iyi mark fiyatı {price(o.best)}, seviyesi "
                 f"{price(rules.trail_level(side, o.best, o.trail_pct))}")
    return text


# SL ya da izle korunmayan miktar. Miktarsız bir stop pozisyonun tamamını
# korur. Giriş başına ayrı SL konunca bir girişin SL'siz kalması buradan
# görünür.
def _unguarded(p: Position, exits: list[Order]) -> float:
    stops = [o for o in exits if o.type in (OrderType.STOP, OrderType.TRAILING_STOP)]
    if any(o.qty is None for o in stops):
        return 0.0
    return max(0.0, round(p.qty - sum(o.qty for o in stops), 10))


def _thesis(t: Thesis) -> str:
    risk = t.reason.get("risk_beyani") or {}
    why = (risk.get("gerekce") or "").strip()
    if len(why) > _REASON_SHOWN:
        why = why[:_REASON_SHOWN].rsplit(" ", 1)[0] + "..."
    said = f"risk beyanı yüzde {numbers.num(float(risk['yuzde']), 2)}, " if "yuzde" in risk else ""
    return f"{said}geçersizlik {price(t.invalid_price)}: {t.invalid_text}" + (f" Gerekçe: {why}" if why else "")


def _position_lines(p: Position, s: AccountState, ctx: Context) -> list[str]:
    last = ctx.prices.get(p.symbol, p.entry_price)
    mark = ctx.marks.get(p.symbol, last)
    upnl = rules.unrealized_pnl(p.side, p.entry_price, p.qty, mark)
    exits = [o for o in s.orders if o.reduce and o.symbol == p.symbol and o.side is p.side]
    by_admin = ", yönetici açtı" if ctx.facts.position_origins.get(p.id) == "admin" else ""
    lines = [f"- {p.symbol} {p.side}{by_admin}, miktar {p.qty:g}, marjin {p.margin:.2f}, büyüklük "
             f"{p.qty * p.entry_price:.2f}, giriş {price(p.entry_price)}, güncel {price(last)}, mark {price(mark)}, "
             f"açılış {clock(p.entry_time)}, likidasyon {price(p.liq_price)}, açık kâr/zarar {upnl:+.2f}, fonlama "
             f"{-p.funding:+.4f}"]
    for t in ctx.facts.position_notes.get(p.id, ()):
        lines.append(f"  Giriş notu, {clock(t.created_at)}: {_thesis(t)}")
    if exits:
        lines.append("  Koruma emirleri:")
        lines += [f"  - {_exit_line(o, p.side)}" for o in exits]
    else:
        lines.append("  Koruma emri yok.")
    if exits and (rest := _unguarded(p, exits)) > 0:
        lines.append(f"  SL ya da izle korunmayan miktar {rest:g}.")
    return lines


def _entry_lines(o: Order, ctx: Context) -> list[str]:
    valid = f", geçerlilik {clock(o.expires_at)}" if o.expires_at else ""
    asks = ", ".join(guard_text(x, "girişin tamamı") for x in o.protection) or "yok"
    reserved = f", ayırdığı {o.margin:.2f}" if o.margin else ""
    by_admin = ", yönetici verdi" if ctx.facts.order_origins.get(o.id) == "admin" else ""
    lines = [f"- #{o.number} {o.symbol} {o.side} {ORDER_NAMES[str(o.type)]} {price(o.price)} miktar {o.qty:g}"
             f"{reserved}{by_admin}, gerçekleşince kurulacak koruma: {asks}{valid}"]
    if t := ctx.facts.order_notes.get(o.id):
        lines.append(f"  Giriş notu: {_thesis(t)}")
    return lines


def _facts_text(ctx: Context) -> list[str]:
    f = ctx.facts
    out = [f"\nBugün: {f.today_count} pozisyon kapandı, {f.today_stops} tanesi zararla stoplandı, günün neti "
           f"{f.today_net:+.2f} dolar. Art arda zararla kapanan pozisyon: {f.loss_streak}."]
    if f.recent:
        out.append("Son kapanan pozisyonlar, eskiden yeniye:")
        out += [f"- #{c.position_id} {c.symbol} {c.side}, net {c.net:+.2f}, {REASONS.get(c.reason, c.reason)}, "
                f"{clock(c.closed_at)}" for c in f.recent]
    filled, total = f.fill_rate
    if total:
        out.append(f"Son {total} bekleyen giriş emrinden {filled} tanesi doldu, {total - filled} tanesi iptal edildi, "
                   f"süresi doldu ya da açılamadı.")
    if f.invalidated:
        out.append("Son çağrından beri geçersizlik fiyatına değdiği için sistemin iptal ettiği girişler:")
        out += [f"- {t.symbol} {t.side}, geçersizlik {price(t.invalid_price)}, {clock(at)}" for t, at in f.invalidated]
    if f.unreviewed:
        out.append("Sınıflandırılmayı bekleyen pozisyonlar, kapanan_islemler alanında:")
        out += [f"- #{c.position_id} {c.symbol} {c.side}, net {c.net:+.2f}, {REASONS.get(c.reason, c.reason)}, "
                f"{clock(c.closed_at)}" for c in f.unreviewed]
    return out


def _account(ctx: Context) -> str:
    s, st = ctx.state, ctx.stats
    ret = (s.equity / ctx.start_balance - 1) * 100 if ctx.start_balance else 0.0
    levs = ", ".join(f"{sym} {s.leverage(sym)}x" for sym in ctx.symbols)
    a = [f"# Hesap\n\nNakit: {s.cash:.2f} dolar\nKullanılabilir bakiye: {s.available:.2f} dolar\n"
         f"Özkaynak: {s.equity:.2f} dolar, başlangıç {ctx.start_balance:g}, getiri yüzde {ret:+.2f}\n"
         f"Paritelerin kaldıracı: {levs}"]
    if s.positions:
        a.append("\nAçık pozisyonlar:")
        for p in s.positions:
            a += _position_lines(p, s, ctx)
    else:
        a.append("\nAçık pozisyon yok.")
    entries = [o for o in s.orders if not o.reduce]
    if entries:
        a.append("\nBekleyen giriş emirleri:")
        for o in entries:
            a += _entry_lines(o, ctx)
    else:
        a.append("\nBekleyen giriş emri yok.")
    if ctx.alerts:
        a.append("\nAlarmlar:")
        for al in ctx.alerts:
            who = {"admin": ", yönetici kurdu", "expectation": ", beklediğin fiyata sistem kurdu"}.get(al.purpose, "")
            a.append(f"- alarm #{al.number} {al.symbol} {price(al.price)}, not: {al.note or 'yok'}{who}")
    else:
        a.append("\nAlarm yok.")
    a += _facts_text(ctx)
    a.append(f"\nBaşlangıçtan beri kapanan işlemler: {st.get('count', 0)}, kazanan {st.get('wins', 0)}, toplam net "
             f"{st.get('net', 0.0):+.2f} dolar, ödenen toplam komisyon {st.get('fee', 0.0):.2f}")
    return "\n".join(a)


def build_prompt(ctx: Context, cards: str, written_at: int | None = None) -> str:
    """Opus'a gönderilecek istemi kurar.

    Args:
        ctx: Uyanmanın bağlamı.
        cards: cards.cards_text() çıktısı.
        written_at: Defterin yazıldığı kararın anı, tepe ve diplerin hangi karardan geldiği için.

    Returns:
        Markdown biçiminde tek bir metin.
    """
    reasons = " ".join(t for w in ctx.wakes if w.kind != "correction" and (t := wake_text(w, ctx.now)))
    out = [f"# Çağrı\n\nŞu an {date_text(ctx.now, weekday_info=True)} {TZ_LABEL}.\n\n"
           f"Çağrılma nedeni: {reasons or 'Önceki kararının sonuçları.'}{_blind(ctx)}"]
    for w in ctx.wakes:
        if w.kind == "correction":
            out.append(_correction(w.data))
    if admin := _admin(ctx):
        out.append(admin)
    out.append(nbk.text(nbk.read(ctx.memory), ctx.now, written_at))
    if history := _history(ctx):
        out.append(history)
    out.append(_account(ctx))
    out.append(hours_text(datetime.fromtimestamp(ctx.now, timezone.utc)))
    out.append(cards)
    return "\n\n".join(out)
