"""
file: commands.py
importable: Command, KINDS, SOURCES, RESTRICTIONS, execute, describe, restriction_text, check_restrictions

Hesabı değiştiren her istek bir Command olur ve execute() ile hesaba
uygulanır. İsteği kimin yaptığı Command'in source alanında taşınır: opus,
system ya da admin. execute() sonucu Opus'un da göreceği Türkçe bir cümleyle
döndürür, istek reddedilirse AccountError fırlatır. describe() bir komutu
henüz uygulanmamış haliyle Türkçe anlatır, yöneticinin teklifleri Opus'a
böyle gider. check_restrictions() yöneticinin kesin kısıtlarını bir komuta
uygular.

description: Hesabı değiştiren bütün isteklerin geçtiği tek kapı.
"""

# NOTE: Kapıdan yalnızca istekler geçer. TP, SL, iz süren stop, likidasyon,
# emir tetiklenmesi ve fonlama motorun kendi kuralıdır, bunlar doğrudan
# Account.check_price ve Account.apply_funding içinde işler.
#
# NOTE: Komutların parametreleri İngilizcedir. Opus'un Türkçe eylemleri
# opus/actions.py içinde komuta çevrilir, admin paneli komutları doğrudan
# kurar. Kapı hangi dilden geldiğini bilmez.
#
# NOTE: Başarılı her komut Account.log_event ile trade_events tablosuna
# kaynağıyla yazılır. Reddedilen istekler hesabı değiştirmediği için yazılmaz,
# Opus'un reddedilen eylemleri günlükteki action_results alanında durur.
#
# NOTE: Kesin kısıtlar yalnızca Opus'un isteklerine uygulanır. Yöneticinin
# kendi işlemleri ve dönem sonu gibi sistem işleri kısıta takılmaz. Kısıt
# konmadan önce verilmiş bir bekleyen emir de tetiklenince kısıta bakılmaz,
# çünkü tetiklenme kapıdan değil motordan geçer.

from dataclasses import dataclass, field

from .account import Account, AccountError

KINDS = ("open", "place_order", "cancel_order", "add", "close", "protect", "set_alert", "cancel_alert", "close_all")
SOURCES = ("opus", "system", "admin")

# Kısıt türleri ve bir komutun hangi türlere takılabileceği. Yalnızca hesabın
# riskini artıran komutlar kısıta bakar, kapatma ve iptal hiçbir zaman engellenmez.
RESTRICTIONS = ("no_open", "max_leverage", "max_margin", "side_only", "symbol_ban")
_RISKY = ("open", "place_order", "add")

# Kapatmanın veritabanındaki nedeni kaynağa göre seçilir, arayüz ve Opus'un
# kapanmış işlem araçları bunu ayrı gösterir.
_CLOSE_REASON = {"opus": "agent", "admin": "admin", "system": "period_end"}
_CANCEL_NOTE = {"opus": "Opus iptal etti", "admin": "Yönetici iptal etti", "system": "Dönem bitti"}


@dataclass
class Command:
    """Hesaba uygulanacak tek bir istek.

    Args:
        kind: KINDS içinden komutun türü.
        params: Komutun İngilizce parametreleri.
        source: İsteği yapan, SOURCES içinden.
    """
    kind: str
    params: dict = field(default_factory=dict)
    source: str = "opus"

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise AccountError(f"Bilinmeyen komut: {self.kind}")
        if self.source not in SOURCES:
            raise AccountError(f"Bilinmeyen kaynak: {self.source}")


def _protection(c: dict) -> dict:
    return {"tp": c.get("tp"), "tp_levels": c.get("tp_levels"), "sl": c.get("sl"), "trail_pct": c.get("trail_pct")}


def _g(x) -> str:
    return format(x, "g")


# Likidasyon fiyatı istemin hesap bölümündeki kuralla, paritenin büyüklüğüne
# göre basamakla yazılır. Kural opus/agent.py _f ile aynıdır.
def _liq(x: float) -> str:
    return f"{x:.1f}" if x >= 1000 else f"{x:.2f}" if x >= 10 else f"{x:.4f}"


