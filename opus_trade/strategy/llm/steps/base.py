"""
file: base.py
importable: DecisionStep, Steps

DecisionStep bir karar adımının soyut sınıfıdır. Bir adım sisteme şunları
katar: karar şemasına alanlar, talimatın Nasıl karar verirsin bölümüne
adım metni ve varsa ek bölüm, hesabın kurallarına cümle, kararın bütününe bakan denetim, tek bir
girişin denetimi, girişin notuna alanlar, günlük kaydına alanlar, not
defterine taşınan veri, alarmlar ve bir sonraki çağrının en geç anı. Varsayılan uygulamalar boş
döner, bir adım yalnızca ihtiyacı olanı yazar.

Steps bir strateji türünün adım listesidir. Adımları sırayla numaralar ve
her işi adımlara sırayla dağıtır.

    steps = Steps([BookStep(), StructureStep(("4h", "1h"), ("duzenli_trend",)), ClosedStep(), EntryStep(fees)])
    review = steps.validate(k, ctx, old)

description: Karar adımının soyut sınıfı ve adım listesi.
"""

# NOTE: Adımların numarası talimatta ve ret metinlerinde listedeki sıradan
# üretilir. Bir adım birden çok numaralı adım taşıyabilir, size onu söyler.
# Yapı adımı yapı ve rejim diye iki adımdır. first Steps kurulurken yazılır.
# size sıfır olan adım numara almaz, yalnızca metin ve denetim katar. Oyun
# planı adımı böyledir, Opus'un yazdığı ayrı bir alanı yoktur.
#
# NOTE: Girişin denetimleri adımların sırasıyla yapılır, ilk takılan ret
# metni döner. Yöntem adımları evrensel giriş alanlarından önce durduğu için
# bir giriş hem yapıya hem risk beyanına takılırsa Opus önce yapıyı görür.
#
# NOTE: Bütün metotlar tek bir soyut sınıfta durur. Adımların çoğu bunların
# yalnızca birkaçını yazar ama metotlar aynı kararın aynı parçalarına bakar,
# küçük soyut sınıflara bölmek her adımı birkaç sınıftan türetmek olurdu.
# Bir adım türü yalnızca denetim ya da yalnızca metin taşımaya başlarsa
# bölünür.

from abc import ABC, abstractmethod
from typing import ClassVar

from ....domain.trading import PlaceOrder
from ...base import Context
from ..checks import Review
from ..notebook import Notebook

# Talimattaki adım sayısının yazısı, İlk beş adım gibi.
_COUNT_WORDS = {1: "bir", 2: "iki", 3: "üç", 4: "dört", 5: "beş", 6: "altı", 7: "yedi", 8: "sekiz", 9: "dokuz"}


class DecisionStep(ABC):
    key: ClassVar[str]
    size: ClassVar[int] = 1

    def __init__(self) -> None:
        self.first = 0

    @abstractmethod
    def texts(self) -> list[str]:
        """Talimattaki adım metinlerini numarasız döndürür, size kadar."""

    def fields(self, symbols: list[str]) -> dict[str, dict]:
        """Karar şemasına eklediği alanları sırasıyla döndürür."""
        return {}

    def entry_rules(self) -> str:
        """Talimatta adımlardan sonra girişin denetimini anlatan cümleleri döndürür."""
        return ""

    def sections(self) -> list[str]:
        """Nasıl karar verirsin bölümünün sonuna eklenen alt bölümleri döndürür, örneğin Rejimler."""
        return []

    def notebook_note(self) -> tuple[str, str]:
        """Defterin seviyeler notunun yanına eklenen cümleyi talimat ve şema için döndürür."""
        return "", ""

    def alarm_rule(self) -> str:
        """Hesabın kurallarındaki fiyat alarmı maddesine eklenen cümleyi döndürür, sistemin kurduğu alarmlar için."""
        return ""

    def validate(self, k: dict, ctx: Context, old: Notebook, review: Review) -> None:
        """Kararın bütününe bakar, hataları, iptalleri ve notları review'a ekler."""

    def entry_problem(self, a: dict, cmd: PlaceOrder, k: dict, review: Review, ctx: Context, leverage: int,
                      busy: set[tuple[str, str]], at: int) -> str | None:
        """Bir girişin bu adıma göre ret metnini döndürür, sorun yoksa None.

        Args:
            a: Opus'un giriş eylemi.
            cmd: Eylemin çevrildiği giriş komutu, miktar ve fiyat buradan okunur.
            k: Kararın tamamı.
            review: Kararın denetim sonucu.
            ctx: Uyanmanın bağlamı.
            leverage: Girişin kaldıracı, SL'siz girişin marjini için.
            busy: Bu kararda daha önce giriş verilen parite ve yönler.
            at: Giriş alanları adımının numarası, ret metninde girişin hangi adımda takıldığı için.
        """
        return None

    def entry_note(self, a: dict, cmd: PlaceOrder, k: dict, review: Review) -> dict:
        """Geçen bir girişin notunun reason alanına eklediği anahtarları döndürür, örneğin oyun planı."""
        return {}

    def record(self, k: dict) -> dict:
        """Günlük kaydına eklediği alanları döndürür, örneğin rozet."""
        return {}

    def checks(self, review: Review) -> dict:
        """Kaydın checks alanına eklediği alanları döndürür."""
        return {}

    def swings(self, k: dict) -> dict:
        """Not defterine taşınan tepe ve dipleri parite ve aralık başına döndürür."""
        return {}

    def alarms(self, k: dict, review: Review) -> list[dict]:
        """Kararın kurduğu ya da kaldırdığı sistem alarmlarını döndürür."""
        return []

    def wait_until(self, review: Review) -> int | None:
        """Bir sonraki çağrının en geç anını döndürür, yoksa None."""
        return None


