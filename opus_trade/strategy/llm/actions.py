"""
file: actions.py
importable: ACTIONS, ORDER_TYPES, Translation, schema, translate

Opus'un karar şemasındaki eylemler. Her eylem tek bir kayıttır: Türkçe tipi,
Opus'a görünen açıklaması, zorunlu ve isteğe bağlı alanları. schema() eylem
listesinin bir elemanının JSON şemasını profilin paritelerine göre kurar.
translate() Opus'un bir eylemini broker komutlarına ya da bir alarm
işlemine çevirir, eylem kendi içinde çelişkiliyse ya da hesaba uymuyorsa
ValueError fırlatır.

description: Opus'un eylemlerinin şeması ve komuta çevirisi.
"""

# NOTE: Komutlar Binance gibi miktar taşır, Opus ise marjinle ve yüzdeyle
# düşünür. Çeviri bu katmandadır: marjin paritenin kaldıracıyla miktara, TP
# payı girişin ya da pozisyonun miktarına çevrilir. Pozisyon parite ve yönle
# bulunur, numarası yoktur. Numara yalnızca emirdedir ve numarayla bir emre
# dokunan eylem sembol ve yönü de taşır, kapı ikisini karşılaştırır.
#
# NOTE: Marjin kullanılabilir bakiyeyi yüzde 2'ye kadar aşarsa miktar bakiyeye
# sığacak kadar küçültülür ve sonuca not düşülür. Komisyon payı gibi kıl payı
# farklar boşuna bir düzeltme çağrısına mal olmasın, büyük bir hata da
# sessizce küçük bir işleme dönüşmesin diye, Burak'ın 21.11 kararı. Maliyet
# temkinli hesaplanır: marjin, taker komisyonu ve limitte açık zarar.
#
# NOTE: Koruma değiştirmede önce yeni emir kurulur, sonra eskisi iptal edilir.
# Arada pozisyon korumasız kalmaz. Bir eylemin komutları zincirdir, strateji
# onları Decision.chained ile işaretler: yeni emir reddedilirse iptal hiç
# gönderilmez, eski koruma yerinde kalır. Kaldıraç ayarı reddedilirse giriş de
# gitmez. Kâğıt broker kalanı aşan korumayı kalanı kapatan koruma sayar, iki
# emrin kısa süre birlikte durması zararsız.
#
# XXX: Kullanılabilir bakiye kararın başındaki haldir. Aynı kararda birden
# çok giriş verilirse her biri tek başına sığdırılır, toplamı sığmazsa
# sonrakini broker reddeder ve düzeltme çağrısı gelir.
#
# XXX: Alan adları, eylem tipleri ve emir türlerinin adları Opus'a görünür,
# Türkçe kalır.

from dataclasses import dataclass

from ...domain import numbers, rules
from ...domain.catalog import Catalog
from ...domain.errors import RuleError
from ...domain.fees import FeeSchedule
from ...domain.models import Side, SymbolSpec
from ...domain.trading import (AccountState, AmendOrder, CancelOrder, ClosePosition, Command, OrderType, PlaceOrder,
                               Position, Protection, Ref, SetLeverage)

# Opus'a görünen emir türü ve Binance'teki karşılığı.
ORDER_TYPES = {"limit": OrderType.LIMIT, "kirilim": OrderType.STOP, "geri_cekilme": OrderType.TAKE_PROFIT}

# Marjinin kullanılabilir bakiyeyi aşıp yine de sığdırılabileceği pay.
_FIT_SLACK = 0.02

_LEVEL = {"type": "object", "additionalProperties": False, "required": ["fiyat", "yuzde"],
          "properties": {"fiyat": {"type": "number"}, "yuzde": {"type": "number"}}}

_FIELDS = {
    "yon": {"type": "string", "enum": ["long", "short"]},
    "numara": {"type": "integer"},
    "tur": {"type": "string", "enum": list(ORDER_TYPES)},
    "fiyat": {"type": "number"},
    "kaldirac": {"type": "integer"},
    "marjin_usd": {"type": "number"},
    "yuzde": {"type": "number"},
    "tp": {"type": "number"},
    "tp_kademeleri": {"type": "array", "items": _LEVEL},
    "sl": {"type": "number"},
    "iz_yuzde": {"type": "number"},
    "gecerlilik_dk": {"type": "integer"},
    "not": {"type": "string"},
    "alarm_no": {"type": "integer"},
}

