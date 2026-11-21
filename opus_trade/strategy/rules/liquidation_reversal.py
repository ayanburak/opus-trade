"""
file: liquidation_reversal.py
importable: LiquidationReversal

Likidasyon dalgasından sonraki geri dönüşü işleyen kural stratejisi. Bir
likidasyon dalgası tetikleyicisi uyandırınca dalganın paritesinde, baskın
tarafın yönünde piyasa fiyatından işlem açar. TP girişten aralığın bir oranı
kadar, SL dalganın ucunun ötesinde durur. Pozisyon belirlenen süre dolunca
hâlâ açıksa kapatılır.

description: Likidasyon dalgası sonrası geri dönüş kuralı.
"""

# NOTE: Kurallar 18.11 testinden gelir, raporu .claude/research/likidasyon-dalgasi.md.
# Eskiden Opus bu kuralları tarz metninden okuyup uyguluyordu. Burada kodla ve
# her seferinde aynı şekilde uygulanır, Opus çağrılmaz. Aralık dalganın en
# yüksek ve en düşük fiyatının farkıdır, tetikleyici bunu mumlardan ölçer.
#
# NOTE: Baskın taraf long ise long açılır. Long'ların zorla kapatılması fiyatı
# aşağı iter, kural bu itişin bittiği yerden geri dönüşü bekler.
#
# NOTE: Her paritede aynı anda en fazla bir pozisyon olur, o paritede açık
# pozisyon varken gelen dalga atlanır. Farklı paritelerde nakit yettiği kadar
# pozisyon açılabilir.
#
# NOTE: Kaldıraç paritenin ayarıdır. Paritenin kaldıracı kuralınkinden farklıysa
# girişten önce kaldıraç ayarlanır. Paritede açık pozisyon olmadığı için ayar
# her zaman kabul edilir, kaldıracın paritenin üst sınırını aşmasını broker
# reddeder.

from ...domain import rules
from ...domain.catalog import Catalog
from ...domain.models import Side
from ...domain.trading import ClosePosition, Command, OrderType, PlaceOrder, Protection, SetLeverage
from ...registry import Param
from ...triggers.liquidation_wave import Wave
from ..base import STRATEGIES, Context, Decision, Strategy


class LiquidationReversal(Strategy):
    """Likidasyon dalgası sonrası geri dönüş kuralı.

    Args:
        notional: Her işlemin büyüklüğü, dolar.
        leverage: Kaldıraç.
        tp_range: TP'nin girişten uzaklığı, aralığın oranı olarak.
        min_tp_pct: TP uzaklığı fiyatın bu yüzdesinden kısaysa işlem açılmaz.
        sl_range: SL'nin dalganın ucundan uzaklığı, aralığın oranı olarak.
        hold_min: Pozisyon bu kadar dakika sonra hâlâ açıksa kapatılır.
        catalog: Miktarın adıma yuvarlanması için parite kuralları.
    """

    def __init__(self, notional: float, leverage: float, tp_range: float, min_tp_pct: float, sl_range: float,
                 hold_min: int, catalog: Catalog) -> None:
        self.notional = notional
        self.leverage = int(round(leverage))
        self.catalog = catalog
        self.tp_range = tp_range
        self.min_tp_pct = min_tp_pct
        self.sl_range = sl_range
        self.hold_s = hold_min * 60

    # Dalga işlenmez ise nedeni kayda geçer, admin panelinde neden işlem
    # açılmadığı görülsün diye.
    def _entry(self, w: Wave, ctx: Context, busy: set[str], cash: float) -> tuple[list[Command], float, str]:
        price = ctx.prices.get(w.symbol)
        if w.symbol not in ctx.symbols:
            return [], 0.0, "parite profilde yok"
        if w.symbol in busy:
            return [], 0.0, "paritede açık pozisyon var"
        if price is None:
            return [], 0.0, "fiyat henüz yok"
        span = w.high - w.low
        tp_dist = span * self.tp_range
        if tp_dist < price * self.min_tp_pct / 100:
            return [], 0.0, f"TP uzaklığı {tp_dist:g}, fiyatın yüzde {self.min_tp_pct:g}'ünden kısa"
        margin = self.notional / self.leverage
        if margin > cash:
            return [], 0.0, "nakit yetmiyor"
        side = w.dominant
        tp = price + tp_dist if side is Side.LONG else price - tp_dist
        sl = w.low - span * self.sl_range if side is Side.LONG else w.high + span * self.sl_range
        qty = rules.qty_for_margin(self.catalog.get(w.symbol), self.leverage, margin, price)
        out: list[Command] = []
        if ctx.state.leverage(w.symbol) != self.leverage:
            out.append(SetLeverage(w.symbol, self.leverage))
        out.append(PlaceOrder(w.symbol, side, OrderType.MARKET, qty=qty,
                              protection=(Protection(OrderType.TAKE_PROFIT, tp), Protection(OrderType.STOP, sl))))
        return out, margin, "açıldı"

    async def decide(self, ctx: Context) -> Decision:
        d = Decision()
        for p in ctx.state.positions:
            if ctx.now - p.entry_time >= self.hold_s:
                d.commands.append(ClosePosition(p.symbol, p.side))
                d.labels.append(f"{p.symbol} {p.side} süre doldu, kapat")
        busy = {p.symbol for p in ctx.state.positions}
        cash = ctx.state.available
        notes = []
        for wake in ctx.wakes:
            fire = wake.data
            if wake.kind != "trigger" or getattr(fire, "kind", None) != "liquidation_wave":
                continue
            cmds, margin, why = self._entry(fire.data, ctx, busy, cash)
            notes.append({"symbol": fire.symbol, "result": why})
            for cmd in cmds:
                d.commands.append(cmd)
                d.labels.append(f"{cmd.symbol} kaldıraç {cmd.leverage}x" if isinstance(cmd, SetLeverage)
                                else f"{cmd.symbol} {cmd.side} aç")
            if cmds:
                busy.add(fire.symbol)
                cash -= margin
        # Açık ya da yeni açılan her pozisyonun süresi dolunca uyanılır.
        ends = [p.entry_time + self.hold_s for p in ctx.state.positions if ctx.now - p.entry_time < self.hold_s]
        ends += [ctx.now + self.hold_s for c in d.commands if isinstance(c, PlaceOrder)]
        d.wait_s = max(1, min(ends) - ctx.now) if ends else None
        d.record = {"waves": notes}
        return d


STRATEGIES.register(
    "liquidation_reversal", "Likidasyon geri dönüşü",
    "Likidasyon dalgası durulunca dalganın paritesinde baskın taraf yönünde, sabit kurallarla işlem açar. "
    "Profilde likidasyon dalgası tetikleyicisi olmalıdır.",
    (Param("notional", "float", "İşlem büyüklüğü, dolar", default=75.0, low=5, high=100000),
     Param("leverage", "float", "Kaldıraç", default=20.0, low=1, high=150),
     Param("tp_range", "float", "TP uzaklığı, aralığın oranı", default=0.5, low=0.05, high=5),
     Param("min_tp_pct", "float", "En kısa TP uzaklığı, yüzde", default=0.3, low=0, high=10),
     Param("sl_range", "float", "SL'nin dalga ucundan uzaklığı, aralığın oranı", default=0.5, low=0, high=5),
     Param("hold_min", "int", "En uzun tutma süresi, dakika", default=60, low=1, high=10080)),
    lambda deps, **p: LiquidationReversal(**p, catalog=deps.catalog),
    LiquidationReversal,
)