# --- Uygulama ---

# Mesajlar Opus'a bir sonraki çağrıda aynen gösterilir. Opus'un kendi
# eylemlerinin mesajları kapı kurulmadan önceki haliyle aynıdır.
def execute(acc: Account, prices: dict[str, float], cmd: Command, now: int,
            restrictions: list[dict] | None = None) -> str:
    """Komutu hesaba uygular ve sonucu Türkçe bir cümleyle döndürür.

    Args:
        acc: Komutun uygulanacağı hesap.
        prices: Paritelerin güncel fiyatları.
        cmd: Uygulanacak komut.
        now: Unix saniye.
        restrictions: Yürürlükteki kesin kısıtlar. Yalnızca Opus'un komutlarına uygulanır.

    Raises:
        AccountError: Hesap kuralları ya da bir kesin kısıt isteği reddederse.
        KeyError: Komutun zorunlu bir parametresi eksikse.
    """
    if cmd.source == "opus" and restrictions:
        check_restrictions(acc, cmd, restrictions)
    if cmd.kind == "close_all":
        return _close_all(acc, prices, cmd, now)
    message, log = _run(acc, prices, cmd, now)
    acc.log_event(now, cmd.source, cmd.kind, message, **log)
    return message


# close_all, yöneticinin hepsini kapat isteğidir. Pozisyonlar kapanır,
# bekleyen emirler iptal olur, alarmlara dokunulmaz. Her kapatma ve iptal
# kapıdan ayrı bir komut olarak geçer, böylece her biri ayrı bir olay olur.
def _close_all(acc: Account, prices: dict[str, float], cmd: Command, now: int) -> str:
    out = [execute(acc, prices, Command("close", {"position_id": p["id"]}, cmd.source), now)
           for p in acc.positions()]
    orders = acc.pending_orders()
    for o in orders:
        execute(acc, prices, Command("cancel_order", {"order_id": o["id"]}, cmd.source), now)
    if orders:
        out.append(f"{len(orders)} bekleyen emir iptal edildi.")
    return " ".join(out) or "Açık pozisyon ve bekleyen emir yoktu."