_GUARDS = ["tp", "tp_kademeleri", "sl", "iz_yuzde"]
_REF = ["numara", "sembol", "yon"]

_ENTRY_NOTE = ("marjin_usd pozisyona ayıracağın marjindir, miktar ona ve paritenin kaldıracına göre hesaplanır. "
               "kaldirac verirsen ve paritenin ayarlı kaldıracından farklıysa önce parite o kaldıraca ayarlanır. "
               "Aynı yönde açık pozisyon varsa giriş onunla birleşir. tp ya da tp_kademeleri, sl ve iz_yuzde bu "
               "girişin miktarına kurulan koruma emirleridir. tp_kademeleri en fazla dört kademedir, her kademenin "
               "yuzde alanı girişin yüzde kaçını kapatacağıdır.")

# tip, açıklama, zorunlu alanlar, isteğe bağlı alanlar.
ACTIONS = [
    ("kaldirac_ayarla", "Paritenin kaldıracını ayarlar. Kaldıraç long ve short için ortaktır ve sonraki girişlere "
     "uygulanır. Paritede açık pozisyon varken düşürülemez.", ["sembol", "kaldirac"], []),
    ("piyasa_giris", "Piyasa fiyatından giriş yapar. " + _ENTRY_NOTE,
     ["sembol", "yon", "marjin_usd"], ["kaldirac"] + _GUARDS),
    ("emir_ver", "Bekleyen giriş emri verir. tur limit ise fiyat emrin fiyatıdır: long için güncel fiyatın altında, "
     "short için üstünde defterde bekler ve tam fiyatından maker dolar, yanlış taraftaysa hemen taker dolar. tur "
     "kirilim Binance'in STOP_MARKET emridir: fiyat long için güncelin üstünde, short için altında bir tetiktir, son "
     "fiyat oraya değince piyasa emri olur. tur geri_cekilme Binance'in TAKE_PROFIT_MARKET emridir: fiyat long için "
     "güncelin altında, short için üstünde bir tetiktir, son fiyat oraya gelince piyasa emri olur. gecerlilik_dk "
     "verirsen emir o kadar dakika içinde dolmazsa iptal olur. " + _ENTRY_NOTE,
     ["sembol", "yon", "tur", "fiyat", "marjin_usd"], ["kaldirac"] + _GUARDS + ["gecerlilik_dk"]),
    ("emir_degistir", "Bekleyen bir limit emrin fiyatını değiştirir, miktarı aynı kalır. numara, sembol ve yon emrin "
     "hesaptaki değerleridir.", _REF + ["fiyat"], []),
    ("emir_iptal", "Bekleyen bir giriş emrini ya da bir koruma emrini iptal eder. numara, sembol ve yon emrin "
     "hesaptaki değerleridir.", _REF, []),
    ("koruma_ekle", "Açık bir pozisyona koruma emirleri ekler: tp ya da tp_kademeleri, sl ve iz_yuzde. yuzde "
     "pozisyonun yüzde kaçını koruyacaklarıdır, vermezsen tamamı. Tamamını koruyan emir tetiklendiği an "
     "pozisyonda kalan miktarın tamamını kapatır. tp_kademeleri en fazla dört kademedir, kademelerin yuzde alanı "
     "korunan miktarın yüzde kaçını kapatacaklarıdır.", ["sembol", "yon"], _GUARDS + ["yuzde"]),
    ("koruma_degistir", "Bir koruma emrinin seviyesini değiştirir. TP ve SL için fiyat, iz için iz_yuzde verirsin, "
     "miktar aynı kalır. Önce yeni emir kurulur, sonra eskisi iptal edilir. numara, sembol ve yon emrin hesaptaki "
     "değerleridir.", _REF, ["fiyat", "iz_yuzde"]),
    ("pozisyon_kapat", "Bir pozisyonu piyasa fiyatından kapatır. yuzde verirsen o kadarını kapatır, vermezsen "
     "tamamını. Pozisyon tamamen kapanınca ona bağlı koruma emirleri iptal olur.", ["sembol", "yon"], ["yuzde"]),
    ("alarm_kur", "sembol, fiyat ve not ile bir fiyat alarmı kurar.", ["sembol", "fiyat", "not"], []),
    ("alarm_iptal", "alarm_no ile bir alarmı iptal eder.", ["alarm_no"], []),
]

