"""
file: base.py
importable: Wake, Context, Decision, ActionResult, Deps, Strategy, STRATEGIES

Strategy bir profilin karar veren parçasıdır. decide() bir Context alır ve bir
Decision döndürür. Context stratejiyi neyin uyandırdığını, hesabın halini,
piyasa verisine erişimi, son kararları ve stratejinin kendi hafızasını taşır.
Decision hesaba gönderilecek komutları, kurulacak ya da iptal edilecek
alarmları, bir sonraki uyanma süresini ve kaydedilecek ayrıntıları taşır.
STRATEGIES strateji türlerinin kayıt defteridir.

description: Strateji soyut sınıfı, bağlamı, kararı ve tür kayıt defteri.
"""

# NOTE: Strateji hesaba hiçbir zaman doğrudan dokunmaz. Kararındaki komutları
# çalıştırıcı kapıdan geçirir, sonuçlarını bir sonraki uyanmada Wake olarak
# geri verir. Bu yüzden aynı strateji kâğıt ve canlı hesapta hiç değişmeden
# çalışır.
#
# NOTE: Yetenekler sınıf bayraklarıdır. accepts_messages yönetici mesajlarını,
# yani teklif, soru ve haberi okuyabilen stratejidedir, admin paneli bu
# müdahaleleri yalnızca orada açar. corrects reddedilen bir komuttan hemen sonra
# düzeltme için yeniden uyandırılmak isteyen stratejidedir. Kural stratejisi
# ikisini de istemez, aynı girdiyle aynı reddi tekrar alırdı. uses_model model
# çağıran stratejidedir, model erişimi kapalıyken o strateji bekletilir.
#
# NOTE: memory stratejinin uyanmalar arasında taşıdığı kendi durumudur,
# örneğin Opus'un not defteri. Çalıştırıcı onu JSON olarak saklar ve bir
# sonraki uyanmada aynen geri verir.

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, ClassVar

from ..domain.catalog import Catalog
from ..domain.clock import Clock
from ..domain.trading import AccountState, Command
from ..market.source import MarketData
from ..registry import Registry


# kind uyandırmanın türüdür: timer, account, trigger, admin, correction,
# restart ya da shutdown. data türe göre değişir: hesap olayı, tetikleyicinin
# bildirimi, yönetici mesajı ya da düzeltmede ActionResult listesi.
@dataclass(frozen=True)
class Wake:
    kind: str
    time: int
    data: Any = None


@dataclass(frozen=True)
class ActionResult:
    """Bir komutun sonucu. label stratejinin kendi diliyle komutun kısa adıdır, message sonucun ya da reddin metnidir."""

    label: str
    ok: bool
    message: str


@dataclass
class Context:
    """Bir uyanmada stratejinin görebildiği her şey.

    Args:
        now: Uyanma anı, unix saniye.
        wakes: Bu uyanmanın nedenleri, eskiden yeniye.
        state: Hesabın hali.
        prices: Paritelerin son fiyatları.
        marks: Paritelerin mark fiyatları. SL, iz süren stop ve likidasyon buna bakar.
        symbols: Profilin işlem yapabildiği pariteler.
        market: Piyasa verisi.
        memory: Stratejinin önceki uyanmadan bıraktığı hafıza.
        history: Son kararların kısa kayıtları, eskiden yeniye.
        admin: Yönetici bölümünün satırları, yalnızca accepts_messages olan stratejiye dolu gelir.
        alerts: Profilin kurulu fiyat alarmları.
        stats: Kapanmış işlemlerin özeti: count, wins, net ve fee.
        start_balance: Hesabın başlangıç bakiyesi.
        on_tool: LLM stratejisinin her araç kullanımında çağırdığı fonksiyon, canlı gösterim için.
    """

    now: int
    wakes: list[Wake]
    state: AccountState
    prices: dict[str, float]
    symbols: tuple[str, ...]
    market: MarketData
    marks: dict[str, float] = field(default_factory=dict)
    memory: dict[str, Any] = field(default_factory=dict)
    history: list[dict] = field(default_factory=list)
    admin: dict[str, list[str]] = field(default_factory=dict)
    alerts: list = field(default_factory=list)
    stats: dict[str, float] = field(default_factory=dict)
    start_balance: float = 0.0
    on_tool: Callable[[dict], None] | None = None


# commands sırayla kapıya gider, biri reddedilse diğerleri yine denenir.
# labels her komutun stratejinin dilindeki adıdır, sonuç ona bu adla döner.
# alarms kurulacak ya da iptal edilecek alarmlardır. wait_s bir sonraki
# uyanmaya kadar beklenecek süredir, None ise strateji yalnızca olayla uyanır.
@dataclass
class Decision:
    commands: list[Command] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    alarms: list[dict] = field(default_factory=list)
    errors: list[ActionResult] = field(default_factory=list)
    wait_s: int | None = None
    memory: dict[str, Any] = field(default_factory=dict)
    record: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Deps:
    """Strateji türlerinin kurulurken alabildiği ortak parçalar."""

    clock: Clock
    catalog: Catalog
    llm: Any = None
    paths: dict[str, Any] = field(default_factory=dict)


class Strategy(ABC):
    accepts_messages: ClassVar[bool] = False
    corrects: ClassVar[bool] = False
    uses_model: ClassVar[bool] = False

    @abstractmethod
    async def decide(self, ctx: Context) -> Decision:
        """Bağlama bakıp kararını döndürür."""


STRATEGIES: Registry[Strategy] = Registry("strateji türü")
