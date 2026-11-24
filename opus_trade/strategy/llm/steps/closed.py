"""
file: closed.py
importable: VERDICTS, ClosedStep

Kapanan işlemlerin sınıflandırılması. Hesap bölümünde sınıflandırılmayı
bekleyen her pozisyon bir kez sınıflandırılır. Denetim listenin
bekleyenlerle tuttuğuna bakar.

description: Kapanan işlemler adımı.
"""

# XXX: Sınıfların anahtarları Opus'a görünür, Türkçe kalır.

from ...base import Context
from ..checks import Review, fail
from ..notebook import Notebook
from .base import DecisionStep

# Kapanan pozisyonun sınıfları ve arayüzdeki etiketleri.
VERDICTS = {"tez_dogruydu": "tez doğruydu", "tez_yanlisti": "tez yanlıştı", "zamanlama_yanlisti": "zamanlama yanlıştı",
            "koruma_yanlis_yerdeydi": "koruma yanlış yerdeydi", "beklenmedik_olay": "beklenmedik olay"}


class ClosedStep(DecisionStep):
    key = "closed"

    def texts(self) -> list[str]:
        verdicts = ", ".join(VERDICTS.values())
        return [f"Son işlemler, kapanan_islemler. Kayıplardan ve kazançlardan öğrenmek ölçülebilir olsun diye var. "
                f"Hesap bölümünde sınıflandırılmayı bekleyen her pozisyonu bir kez sınıflandırırsın: {verdicts}, "
                f"yanına bir cümle. Beklenmedik olay, bir haberin ya da ani bir olayın tezini ve korumanı geçersiz "
                f"kıldığı işlemdir. Sınıflandırma işlemin kaydına bağlanır, kapanan işlemlerine araçla baktığında "
                f"görürsün. Yöneticinin açtığı ya da kapattığı pozisyonlar sınıflandırılmaz."]

    def fields(self, symbols: list[str]) -> dict[str, dict]:
        item = {"type": "object", "additionalProperties": False, "required": ["pozisyon_no", "sinif", "aciklama"],
                "properties": {"pozisyon_no": {"type": "integer"}, "sinif": {"type": "string", "enum": list(VERDICTS)},
                               "aciklama": {"type": "string", "minLength": 1, "description": "Bir cümle."}}}
        return {"kapanan_islemler": {
            "type": "array", "items": item,
            "description": f"Adım {self.first}. Hesap bölümünde sınıflandırılmayı bekleyen her pozisyon bir kez. "
                           f"Bekleyen yoksa boş liste."}}

    def validate(self, k: dict, ctx: Context, old: Notebook, review: Review) -> None:
        step = f"adım {self.first}, kapanan işlemler"
        want = {c.position_id for c in ctx.facts.unreviewed}
        given = [x["pozisyon_no"] for x in k["kapanan_islemler"]]
        if missing := sorted(want - set(given)):
            review.errors.append(fail(step, f"Sınıflandırılmayı bekleyen pozisyonlar listede yok: "
                                            f"{', '.join(f'#{n}' for n in missing)}."))
        if extra := sorted(set(given) - want):
            review.errors.append(fail(step, f"Sınıflandırılmayı beklemeyen pozisyonları yazdın: "
                                            f"{', '.join(f'#{n}' for n in extra)}. Yalnızca hesap bölümünde "
                                            f"bekleyenleri yaz."))
        if len(given) != len(set(given)):
            review.errors.append(fail(step, "Bir pozisyonu birden çok kez sınıflandırdın."))
