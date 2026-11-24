"""
file: spike_fade.py
importable: SpikeFade

İğne dönüşü kuralı. İşlem patlaması tetikleyicisi uyandırınca iğnenin
tersine, ucun ötesine bekleyen limit emirlerle girer: yükselen iğneye short,
çöken iğneye long. TP sıçramanın bir oranı geri alınınca, SL en uzak girişin
ötesinde son fiyatla tetiklenir. Pozisyon süre sınırını aşarsa piyasa
emriyle kapanır, dolmayan giriş emri ömrü dolunca iptal edilir.

description: Tekil işlem akışındaki iğneye karşı limitle giren hızlı kural stratejisi.
"""

# NOTE: Kural Burak'ın 24.11 getirdiği metinden gelir, metin ve uygulamadaki
# eksikler çalışmanın belgelerindedir. Metinde zarar kesme ve süre sınırı
# yoktu, ikisi de burada parametredir.
#
# NOTE: Büyüklük özkaynağın oranıdır ama Binance'in en küçük işlem
# büyüklüğünün altına inemez. Küçük hesapta bu yüzden pozisyon özkaynağın
# büyük bir payı olur, asıl sınır SL'deki olası kayıptır: kayıp özkaynağın
# max_risk_pct'sini geçecekse sinyal atlanır. İki giriş ancak ikisi de en küçük
# büyüklüğü geçebiliyorsa verilir, yoksa tek giriş verilir.
#
# NOTE: En küçük büyüklük girişin, TP'nin ve SL'nin en düşük fiyatında
# sağlanır. Long'un SL'si ve short'un TP'si girişin aşağısındadır, miktar
# yalnızca girişte en küçük büyüklüğü geçseydi o koruma emri büyüklük
# kuralıyla reddedilir ve long SL'siz kalırdı. 25.11'de uçtan uca testte
# görüldü. Binance'in kapatan emirlere bu kuralı uygulayıp uygulamadığı
# belgede açık değil, bu yol iki durumda da güvenlidir.
#
# NOTE: Kaldıraç toplam marj sınırının pozisyon başına payına sığan en küçük
# tamsayıdır. Yüksek kaldıraç riski büyütmez, risk SL'den gelir, ama izole
# marjinde likidasyon SL'den önce gelmemeli. Bu yüzden kaldıraç SL uzaklığının
# iki katının tersini, paritenin üst sınırını ve max_leverage'ı geçmez. Sığmazsa
# sinyal atlanır.
#
# NOTE: Her paritede aynı anda tek pozisyon olur. Paritede pozisyon ya da
# bekleyen giriş varken gelen sinyal atlanır. Pozisyon kapanınca o paritede
# kalan giriş emri hemen iptal edilir, iğnenin ikinci ucu boş bir hesaba
# yeniden pozisyon açmasın diye.

import math

from ...domain import rules
from ...domain.catalog import Catalog
from ...domain.fees import BINANCE_STANDARD
from ...domain.models import Side, SymbolSpec
from ...domain.trading import (CancelOrder, ClosePosition, Command, EventKind, OrderType, PlaceOrder, Protection, Ref,
                               SetLeverage)
from ...registry import Param
from ...triggers.trade_burst import Burst
from ..base import STRATEGIES, Context, Decision, Strategy


# Emrin ve korumalarının en düşük fiyatında da en küçük büyüklüğü geçen
# miktar. Bir tick pay bırakılır, kapı fiyatı tick'e yuvarlarken aşağı
# kaydırabilir.
def _min_qty(spec: SymbolSpec, low: float) -> float:
    low -= spec.tick
    steps = max(math.ceil(spec.min_notional / low / spec.qty_step - 1e-9), 1)
    return round(steps * spec.qty_step, 10)