_BY_TYPE = {a[0]: a for a in ACTIONS}


def schema(symbols: list[str]) -> dict:
    """Eylem listesinin bir elemanının JSON şemasını döndürür."""
    fields = dict(_FIELDS, sembol={"type": "string", "enum": list(symbols)})
    options = []
    for tip, text, required, optional in ACTIONS:
        props = {"tip": {"type": "string", "const": tip}}
        props.update({f: fields[f] for f in required + optional})
        options.append({"type": "object", "additionalProperties": False, "description": text,
                        "required": ["tip"] + required, "properties": props})
    return {"anyOf": options}


# kind command ise item komut listesi, alarm ise op anahtarı add ya da
# cancel olan bir sözlüktür. note sonuca eklenecek bir açıklamadır, örneğin
# miktarın bakiyeye sığdırıldığı.
@dataclass(frozen=True)
class Translation:
    kind: str
    item: list[Command] | dict
    note: str | None = None


# --- Koruma ---

# Korumalar base miktarına kurulur. base None ise miktarsızdır: girişte
# girişin dolan miktarının, açık pozisyonda tetik anında kalanın tamamı.
# Kademe yüzdeleri whole'un payıdır ve miktarı hep yazılır. Toplam yüzse son
# kademe öncekilerden artanı alır, yuvarlamadan korumasız kırıntı kalmaz.
# rest_open True ise, yani açık pozisyonun tamamı korunuyorsa, son kademe
# miktarsız kalır ve sonradan eklenen miktarı da kapatır. Tek TP yüzde
# yüzlük tek kademedir, ikisi birlikte verilirse hangisinin istendiği
# bilinemez.
def _guards(a: dict, spec: SymbolSpec, base: float | None, whole: float,
            rest_open: bool = False) -> tuple[Protection, ...]:
    tp, levels = a.get("tp"), a.get("tp_kademeleri")
    if tp and levels:
        raise ValueError("tp ve tp_kademeleri birlikte verilemez, birini seç.")
    out = []
    if tp:
        out.append(Protection(OrderType.TAKE_PROFIT, float(tp), qty=base))
    if levels:
        total = sum(float(k["yuzde"]) for k in levels)
        if total > 100 + rules.TOLERANCE:
            raise ValueError(f"TP kademelerinin yüzdeleri toplamı {numbers.num(total)}, en fazla 100 olabilir.")
        used = 0.0
        for i, k in enumerate(levels):
            last = i == len(levels) - 1 and total >= 100 - rules.TOLERANCE
            if last and rest_open:
                out.append(Protection(OrderType.TAKE_PROFIT, float(k["fiyat"])))
                continue
            q = round(whole - used, 10) if last else rules.round_qty(spec, whole * float(k["yuzde"]) / 100)
            used += q
            if q <= 0:
                raise ValueError(f"{numbers.price(float(k['fiyat']))} kademesinin miktarı bir miktar adımından, "
                                 f"{numbers.qty(spec.qty_step, spec.symbol)}, küçük kalıyor. Kademe sayısını azalt.")
            out.append(Protection(OrderType.TAKE_PROFIT, float(k["fiyat"]), qty=q))
    if a.get("sl"):
        out.append(Protection(OrderType.STOP, float(a["sl"]), qty=base))
    if a.get("iz_yuzde"):
        out.append(Protection(OrderType.TRAILING_STOP, trail_pct=float(a["iz_yuzde"]), qty=base))
    return tuple(out)


def _position(a: dict, state: AccountState) -> Position:
    p = state.position(a["sembol"], Side(a["yon"]))
    if p is None:
        raise ValueError(f"Açık {a['sembol']} {a['yon']} pozisyonu yok.")
    return p