class Steps:
    """Bir strateji türünün karar adımları, talimattaki sırasıyla.

    Raises:
        ValueError: Aynı anahtarlı iki adım varsa.
    """

    def __init__(self, steps: list[DecisionStep]) -> None:
        keys = [s.key for s in steps]
        if len(keys) != len(set(keys)):
            raise ValueError(f"Aynı karar adımı iki kez verildi: {', '.join(keys)}.")
        n = 1
        for s in steps:
            s.first = n
            n += s.size
        self.items = list(steps)
        self.count = n - 1
        self.keys = tuple(keys)
        # Girişin ret metinleri giriş alanları adımının numarasını taşır.
        self.entry_no = next((s.first for s in steps if s.key == "entry"), self.count)

    def count_word(self) -> str:
        return _COUNT_WORDS.get(self.count, str(self.count))

    def numbered(self) -> list[str]:
        """Talimattaki adımları numaralarıyla döndürür."""
        out = []
        for s in self.items:
            out += [f"{s.first + i}. {t}" for i, t in enumerate(s.texts())]
        return out

    def fields(self, symbols: list[str]) -> dict[str, dict]:
        out = {}
        for s in self.items:
            out.update(s.fields(symbols))
        return out

    def entry_rules(self) -> str:
        return " ".join(t for s in self.items if (t := s.entry_rules()))

    def sections(self) -> list[str]:
        return [x for s in self.items for x in s.sections()]

    def notebook_note(self) -> tuple[str, str]:
        notes = [s.notebook_note() for s in self.items]
        return " ".join(p for p, _ in notes if p), " ".join(q for _, q in notes if q)

    def alarm_rule(self) -> str:
        return " ".join(t for s in self.items if (t := s.alarm_rule()))

    def validate(self, k: dict, ctx: Context, old: Notebook) -> Review:
        """Kararın bütününü adımların sırasıyla denetler."""
        review = Review()
        for s in self.items:
            s.validate(k, ctx, old, review)
        return review

    def entry_problem(self, a: dict, cmd: PlaceOrder, k: dict, review: Review, ctx: Context, leverage: int,
                      busy: set[tuple[str, str]]) -> str | None:
        """Bir girişin ilk takıldığı denetimin ret metnini döndürür, geçerse None."""
        for s in self.items:
            if why := s.entry_problem(a, cmd, k, review, ctx, leverage, busy, self.entry_no):
                return why
        return None

    def entry_note(self, a: dict, cmd: PlaceOrder, k: dict, review: Review) -> dict:
        out = {}
        for s in self.items:
            out.update(s.entry_note(a, cmd, k, review))
        return out

    def record(self, k: dict) -> dict:
        out = {}
        for s in self.items:
            out.update(s.record(k))
        return out

    def checks(self, review: Review) -> dict:
        out = {}
        for s in self.items:
            out.update(s.checks(review))
        return out

    def swings(self, k: dict) -> dict:
        out = {}
        for s in self.items:
            out.update(s.swings(k))
        return out

    def alarms(self, k: dict, review: Review) -> list[dict]:
        return [x for s in self.items for x in s.alarms(k, review)]

    def wait_until(self, review: Review) -> int | None:
        times = [t for s in self.items if (t := s.wait_until(review)) is not None]
        return min(times) if times else None