class SpikeFade(Strategy):
    """İğne dönüşü kuralı.

    Args:
        size_pct: Pozisyonun özkaynağa oranı, yüzde. En küçük işlem büyüklüğünün altına inmez.
        max_risk_pct: SL'de olası kaybın özkaynağa en büyük oranı, yüzde.
        max_positions: Aynı anda en fazla pozisyon.
        max_margin_pct: Toplam marjinin özkaynağa en büyük oranı, yüzde.
        entry_pct: İlk limitin son fiyattan uzaklığı, yüzde.
        add_pct: İkinci limitin son fiyattan uzaklığı, yüzde.
        order_ttl_s: Dolmayan giriş emrinin ömrü, saniye.
        retrace: TP için sıçramanın geri alınacak oranı.
        min_tp_pct: TP'nin ilk girişten en küçük uzaklığı, yüzde.
        sl_pct: SL'nin en uzak girişten uzaklığı, yüzde.
        hold_s: Pozisyonun en uzun tutulma süresi, saniye.
        max_leverage: En yüksek kaldıraç.
        catalog: Parite kuralları.
    """

    fast = True

    def __init__(self, size_pct: float, max_risk_pct: float, max_positions: int, max_margin_pct: float,
                 entry_pct: float, add_pct: float, order_ttl_s: int, retrace: float, min_tp_pct: float,
                 sl_pct: float, hold_s: int, max_leverage: int, catalog: Catalog) -> None:
        self.size = size_pct / 100
        self.max_risk = max_risk_pct / 100
        self.max_positions = int(max_positions)
        self.max_margin = max_margin_pct / 100
        self.entry = entry_pct / 100
        self.add = add_pct / 100
        self.ttl = int(order_ttl_s)
        self.retrace = retrace
        self.min_tp = min_tp_pct / 100
        self.sl = sl_pct / 100
        self.hold = int(hold_s)
        self.max_leverage = int(max_leverage)
        self.catalog = catalog

    # --- Giriş ---

    # Bacaklar ilk limitten başlayarak fiyat ve miktar çiftleridir, yanında
    # SL'nin fiyatı. Hedefin yarısı iki bacakta da en küçük büyüklüğü
    # geçiyorsa iki bacak olur. SL en uzak bacağın ötesindedir.
    def _legs(self, spec: SymbolSpec, sign: int, tip: float, tp: float,
              equity: float) -> tuple[list[tuple[float, float]], float]:
        prices = [tip * (1 + sign * self.entry), tip * (1 + sign * self.add)]
        target = self.size * equity
        two_sl = prices[1] * (1 + sign * self.sl)
        two_min = _min_qty(spec, min(*prices, tp, two_sl))
        if all(target / 2 / p >= two_min for p in prices):
            return [(p, max(rules.round_qty(spec, target / 2 / p), two_min)) for p in prices], two_sl
        p1 = prices[0]
        sl = p1 * (1 + sign * self.sl)
        return [(p1, max(rules.round_qty(spec, target / p1), _min_qty(spec, min(p1, tp, sl))))], sl

    # SL'de olası kayıp: fiyat farkı, girişin maker komisyonu, çıkışın taker
    # komisyonu ve kayması.
    def _loss(self, spec: SymbolSpec, legs: list[tuple[float, float]], sl: float) -> float:
        out = 0.0
        for price, qty in legs:
            out += qty * abs(sl - price) + qty * price * BINANCE_STANDARD.maker
            out += qty * sl * (BINANCE_STANDARD.taker + spec.slippage)
        return out

    # Komutları, sonucun metnini ve girişlerin ayıracağı marjini döndürür.
    def _open(self, b: Burst, ctx: Context, used_margin: float) -> tuple[list[Command], str, float]:
        spec = self.catalog.get(b.symbol)
        equity = ctx.state.equity
        side = Side.SHORT if b.up else Side.LONG
        sign = 1 if b.up else -1
        tip = ctx.prices.get(b.symbol, b.tip)
        jump = abs(tip - b.start)
        p1 = tip * (1 + sign * self.entry)
        tp = (min(tip - self.retrace * jump, p1 * (1 - self.min_tp)) if b.up
              else max(tip + self.retrace * jump, p1 * (1 + self.min_tp)))
        legs, sl = self._legs(spec, sign, tip, tp, equity)
        loss = self._loss(spec, legs, sl)
        if loss > self.max_risk * equity:
            return [], f"SL'de olası kayıp {loss:.2f} dolar, özkaynağın %{self.max_risk * 100:g}'ünü geçiyor", 0.0
        notional = sum(p * q for p, q in legs)
        avg = notional / sum(q for _, q in legs)
        budget = self.max_margin * equity / self.max_positions
        lev = max(math.ceil(notional / budget - 1e-9), 1)
        safe = math.floor(1 / (2 * abs(sl - avg) / avg))
        cap = min(self.max_leverage, spec.brackets[0].max_leverage, safe)
        if lev > cap:
            return [], f"marj payına {lev}x gerekiyor, en fazla {cap}x olabilir", 0.0
        margin = notional / lev
        if used_margin + margin > self.max_margin * equity + rules.TOLERANCE:
            return [], "toplam marj sınırı dolu", 0.0
        if margin > ctx.state.available:
            return [], "nakit yetmiyor", 0.0
        out: list[Command] = []
        if ctx.state.leverage(b.symbol) != lev:
            out.append(SetLeverage(b.symbol, lev))
        protection = (Protection(OrderType.TAKE_PROFIT, tp), Protection(OrderType.STOP, sl, by_last=True))
        out += [PlaceOrder(b.symbol, side, OrderType.LIMIT, qty=q, price=p, protection=protection) for p, q in legs]
        return out, "açıldı", margin

    # --- Karar ---

    async def decide(self, ctx: Context) -> Decision:
        d = Decision()
        st = ctx.state

        def add(cmd: Command, label: str, chained: bool = False) -> None:
            d.commands.append(cmd)
            d.labels.append(label)
            d.chained.append(chained)

        for p in st.positions:
            if ctx.now - p.entry_time >= self.hold:
                add(ClosePosition(p.symbol, p.side), f"{p.symbol} {p.side} süre doldu, kapat")
        open_symbols = {p.symbol for p in st.positions}
        closed = {w.data.symbol for w in ctx.wakes
                  if w.kind == "account" and getattr(w.data, "kind", None) is EventKind.CLOSED}
        entries = [o for o in st.orders if not o.reduce]
        for o in entries:
            expired = ctx.now - o.created_at >= self.ttl
            orphan = o.symbol in closed and o.symbol not in open_symbols
            if expired or orphan:
                why = "ömrü doldu" if expired else "pozisyon kapandı"
                add(CancelOrder(Ref(o.number, o.symbol, o.side)), f"#{o.number} {o.symbol} {why}, iptal")
        cancelled = {c.ref.symbol for c in d.commands if isinstance(c, CancelOrder)}
        busy = open_symbols | {o.symbol for o in entries if o.symbol not in cancelled}
        used = sum(p.margin for p in st.positions) + sum(o.margin for o in entries if o.symbol not in cancelled)
        signals = []
        for w in ctx.wakes:
            b = getattr(w.data, "data", None)
            if w.kind != "trigger" or not isinstance(b, Burst):
                continue
            note = {"symbol": b.symbol, "up": b.up, "move_5s": round(b.move_5s * 100, 2),
                    "move_60s": round(b.move_60s * 100, 2), "rate_mult": round(min(b.rate_mult, 1e6), 1),
                    "side_share": round(b.side_share * 100, 1), "mean_gap": round(b.mean_gap * 100, 2)}
            if b.symbol not in ctx.symbols:
                note["result"] = "parite evrende yok"
            elif b.symbol in busy:
                note["result"] = "paritede pozisyon ya da emir var"
            elif len(busy) >= self.max_positions:
                note["result"] = "pozisyon sınırı dolu"
            else:
                cmds, note["result"], margin = self._open(b, ctx, used)
                for i, cmd in enumerate(cmds):
                    label = (f"{cmd.symbol} kaldıraç {cmd.leverage}x" if isinstance(cmd, SetLeverage)
                             else f"{cmd.symbol} {cmd.side} limit {cmd.price:g}")
                    add(cmd, label, chained=i > 0)
                if cmds:
                    busy.add(b.symbol)
                    used += margin
            signals.append(note)
        # Süre sınırına ya da emrin ömrüne en yakın an için uyanılır.
        ends = [p.entry_time + self.hold for p in st.positions if p.symbol not in
                {c.symbol for c in d.commands if isinstance(c, ClosePosition)}]
        ends += [o.created_at + self.ttl for o in entries if o.symbol not in cancelled]
        ends += [ctx.now + self.ttl for c in d.commands if isinstance(c, PlaceOrder)]
        d.wait_s = max(1, min(ends) - ctx.now) if ends else None
        if signals:
            d.record = {"signals": signals}
        return d