def _add_guards(a: dict, state: AccountState, catalog: Catalog) -> list[Command]:
    p, pct = _position(a, state), float(a.get("yuzde") or 100)
    spec = catalog.get(p.symbol)
    base = None if pct >= 100 else rules.round_qty(spec, p.qty * pct / 100)
    if base is not None and base <= 0:
        raise ValueError(f"Pozisyonun yüzde {numbers.num(pct)}'i bir miktar adımından küçük kalıyor.")
    guards = _guards(a, spec, base, p.qty if base is None else base, rest_open=base is None)
    if not guards:
        raise ValueError("Eklenecek koruma yok, tp, tp_kademeleri, sl ya da iz_yuzde ver.")
    return [PlaceOrder(p.symbol, p.side, g.type, qty=g.qty, price=g.price, reduce=True, trail_pct=g.trail_pct)
            for g in guards]


def _change_guard(a: dict, state: AccountState) -> list[Command]:
    ref = Ref(int(a["numara"]), a["sembol"], Side(a["yon"]))
    o = state.order(ref.number)
    if o is None or o.symbol != ref.symbol or o.side is not ref.side:
        raise ValueError(f"#{ref.number} numaralı {ref.symbol} {ref.side} emri yok.")
    if not o.reduce:
        raise ValueError(f"#{ref.number} bir giriş emri. Limit girişin fiyatı emir_degistir ile değişir, diğer "
                         f"girişler iptal edilip yeniden verilir.")
    if o.type is OrderType.TRAILING_STOP:
        if not a.get("iz_yuzde"):
            raise ValueError(f"#{ref.number} bir iz süren stop, yeni değeri iz_yuzde ile ver.")
        new = PlaceOrder(o.symbol, o.side, o.type, qty=o.qty, reduce=True, trail_pct=float(a["iz_yuzde"]))
    else:
        if not a.get("fiyat"):
            raise ValueError(f"#{ref.number} için yeni seviyeyi fiyat ile ver.")
        new = PlaceOrder(o.symbol, o.side, o.type, qty=o.qty, price=float(a["fiyat"]), reduce=True)
    return [new, CancelOrder(ref)]


# --- Giriş ---

# Paritenin kaldıracı girişten önce ayarlanır. Düşürme açık pozisyonda
# reddedileceği için burada önceden sorulur, yoksa kaldıraç değişmeden giriş
# yine giderdi ve Opus'un istediğinden farklı bir marjinle açılırdı.
def _leverage(a: dict, state: AccountState, spec: SymbolSpec) -> tuple[int, list[Command]]:
    now = state.leverage(spec.symbol)
    want = a.get("kaldirac")
    if want is None or int(want) == now:
        return now, []
    open_ = max((p.qty * p.entry_price for p in state.positions if p.symbol == spec.symbol), default=0.0)
    try:
        rules.check_leverage_change(spec, now, want, open_)
    except RuleError as e:
        raise ValueError(str(e)) from e
    return int(want), [SetLeverage(spec.symbol, int(want))]


# Bir birim miktarın temkinli maliyeti: marjin, taker komisyonu ve limitte
# mark fiyatının aleyhindeki açık zarar.
def _unit_cost(at: float, lev: int, fees: FeeSchedule, buy: bool, mark: float, limit: bool) -> float:
    loss = rules.open_loss(buy, 1.0, at, mark) if limit else 0.0
    return at / lev + at * fees.taker + loss


def _fit(qty: float, unit: float, available: float, spec: SymbolSpec) -> tuple[float, str | None]:
    cost = qty * unit
    if cost <= available + rules.TOLERANCE:
        return qty, None
    if cost > available * (1 + _FIT_SLACK):
        raise ValueError(f"Kullanılabilir bakiye {numbers.num(available, 2)} dolar, bu giriş komisyonuyla "
                         f"{numbers.num(cost, 2)} dolar istiyor.")
    fitted = rules.round_qty(spec, available / unit)
    return fitted, (f"Marjin kullanılabilir bakiyeyi az farkla aştığı için miktar "
                    f"{numbers.qty(qty, spec.symbol)} yerine {numbers.qty(fitted, spec.symbol)} oldu.")