# Komutu uygular, mesajı ve olay kaydının alanlarını döndürür. price o anki
# piyasa fiyatı ya da gerçekleşen fiyattır, detail ise değişen kaydın kendisi.
def _run(acc: Account, prices: dict[str, float], cmd: Command, now: int) -> tuple[str, dict]:
    c, kind = cmd.params, cmd.kind
    if kind == "open":
        s, side = c["symbol"], c["side"]
        p = acc.open_position(s, side, c["leverage"], c["margin"], prices[s], now, **_protection(c))
        return (f"#{p['id']} {s} {side} {p['leverage']:g}x açıldı, giriş {p['entry_price']:g}, miktar "
                f"{p['qty']:g}, marjin {p['margin']:.2f}, likidasyon {_liq(p['liq_price'])}.",
                {"symbol": s, "position_id": p["id"], "price": p["entry_price"], "detail": p})
    if kind == "place_order":
        s = c["symbol"]
        o = acc.place_order(s, c["side"], c["trigger_price"], c["leverage"], c["margin"], prices[s], now,
                            valid_min=c.get("valid_min"), **_protection(c))
        return (f"#{o['id']} {s} {o['side']} {o['leverage']}x emri {o['trigger_price']:g} tetiğiyle kaydedildi, "
                f"{'maker' if o['maker'] else 'taker'}.",
                {"symbol": s, "order_id": o["id"], "price": prices[s], "detail": o})
    if kind == "cancel_order":
        acc.cancel_order(c["order_id"], now, _CANCEL_NOTE[cmd.source])
        return f"#{c['order_id']} emri iptal edildi.", {"order_id": c["order_id"]}
    if kind == "add":
        s = acc._position(c["position_id"])["symbol"]
        p = acc.add_to_position(c["position_id"], c["leverage"], c["margin"], prices[s])
        return (f"#{p['id']} {s} pozisyonuna ekleme yapıldı. Yeni ortalama giriş {p['entry_price']:g}, miktar "
                f"{p['qty']:g}, marjin {p['margin']:.2f}, kaldıraç {p['leverage']:g}x, likidasyon "
                f"{_liq(p['liq_price'])}.",
                {"symbol": s, "position_id": p["id"], "price": prices[s], "detail": p})
    if kind == "close":
        id_ = c["position_id"]
        s = acc._position(id_)["symbol"]
        t = acc.close_position(id_, prices[s], now, _CLOSE_REASON[cmd.source], c.get("pct", 100) / 100)
        part = "tamamı" if t["id"] == id_ else f"{t['qty']:g} birimlik kısmı"
        return (f"#{id_} {s} pozisyonunun {part} {t['exit_price']:g} fiyatından kapatıldı, "
                f"net {t['pnl'] - t['fee']:+.2f} dolar.",
                {"symbol": s, "position_id": id_, "price": t["exit_price"], "detail": t})
    if kind == "protect":
        s = acc._position(c["position_id"])["symbol"]
        p = acc.update_protection(c["position_id"], prices[s], **_protection(c))
        levels = ", ".join(f"{k['price']:g}" for k in p["tp_levels"]) or "yok"
        return (f"#{p['id']} {s} pozisyonunda TP {levels}, SL {p['sl'] or 'yok'}, "
                f"iz süren stop {('yüzde ' + _g(p['trail_pct'])) if p['trail_pct'] else 'yok'} oldu.",
                {"symbol": s, "position_id": p["id"], "price": prices[s], "detail": p})
    if kind == "set_alert":
        s = c["symbol"]
        al = acc.set_alert(s, c["price"], c.get("note", ""), prices[s], now)
        return f"#{al['id']} {s} {al['price']:g} alarmı kuruldu.", {"symbol": s, "alert_id": al["id"],
                                                                    "price": prices[s], "detail": al}
    acc.cancel_alert(c["alert_id"], now)
    return f"#{c['alert_id']} alarmı iptal edildi.", {"alert_id": c["alert_id"]}


# --- Anlatım ---

def _protection_text(c: dict) -> str:
    parts = []
    if c.get("tp_levels"):
        parts.append("TP kademeleri " + ", ".join(f"{_g(k['price'])} yüzde {_g(k['pct'])}" for k in c["tp_levels"]))
    elif c.get("tp") is not None:
        parts.append(f"TP {_g(c['tp'])}" if c["tp"] else "TP kaldırılsın")
    if c.get("sl") is not None:
        parts.append(f"SL {_g(c['sl'])}" if c["sl"] else "SL kaldırılsın")
    if c.get("trail_pct") is not None:
        parts.append(f"iz süren stop yüzde {_g(c['trail_pct'])}" if c["trail_pct"] else "iz süren stop kaldırılsın")
    return ", ".join(parts)


