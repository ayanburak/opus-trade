"""
file: entry.py
importable: RISK_SLACK, EntryStep

Giriş alanları, her strateji türünde son adımdır. piyasa_giris ve emir_ver
eylemlerinde karşı kanıt, risk beyanı ve geçersizlik zorunludur, son işlem
aynı yönde stopla kapandıysa son stoptan farkı da. Denetim karşı kanıtın
boş olmadığına, geçersizlik fiyatının doğru tarafta olduğuna ve gerçek
riskin beyanı aşmadığına bakar.

description: Giriş alanları adımı ve gerçek riskin hesabı.
"""

# NOTE: Risk beyanı bir tavan değildir. Opus istediği riski alır, yalnızca
# beyanı gerçek rakamla tutarlı olmak zorundadır.
#
# NOTE: Risk hesabı kâğıt broker'ın formülleriyle yapılır, ayrı bir kâr ve
# zarar formülü yoktur. SL'siz girişin riski izole marjinin tamamıdır, en
# kötü durumda likidasyonda giden odur.

from ....domain import numbers, rules
from ....domain.fees import FeeSchedule
from ....domain.models import Side
from ....domain.trading import PlaceOrder
from ...base import Context
from ..checks import Review, entry_price, fail, worst_stop
from ..texts import price
from .base import DecisionStep

# Gerçek risk beyanı bu kat aşarsa giriş reddedilir. Komisyon ve kayma gibi
# Opus'un tam hesaplayamayacağı paylar için yer bırakır.
RISK_SLACK = 1.25

# Karşı kanıt alanında yok anlamına gelen kısa cevaplar.
_EMPTY_WORDS = ("yok", "yoktur", "bulunmuyor", "bulunamadı", "görünmüyor", "none", "-", "hiç yok",
                "karşı kanıt yok", "aykırı veri yok")

# Karşı kanıtın en kısa boyu. Bir veri ve neden ağır basmadığı bir cümleden
# kısa olamaz.
_MIN_COUNTER = 15


def _loss(side: Side, entry: float, qty: float, stop: float | None, margin: float, taker: float) -> float:
    if stop is None:
        return margin + qty * entry * taker
    return max(0.0, -rules.unrealized_pnl(side, entry, qty, stop)) + qty * entry * taker + qty * stop * taker


def _counter(text: str) -> bool:
    t = text.strip().lower().rstrip(".!")
    return len(t) >= _MIN_COUNTER and t not in _EMPTY_WORDS and not t.startswith("yok")


class EntryStep(DecisionStep):
    """Giriş alanları adımı.

    Args:
        fees: Komisyon oranları, gerçek riskin hesabı için.
    """

    key = "entry"

    def __init__(self, fees: FeeSchedule) -> None:
        super().__init__()
        self.fees = fees

    def texts(self) -> list[str]:
        slack = numbers.num(RISK_SLACK, 2)
        return ["Giriş alanları. Her girişte riskini, tezini bozan koşulu ve tezine aykırı veriyi bilerek girmen için "
                "var. piyasa_giris ve emir_ver eylemlerinde karşı kanıt, risk beyanı ve geçersizlik zorunludur:\n"
                "- karsi_kanit: Girişe aykırı en güçlü veri ve neden ağır basmadığı. Boş ya da yok anlamında olamaz.\n"
                "- risk_beyani: SL'de kaybedeceğin tutarın özkaynağına oranı ve bu riski neden aldığın. Sistem gerçek "
                "riski hesaplar: girişin SL'deki zararı ve iki yönlü taker komisyonu, aynı yöndeki açık pozisyonun en "
                "kötü stopundaki zararla birlikte. SL yoksa girişin marjininin tamamı risktir. Gerçek risk beyanının "
                f"{slack} katını aşarsa giriş reddedilir ve gerçek rakam sana söylenir.\n"
                "- gecersizlik: Tezini bozan koşul, bir cümle ve bir fiyat. Fiyat short'ta girişin üstünde, long'da "
                "altındadır. Bekleyen emir dolmadan son fiyat oraya değerse sistem emri iptal eder, iptali sonraki "
                "çağrında hesap bölümünde görürsün. Pozisyon açıkken son fiyat oraya değerse hemen çağrılırsın.\n"
                "- son_stoptan_farki: Bir paritede bir yöndeki son işlemin zararla stoplandıysa aynı yöndeki yeni "
                "girişte zorunludur. Art arda gelen stoplar piyasanın değiştiğini gösteren bir veridir."]

    def entry_rules(self) -> str:
        return ("Girişin gerekçesi ve geçersizliği emre bağlı saklanır, hesap bölümünde emrin ve pozisyonun yanında "
                "görünür.")

    def entry_problem(self, a: dict, cmd: PlaceOrder, k: dict, review: Review, ctx: Context, leverage: int,
                      busy: set[tuple[str, str]], at: int) -> str | None:
        sym, side = cmd.symbol, cmd.side
        entry = entry_price(cmd, ctx)
        if not _counter(a["karsi_kanit"]):
            return fail(f"adım {at}, karşı kanıt", "girişe aykırı en güçlü veriyi ve neden ağır basmadığını yaz. Boş "
                                                   "ya da yok anlamında bir cevap kabul edilmez.")
        inv = a["gecersizlik"]["fiyat"]
        if (side is Side.SHORT and inv <= entry) or (side is Side.LONG and inv >= entry):
            where = "üstünde" if side is Side.SHORT else "altında"
            return fail(f"adım {at}, geçersizlik", f"{side} girişte geçersizlik fiyatı girişin {where} olmalı. Giriş "
                                                   f"{price(entry)}, geçersizlik {price(inv)}.")
        if ctx.facts.last_stop.get(f"{sym} {side}") and not (a.get("son_stoptan_farki") or "").strip():
            return fail(f"adım {at}, son stoptan farkı", f"{sym} {side} yönündeki son işlemin stopla kapandı. Bu "
                                                         f"girişin ondan farkını son_stoptan_farki alanına yaz.")
        return self._risk(a, cmd, ctx, leverage, at)

    # Gerçek risk: bu girişin SL'deki zararı ve iki yönlü taker komisyonu,
    # aynı yöndeki açık pozisyonun en kötü stopundaki zararla birlikte.
    def _risk(self, a: dict, cmd: PlaceOrder, ctx: Context, leverage: int, at: int) -> str | None:
        sym, side = cmd.symbol, cmd.side
        entry = entry_price(cmd, ctx)
        sl = a.get("sl")
        p = ctx.state.position(sym, side)
        margin = cmd.qty * entry / leverage
        loss = _loss(side, entry, cmd.qty, sl, margin, self.fees.taker)
        if p is not None:
            loss += _loss(side, p.entry_price, p.qty, worst_stop(ctx, sym, side), p.margin, self.fees.taker)
        equity = ctx.state.equity
        real = loss / equity * 100 if equity > 0 else float("inf")
        said = float(a["risk_beyani"]["yuzde"])
        if real > said * RISK_SLACK + 1e-9:
            basis = "SL'deki zarar ve komisyon" if sl is not None else "SL olmadığı için marjinin tamamı ve komisyon"
            return fail(f"adım {at}, risk beyanı", f"beyanın yüzde {numbers.num(said, 2)}, gerçek risk yüzde "
                                                   f"{numbers.num(real, 2)}: {basis} {numbers.num(loss, 2)} dolar, "
                                                   f"özkaynak {numbers.num(equity, 2)} dolar"
                                                   f"{', aynı yöndeki açık pozisyon dahil' if p else ''}. Beyanını "
                                                   f"gerçek rakamla yaz ya da girişi değiştir.")
        return None
