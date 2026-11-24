"""
file: base.py
importable: Wake, Thesis, ClosedPosition, Facts, Context, Decision, ActionResult, Deps, Strategy, STRATEGIES

Strategy bir profilin karar veren parçasıdır. decide() bir Context alır ve bir
Decision döndürür. Context stratejiyi neyin uyandırdığını, hesabın halini,
piyasa verisine erişimi, son kararları, hesabın hazır gerçeklerini ve
stratejinin kendi hafızasını taşır. Facts defterden ve günlükten hesaplanmış,
stratejinin saymak zorunda kalmadığı gerçeklerdir.
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


# Bir girişin gerekçesi ve geçersizliği. reason stratejinin kendi alanlarıdır,
# LLM stratejisinde karşı kanıt, risk beyanı ve son stoptan farkı.
@dataclass(frozen=True)
class Thesis:
    symbol: str
    side: str
    reason: dict
    invalid_price: float
    invalid_text: str
    created_at: int


# Tamamen kapanmış bir pozisyon, bütün parçalarının toplamıyla. reason son
# parçanın kapanış nedenidir. origin pozisyonu kimin açtığıdır, strategy ya
# da admin. stopped zararla biten stop ya da likidasyondur, kârda tetiklenen
# iz süren stop sayılmaz.
@dataclass(frozen=True)
class ClosedPosition:
    position_id: int
    symbol: str
    side: str
    net: float
    reason: str
    closed_at: int
    origin: str
    stopped: bool


@dataclass(frozen=True)
class Facts:
    """Hesabın hazır gerçekleri. Strateji bunları saymaz, yorumlar.

    Args:
        today_count: Bugün tamamen kapanan pozisyon sayısı. Gün Türkiye saatiyle gece yarısı başlar.
        today_stops: Bugün zararla stoplanan pozisyon sayısı.
        today_net: Bugün kapanan parçaların net toplamı.
        loss_streak: Sondan geriye art arda zararla kapanan pozisyon sayısı.
        recent: Son beş kapanan pozisyon, eskiden yeniye.
        fill_rate: Stratejinin son bekleyen giriş emirlerinden dolanların sayısı ve toplamı.
        last_stop: Parite ve yön başına, sembol ile yön boşlukla birleşik anahtarla, son kapanan pozisyon stoplandı mı.
        position_origins: Açık pozisyonun kimliğinden kim açtığına, strategy ya da admin.
        order_origins: Bekleyen emrin kimliğinden kim verdiğine.
        unreviewed: Sınıflandırılmayı bekleyen pozisyonlar, eskiden yeniye.
        position_notes: Açık pozisyonun kimliğinden giriş notlarına.
        order_notes: Bekleyen emrin kimliğinden giriş notuna.
        invalidated: Son çağrıdan beri geçersizlik yüzünden sistemin iptal ettiği girişler ve iptal anları.
        last_call: Son başarılı kararın anı.
        late_min: Son kararın istediği uyanma anından bu yana geçen dakika, eşiği aştıysa.
        admin_24h: Yöneticinin son yirmi dört saatte hesapta yaptığı işlemler, saatiyle birer cümle.
        version_start: Yürürlükteki sürümün başlangıç anı.
        team_today: Rolden bugün çalışan alt görev sayısına. Gün Türkiye saatiyle gece yarısı başlar.
    """

    today_count: int = 0
    today_stops: int = 0
    today_net: float = 0.0
    loss_streak: int = 0
    recent: tuple[ClosedPosition, ...] = ()
    fill_rate: tuple[int, int] = (0, 0)
    last_stop: dict[str, bool] = field(default_factory=dict)
    position_origins: dict[int, str] = field(default_factory=dict)
    order_origins: dict[int, str] = field(default_factory=dict)
    unreviewed: tuple[ClosedPosition, ...] = ()
    position_notes: dict[int, tuple[Thesis, ...]] = field(default_factory=dict)
    order_notes: dict[int, Thesis] = field(default_factory=dict)
    invalidated: tuple[tuple[Thesis, int], ...] = ()
    last_call: int | None = None
    late_min: int | None = None
    admin_24h: tuple[str, ...] = ()
    version_start: int = 0
    team_today: dict[str, int] = field(default_factory=dict)


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
        facts: Hesabın hazır gerçekleri, çalıştırıcı kurar.
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
    facts: Facts = field(default_factory=Facts)
    on_tool: Callable[[dict], None] | None = None


# commands sırayla kapıya gider, biri reddedilse diğerleri yine denenir.
# labels her komutun stratejinin dilindeki adıdır, sonuç ona bu adla döner.
# chained bir komutun kendinden öncekine bağlı olduğunu söyler, önceki
# reddedilirse o gönderilmez. Tek bir eylemden çıkan kaldıraç ve giriş ya da
# yeni koruma ve eskisinin iptali böyle bir zincirdir, yarısı uygulanırsa
# pozisyon istenmeyen kaldıraçla açılır ya da korumasız kalır. Boşsa hiçbir
# komut zincirli değildir.
# alarms kurulacak ya da iptal edilecek alarmlardır. errors komuta hiç
# çevrilemeyen eylemlerin reddi, notes çevirinin kabul edilmiş açıklamasıdır.
# wait_s bir sonraki uyanmaya kadar beklenecek süredir, None ise strateji
# yalnızca olayla uyanır.
# rejection doluysa kararın bütünü kontrol hatasıyla reddedilmiştir: hiçbir
# komut uygulanmaz, hafıza yazılmaz, satırları reddin nedenleridir. Bu
# durumda wait_s yalnızca düzeltme hakkı bittiğinde kullanılır.
# annotations komutlarla aynı uzunluktadır, bir girişin komutunun hizasında
# girişin notu durur: symbol, side, reason, invalid_price ve invalid_text.
# Diğer komutların hizası None'dır. reviews kapanan pozisyonların
# sınıflandırmasıdır: position_id, verdict ve text. suggestion stratejinin
# yöneticiye önerisidir. alarms içinde op anahtarı expect olan bir işlem
# rejimin uygun olmadığı paritede beklenen fiyata kurulacak sistem alarmıdır.
@dataclass
class Decision:
    commands: list[Command] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    chained: list[bool] = field(default_factory=list)
    alarms: list[dict] = field(default_factory=list)
    errors: list[ActionResult] = field(default_factory=list)
    notes: list[ActionResult] = field(default_factory=list)
    wait_s: int | None = None
    memory: dict[str, Any] = field(default_factory=dict)
    record: dict[str, Any] = field(default_factory=dict)
    rejection: list[str] | None = None
    annotations: list[dict | None] = field(default_factory=list)
    reviews: list[dict] = field(default_factory=list)
    suggestion: str | None = None


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
