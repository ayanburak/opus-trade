"""
file: playbook.py
importable: PLAN_PARAMS, DEFAULT_PLANS, PlaybookStep

Oyun planları, Opus Multi türünün adımı. Her rejimin kendi oyun planı vardır.
Opus bir paritenin rejimini yapı adımında okur ve o paritede girişi o
rejimin planıyla verir. Adım talimata planların metnini ve kuralını ekler,
geçen her girişin notuna paritenin o karardaki rejimini oyun planı olarak
yazar. Açık pozisyon rejim değişse de onu açan planla yönetilir, planın adı
giriş notunda Opus'un önüne gelir.

    PlaybookStep({"duzenli_trend": "...", "yatay": "...", "asiri_uzama": "...", "donus": "..."})

description: Rejim başına oyun planı adımı.
"""

# NOTE: Adımın numarası yoktur, size sıfırdır. Opus planı ayrıca yazmaz,
# plan paritenin rejim alanından gelir. Böylece numaralı adımlar llm
# türüyle aynı kalır ve bir girişin planı Opus'un beyanına değil okuduğu
# rejime bağlı olur.
#
# NOTE: Yapı adımı olmadan çalışmaz, rejimi kararın pariteler alanından
# okur. kinds.py ikisini birlikte kurar. Belirsiz rejimin planı yoktur,
# Opus Multi'de belirsiz uygun rejimlerden değildir, yapı adımı o paritede
# girişi zaten reddeder.
#
# NOTE: Notta planın anahtarı değil Türkçe adı durur. İstem ve panel adı
# doğrudan gösterir, eşleme tablosu taşımaz.
#
# XXX: oyun_plani anahtarı Opus'un önüne gelen notta görünür, Türkçe kalır.

from ....domain.trading import PlaceOrder
from ..checks import Review
from .base import DecisionStep
from .structure import REGIMES

# Rejimin anahtarından profilin parametre adına.
PLAN_PARAMS = {"duzenli_trend": "plan_trend", "yatay": "plan_range", "asiri_uzama": "plan_stretch",
               "donus": "plan_reversal"}

# Profil açılırken formda gelen planlar. Burak panelden değiştirir.
DEFAULT_PLANS = {
    "duzenli_trend": "Trend yönünde girersin. Girişi alt aralıkta trend yönündeki geri çekilmenin bittiği yerde, limit "
                     "ya da geri_cekilme emriyle verirsin. Hareketi kovalamazsın. SL alt aralıktaki son dibin ya da "
                     "tepenin ötesindedir. Kârın bir kısmını önceki tepede ya da dipte alırsın, kalanı izle taşırsın. "
                     "Fiyat üst aralıkta trendin son dibini ya da tepesini kırarsa plan biter.",
    "yatay": "Bandın kenarlarından ters yönde girersin: alt kenarda long, üst kenarda short. Bandın ortasında giriş "
             "açmazsın. SL bandın dışında, kenarın ötesindedir. TP karşı kenardan önce durur. Fiyat bandın dışında bir "
             "alt aralık mumu kapatırsa bant bozulmuştur, o yöndeki bekleyen girişi iptal edersin.",
    "asiri_uzama": "Hareketin tersine yalnızca hareketin yorulduğunu gösteren bir yapıdan sonra girersin: alt aralıkta "
                   "ilk ters tepe ya da dip. Uzama sürebilir, bu yüzden risk diğer planlarınkinin yarısını geçmez. SL "
                   "hareketin ucunun ötesindedir. İlk hedef hareketin üçte birinin geri alınmasıdır. Hareket yeni bir "
                   "uç yaparsa plan biter.",
    "donus": "Yeni yöne, ilk geri çekilmeden sonra girersin. Eski trendin son yapısını kıran ilk hareketi kovalamazsın. "
             "SL geri çekilmenin ucunun ötesindedir. Fiyat eski trendin son ucunu geçerse plan biter.",
}


class PlaybookStep(DecisionStep):
    """Rejim başına oyun planı adımı.

    Args:
        plans: Rejimin anahtarından planın metnine, PLAN_PARAMS'taki dört rejim.

    Raises:
        ValueError: Bir rejimin planı eksik ya da boşsa.
    """

    key = "playbook"
    size = 0

    def __init__(self, plans: dict[str, str]) -> None:
        super().__init__()
        for r in PLAN_PARAMS:
            if not (plans.get(r) or "").strip():
                raise ValueError(f"{REGIMES[r].capitalize()} rejiminin oyun planı boş olamaz.")
        self.plans = {r: plans[r].strip() for r in PLAN_PARAMS}

    def texts(self) -> list[str]:
        return []

    def entry_rules(self) -> str:
        return ("Girişi paritenin bu çağrıda okuduğun rejiminin oyun planıyla verirsin. Sistem girişi o planın adıyla "
                "kaydeder. Açık pozisyonu, rejim değişse de, onu açan planın kurallarıyla yönetirsin. Pozisyonun "
                "planı giriş notunda yazar.")

    def sections(self) -> list[str]:
        lines = [f"- {REGIMES[r].capitalize()}: {text}" for r, text in self.plans.items()]
        lines.append("- Belirsiz: Giriş açmazsın.")
        return ["### Oyun planları\n\n" + "\n".join(lines)]

    def entry_note(self, a: dict, cmd: PlaceOrder, k: dict, review: Review) -> dict:
        return {"oyun_plani": REGIMES[k["pariteler"][cmd.symbol]["rejim"]["rejim"]]}