STRATEGIES.register(
    "spike_fade", "İğne dönüşü",
    "Tekil işlem akışında sakin bir paritede başlayan tek yönlü iğnenin tersine, ucun ötesine bekleyen limit "
    "emirlerle girer ve iğnenin ilk geri dönüşünde çıkar. Profilde işlem patlaması tetikleyicisi olmalıdır.",
    (Param("size_pct", "float", "Pozisyonun özkaynağa oranı, yüzde", default=1, low=0.1, high=100),
     Param("max_risk_pct", "float", "SL'de en büyük kayıp, özkaynağın yüzdesi", default=3, low=0.1, high=50),
     Param("max_positions", "int", "Aynı anda en fazla pozisyon", default=4, low=1, high=20),
     Param("max_margin_pct", "float", "Toplam marjin sınırı, özkaynağın yüzdesi", default=15, low=1, high=100),
     Param("entry_pct", "float", "İlk limitin son fiyattan uzaklığı, yüzde", default=0.5, low=0, high=20),
     Param("add_pct", "float", "İkinci limitin son fiyattan uzaklığı, yüzde", default=1.5, low=0, high=30),
     Param("order_ttl_s", "int", "Dolmayan giriş emrinin ömrü, saniye", default=15, low=1, high=3600),
     Param("retrace", "float", "TP için sıçramanın geri alınan oranı", default=0.5, low=0.05, high=1),
     Param("min_tp_pct", "float", "TP'nin girişten en küçük uzaklığı, yüzde", default=1.2, low=0.1, high=30),
     Param("sl_pct", "float", "SL'nin en uzak girişten uzaklığı, yüzde", default=3, low=0.1, high=50),
     Param("hold_s", "int", "En uzun tutma süresi, saniye", default=120, low=5, high=86400),
     Param("max_leverage", "int", "En yüksek kaldıraç", default=20, low=1, high=125)),
    lambda deps, **p: SpikeFade(**p, catalog=deps.catalog),
    SpikeFade,
)
