"""
file: schema.py
importable: schema

Opus'un kararının JSON şeması. schema() profilin paritelerine ve strateji
türünün karar adımlarına göre şemayı kurar. Alanların sırası Opus'un
düşünme sırasıdır: önce adımların alanları, sonra analiz ve eylemler, en son
defter ve özet. Adımların alanları ve seçenekleri steps paketindedir.

description: Opus'un karar şeması.
"""

# NOTE: claude komutu şemayı tamamen zorlar: maxLength, minItems, enum, const,
# pattern ve iç içe anyOf. Uymayan çıktı modele hatasıyla döner ve model aynı
# çağrıda düzeltir, 22.11 ölçümü. Bu yüzden biçim şemada durur, rakam ve
# hesap isteyen denetimler adımlardadır.
#
# XXX: Talimatta ve açıklamalarda düşünce süreci gibi ifadeler kullanılmaz,
# Opus çağrıyı reddediyor. Kontrol, gerekçe ve sınama kullanılır.
#
# XXX: Alan adları ve seçenekler Opus'a görünür, Türkçe kalır.

from . import actions, notebook
from .steps.base import Steps


def schema(symbols: list[str], steps: Steps) -> dict:
    """Opus'un kararının uyması gereken JSON şemasını döndürür.

    Args:
        symbols: Profilin pariteleri.
        steps: Strateji türünün karar adımları, alanlarını bu sırayla ekler.
    """
    props = {
        **steps.fields(symbols),
        "analiz": {"type": "string", "description": "Adımlara sığmayan değerlendirmen ve kararının gerekçesi."},
        "eylemler": {"type": "array", "items": actions.schema(list(symbols))},
        "not_defteri": notebook.schema(steps.notebook_note()[1]),
        "ozet": {"type": "string", "description": "Tek cümle."},
        "sonraki_cagri_dk": {"type": "integer"},
        "yoneticiye_cevap": {"type": "string", "description": "Yöneticinin sorularına, verdiği haberlere ve "
                             "araştırmanı istediği konulara cevabın. Böyle bir müdahale yoksa boş bırak."},
        "yoneticiye_oneri": {"type": "string", "description": "İşlemlerinde gördüğün bir örüntü ve tarzına önerin. "
                             "Yoksa boş bırak. Deftere değil buraya yazılır."},
    }
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}