# Yöneticinin teklifleri Opus'a uygulanmadan, bu cümleyle gider. Opus teklifi
# kendi eylemleriyle uygulayacağı için anlatım eylem alanlarının adlarını
# değil işin kendisini söyler.
def describe(cmd: Command) -> str:
    """Komutu uygulanmamış haliyle tek bir Türkçe cümleyle anlatır."""
    c, kind = cmd.params, cmd.kind
    extra = _protection_text(c)
    extra = f", {extra}" if extra else ""
    if kind == "open":
        return f"{c['symbol']} {c['side']} {_g(c['leverage'])}x {_g(c['margin'])} dolar marjinle pozisyon aç{extra}."
    if kind == "place_order":
        valid = f", geçerlilik {c['valid_min']} dakika" if c.get("valid_min") else ""
        return (f"{c['symbol']} {c['side']} {_g(c['trigger_price'])} tetiğiyle {_g(c['leverage'])}x "
                f"{_g(c['margin'])} dolar marjinli emir ver{extra}{valid}.")
    if kind == "cancel_order":
        return f"#{c['order_id']} emrini iptal et."
    if kind == "add":
        return f"#{c['position_id']} pozisyonuna {_g(c['leverage'])}x {_g(c['margin'])} dolar marjinle ekleme yap."
    if kind == "close":
        pct = c.get("pct", 100)
        return f"#{c['position_id']} pozisyonunun {'tamamını' if pct >= 100 else f'yüzde {_g(pct)} kadarını'} kapat."
    if kind == "protect":
        return f"#{c['position_id']} pozisyonunda {_protection_text(c) or 'korumayı değiştir'}."
    if kind == "set_alert":
        note = f", not: {c['note']}" if c.get("note") else ""
        return f"{c['symbol']} {_g(c['price'])} seviyesine alarm kur{note}."
    if kind == "cancel_alert":
        return f"#{c['alert_id']} alarmını iptal et."
    return "Bütün pozisyonları kapat ve bütün bekleyen emirleri iptal et."


# --- Kısıtlar ---

def restriction_text(r: dict) -> str:
    """Bir kısıtı Türkçe tek bir cümle parçasıyla anlatır.

    Args:
        r: kind ve params anahtarlı kısıt kaydı.
    """
    k, c = r["kind"], r["params"]
    where = f"{c['symbol']} paritesinde " if c.get("symbol") else ""
    if k == "no_open":
        return f"{where}yeni pozisyon açılmaz, emir verilmez ve ekleme yapılmaz"
    if k == "max_leverage":
        return f"{where}kaldıraç en fazla {_g(c['value'])}x"
    if k == "max_margin":
        if c.get("scope") == "total":
            return f"açık pozisyonların ve bekleyen emirlerin toplam marjini en fazla {_g(c['value'])} dolar"
        return f"bir pozisyonun marjini en fazla {_g(c['value'])} dolar"
    if k == "side_only":
        return f"{c['symbol']} paritesinde yalnızca {c['side']} yönünde işlem"
    if k == "symbol_ban":
        return f"{c['symbol']} paritesinde işlem yapılmaz"
    raise AccountError(f"Bilinmeyen kısıt: {k}")


def _symbol_side(acc: Account, cmd: Command) -> tuple[str, str, dict | None]:
    if cmd.kind == "add":
        p = acc._position(cmd.params["position_id"])
        return p["symbol"], p["side"], p
    return cmd.params["symbol"], cmd.params["side"], None


def _breaks(acc: Account, cmd: Command, r: dict) -> bool:
    k, c = r["kind"], r["params"]
    symbol, side, pos = _symbol_side(acc, cmd)
    if c.get("symbol") and c["symbol"] != symbol:
        return False
    margin = cmd.params["margin"]
    if k in ("no_open", "symbol_ban"):
        return True
    if k == "max_leverage":
        return cmd.params["leverage"] > c["value"]
    if k == "side_only":
        return side != c["side"]
    if c.get("scope") == "total":
        used = sum(p["margin"] for p in acc.positions()) + sum(o["margin"] for o in acc.pending_orders())
        return used + margin > c["value"] + 1e-9
    return (pos["margin"] if pos else 0) + margin > c["value"] + 1e-9


def check_restrictions(acc: Account, cmd: Command, restrictions: list[dict]) -> None:
    """Komut bir kesin kısıtı aşıyorsa AccountError fırlatır.

    Args:
        acc: Komutun uygulanacağı hesap.
        cmd: Denetlenen komut. Riski artırmayan komutlar hiç denetlenmez.
        restrictions: id, kind ve params anahtarlı yürürlükteki kesin kısıtlar.
    """
    if cmd.kind not in _RISKY:
        return
    for r in restrictions:
        if _breaks(acc, cmd, r):
            raise AccountError(f"yöneticinin #{r['id']} numaralı kesin kısıtı geçerli: {restriction_text(r)}.")
