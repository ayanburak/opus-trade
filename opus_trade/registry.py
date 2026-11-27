"""
file: registry.py
importable: Param, Entry, Registry, validate

Strateji türleri, tetikleyiciler ve LLM araçları gibi eklenebilir parçalar
kendilerini bir Registry'ye kaydeder. Her kayıt bir anahtar, Türkçe bir ad,
bir açıklama, parametrelerinin şeması ve parçayı kuran fonksiyonu taşır.
Admin paneli formlarını, arayüz etiketlerini ve doğrulamayı buradan alır.
validate() bir parametre sözlüğünü şemaya göre denetler, eksikleri
varsayılanla doldurur ve hatada Türkçe bir ValueError fırlatır.

description: Eklenebilir parçaların kayıt defteri ve parametre şeması.
"""

# NOTE: Yeni bir strateji türü ya da tetikleyici eklemek yalnızca kendi
# dosyasını yazıp register() çağırmaktır. Arayüzün ve testlerin listelerine
# elle satır eklenmez, hepsi kayıt defterini okur.
#
# NOTE: Param.kind değerleri formun hangi alanla çizileceğini de söyler: int,
# float, text, choice, multi, symbols ve symbol_map. choice tek seçimdir ve
# seçmeli kutuyla, multi choices içinden bir ya da daha çok seçimdir ve işaret
# kutularıyla çizilir. symbol_map her parite için bir sayı taşır, örneğin
# likidasyon dalgasının parite başına eşiği. labels doluysa choices'ın formda
# görünen Türkçe adlarıdır, aynı sırayla.
#
# NOTE: cls kaydın kurduğu sınıftır, verilmesi isteğe bağlıdır. Kontrol API'si
# stratejinin yetenek bayraklarını, örneğin accepts_messages, parçayı kurmadan
# buradan okur. Bayrağın tek kaynağı yine sınıfın kendisidir.

from dataclasses import dataclass, field
from typing import Any, Callable, Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class Param:
    """Bir parametrenin şeması.

    Args:
        name: Parametrenin İngilizce adı, kayıtta bu anahtarla durur.
        kind: int, float, text, choice, multi, symbols ya da symbol_map.
        label: Formda görünen Türkçe ad.
        default: Verilmezse kullanılan değer. None ise parametre zorunludur.
        low: Sayılarda en küçük değer. Metinde doluysa metin boş ya da yalnızca boşluk olamaz.
        high: Sayılarda en büyük değer.
        choices: choice ve multi türünde geçerli değerler.
        help: Formda alanın altında görünen kısa açıklama.
        labels: choices'ın formda görünen adları, boşsa değerlerin kendisi görünür.
    """

    name: str
    kind: str
    label: str
    default: Any = None
    low: float | None = None
    high: float | None = None
    choices: tuple[str, ...] = ()
    help: str = ""
    labels: tuple[str, ...] = ()


def _number(p: Param, v: Any) -> float:
    try:
        x = int(v) if p.kind == "int" else float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{p.label} bir sayı olmalı, {v!r} verildi.") from None
    if p.low is not None and x < p.low or p.high is not None and x > p.high:
        raise ValueError(f"{p.label} {p.low:g} ile {p.high:g} arasında olmalı, {x:g} verildi.")
    return x


def _one(p: Param, v: Any) -> Any:
    if p.kind in ("int", "float"):
        return _number(p, v)
    if p.kind == "text":
        if p.low and not str(v).strip():
            raise ValueError(f"{p.label} boş olamaz.")
        return str(v)
    if p.kind == "choice":
        if v not in p.choices:
            raise ValueError(f"{p.label} şunlardan biri olmalı: {', '.join(p.choices)}.")
        return v
    # Seçimler choices'ın sırasıyla döner, aynı seçim iki kez sayılmaz.
    if p.kind == "multi":
        if not isinstance(v, (list, tuple)) or not v:
            raise ValueError(f"{p.label} için en az bir seçim yapılmalı.")
        if bad := [x for x in v if x not in p.choices]:
            raise ValueError(f"{p.label} şunlardan seçilmeli: {', '.join(p.choices)}. Geçersiz: {', '.join(map(str, bad))}.")
        return tuple(c for c in p.choices if c in v)
    if p.kind == "symbols":
        if not isinstance(v, (list, tuple)) or not v:
            raise ValueError(f"{p.label} en az bir parite içermeli.")
        return tuple(str(s).upper() for s in v)
    if p.kind == "symbol_map":
        if not isinstance(v, dict):
            raise ValueError(f"{p.label} paritelerden sayılara bir eşleme olmalı.")
        return {str(k).upper(): _number(p, x) for k, x in v.items()}
    raise ValueError(f"Bilinmeyen parametre türü: {p.kind}")


def validate(params: tuple[Param, ...], given: dict[str, Any]) -> dict[str, Any]:
    """Verilen değerleri şemaya göre denetler ve eksikleri varsayılanla doldurur.

    Raises:
        ValueError: Bilinmeyen bir parametre varsa, zorunlu bir parametre eksikse ya da bir değer geçersizse.
    """
    known = {p.name for p in params}
    unknown = set(given) - known
    if unknown:
        raise ValueError(f"Bilinmeyen parametre: {', '.join(sorted(unknown))}.")
    out = {}
    for p in params:
        if p.name in given:
            out[p.name] = _one(p, given[p.name])
        elif p.default is not None:
            out[p.name] = p.default
        else:
            raise ValueError(f"{p.label} verilmeli.")
    return out


@dataclass(frozen=True)
class Entry(Generic[T]):
    key: str
    name: str
    description: str
    params: tuple[Param, ...]
    build: Callable[..., T]
    cls: type | None = None


@dataclass
class Registry(Generic[T]):
    """Anahtardan kayda bir defter. Aynı anahtar iki kez kaydedilemez."""

    kind: str
    entries: dict[str, Entry[T]] = field(default_factory=dict)

    def register(self, key: str, name: str, description: str, params: tuple[Param, ...],
                 build: Callable[..., T], cls: type | None = None) -> None:
        if key in self.entries:
            raise ValueError(f"{self.kind} {key} zaten kayıtlı.")
        self.entries[key] = Entry(key, name, description, params, build, cls)

    def get(self, key: str) -> Entry[T]:
        try:
            return self.entries[key]
        except KeyError:
            raise ValueError(f"Bilinmeyen {self.kind}: {key}. Kayıtlı olanlar: {', '.join(self.entries)}.") from None