def _entry(a: dict, state: AccountState, prices: dict[str, float], marks: dict[str, float], catalog: Catalog,
           fees: FeeSchedule, type_: OrderType) -> Translation:
    sym, side = a["sembol"], Side(a["yon"])
    if sym not in prices:
        raise ValueError(f"{sym} fiyatı henüz gelmedi.")
    spec, last = catalog.get(sym), prices[sym]
    buy = rules.is_buy(side, False)
    lev, cmds = _leverage(a, state, spec)
    price = None if type_ is OrderType.MARKET else float(a["fiyat"])
    at = rules.slipped(spec, last, buy) if price is None else price
    qty = rules.qty_for_margin(spec, lev, float(a["marjin_usd"]), at)
    unit = _unit_cost(at, lev, fees, buy, marks.get(sym, last), type_ is OrderType.LIMIT)
    qty, note = _fit(qty, unit, state.available, spec)
    if qty <= 0:
        raise ValueError(f"Marjin bir miktar adımına, {numbers.qty(spec.qty_step, sym)}, yetmiyor.")
    cmds.append(PlaceOrder(sym, side, type_, qty=qty, price=price, protection=_guards(a, spec, None, qty),
                           valid_min=a.get("gecerlilik_dk")))
    return Translation("command", cmds, note)


def translate(a: dict, state: AccountState, prices: dict[str, float], marks: dict[str, float], catalog: Catalog,
              fees: FeeSchedule) -> Translation:
    """Opus'un bir eylemini komutlara ya da bir alarm işlemine çevirir.

    Args:
        a: Opus'un eylemi.
        state: Hesabın hali. Pozisyon, emir, kaldıraç ve kullanılabilir bakiye buradan okunur.
        prices: Paritelerin son fiyatları, piyasa girişinin miktarı bununla hesaplanır.
        marks: Paritelerin mark fiyatları, limit girişin açık zararı için.
        catalog: Parite kuralları.
        fees: Komisyon oranları, girişin maliyeti için.

    Raises:
        ValueError: Eylem tipi bilinmiyorsa, zorunlu bir alan eksikse, alanlar çelişiyorsa, dokunduğu pozisyon ya
            da emir yoksa ya da giriş kullanılabilir bakiyeye sığmıyorsa.
    """
    tip = a.get("tip")
    if tip not in _BY_TYPE:
        raise ValueError(f"Bilinmeyen eylem tipi: {tip}.")
    missing = [f for f in _BY_TYPE[tip][2] if a.get(f) is None]
    if missing:
        raise ValueError(f"{', '.join(missing)} alanı eksik.")
    match tip:
        case "kaldirac_ayarla":
            _, cmds = _leverage(a, state, catalog.get(a["sembol"]))
            if not cmds:
                raise ValueError(f"{a['sembol']} kaldıracı zaten {a['kaldirac']}x.")
            return Translation("command", cmds)
        case "piyasa_giris":
            return _entry(a, state, prices, marks, catalog, fees, OrderType.MARKET)
        case "emir_ver":
            return _entry(a, state, prices, marks, catalog, fees, ORDER_TYPES[a["tur"]])
        case "emir_degistir":
            o = state.order(int(a["numara"]))
            if o is not None and o.reduce:
                raise ValueError(f"#{o.number} bir koruma emri, seviyesi koruma_degistir ile değişir.")
            return Translation("command", [AmendOrder(Ref(int(a["numara"]), a["sembol"], Side(a["yon"])),
                                                      float(a["fiyat"]))])
        case "emir_iptal":
            return Translation("command", [CancelOrder(Ref(int(a["numara"]), a["sembol"], Side(a["yon"])))])
        case "koruma_ekle":
            return Translation("command", _add_guards(a, state, catalog))
        case "koruma_degistir":
            return Translation("command", _change_guard(a, state))
        case "pozisyon_kapat":
            p, pct = _position(a, state), float(a.get("yuzde") or 100)
            qty = None if pct >= 100 else rules.round_qty(catalog.get(p.symbol), p.qty * pct / 100)
            if qty is not None and qty <= 0:
                raise ValueError(f"Pozisyonun yüzde {numbers.num(pct)}'i bir miktar adımından küçük kalıyor.")
            return Translation("command", [ClosePosition(p.symbol, p.side, qty)])
        case "alarm_kur":
            return Translation("alarm", {"op": "add", "symbol": a["sembol"], "price": a["fiyat"], "note": a["not"]})
    return Translation("alarm", {"op": "cancel", "number": a["alarm_no"]})
