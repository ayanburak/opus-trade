"""
file: base.py
importable: LLMRequest, LLMResult, LLMError, LLMClient

LLMClient bir istemi modele gönderip cevabı döndüren parçanın soyut
sınıfıdır. İstek bir şema taşıyorsa cevap şemaya uyan bir sözlüktür ve
LLMResult.decision'a yazılır. Şemasız istekte cevap serbest metindir ve
LLMResult.text'e yazılır. Çağrı başarısız olursa LLMError fırlatılır, o ana
kadar kullanılan araçlar hatada da taşınır.

    res = await llm.call(LLMRequest(system, prompt, schema, "opus", "high", 30, work_dir, toolset))

description: Model çağrısının soyut sınıfı ve veri modelleri.
"""

# NOTE: Strateji paketi de ekip paketi de yalnızca bu dosyaya bağlanır.
# Somut istemci claude_cli.py'dedir ve yalnızca kuruluş noktalarında seçilir.

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .toolset import Toolset


@dataclass(frozen=True)
class LLMRequest:
    """Bir model çağrısı.

    Args:
        system: Talimat.
        prompt: Bu çağrının istemi.
        schema: Cevabın uyması gereken JSON şeması. None ise cevap serbest metindir.
        model: Model takma adı, örneğin opus.
        effort: low, medium ya da high.
        max_turns: Araç adımlarının sert sınırı.
        work_dir: Çağrının çalışma klasörü.
        toolset: Modelin kullanabildiği araçlar.
        timeout_s: Çağrı bundan uzun sürerse süreç öldürülür.
        env: Model sürecine eklenen ortam değişkenleri, örneğin araç zaman aşımı.
    """

    system: str
    prompt: str
    schema: dict | None
    model: str
    effort: str
    max_turns: int
    work_dir: Path
    toolset: Toolset = field(default_factory=Toolset)
    timeout_s: int = 600
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class LLMResult:
    """Başarılı bir çağrının sonucu.

    Args:
        decision: Şemalı çağrıda şemaya uyan cevap, şemasızda None.
        raw: Modelin son mesajının ham hali.
        tools: Kullanılan araçlar, sırayla.
        usage: Tüketim: usage, model_usage ve num_turns.
        duration_s: Çağrının süresi.
        cost_usd: Çağrının API liste fiyatıyla karşılığı, abonelikten ayrıca ücret alınmaz.
        text: Şemasız çağrıda modelin cevabı, şemalıda None.
    """

    decision: dict | None
    raw: str
    tools: list[dict]
    usage: dict
    duration_s: float
    cost_usd: float | None
    text: str | None = None


# tools hataya kadar kullanılan araçlardır. record çağıranın hatalı çağrının
# kaydına eklemek istediği alanlardır, örneğin alt görevler. Çalıştırıcı ikisini
# de günlüğe yazar.
class LLMError(Exception):
    def __init__(self, message: str, tools: list[dict] | None = None, record: dict | None = None) -> None:
        super().__init__(message)
        self.tools = tools or []
        self.record = record or {}


class LLMClient(ABC):
    @abstractmethod
    async def call(self, req: LLMRequest, on_tool: Callable[[dict], None] | None = None) -> LLMResult:
        """İstemi modele gönderir ve cevabı döndürür.

        Args:
            req: Çağrı.
            on_tool: Her araç kullanımında çağrılır, canlı gösterim için.

        Raises:
            LLMError: Çağrı başarısız olursa, zaman aşımına ya da adım sınırına uğrarsa veya cevap okunamazsa.
        """
