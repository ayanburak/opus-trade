"""
file: fingerprint.py
importable: yok

Her profilin dönemini sıfırlatan parçaların özetini çıkarıp verilen kayıt
dosyasındaki özetle karşılaştırır. Proje klasöründe
uv run python -m opus_trade.fingerprint <kayıt dosyası> ile çalışır. Bir profilin
bir parçası değiştiyse hangi profilin hangi parçası olduğunu yazar ve sıfırdan
farklı bir kodla çıkar, deploy.sh bu durumda yayını durdurur. Değişiklik
bilerek yapıldıysa, yani o profillerin dönemi zaten sıfırlanacaksa ya da
değişikliğin dönemi etkilemediği elle doğrulandıysa, --save yeni hali kayıt
olarak yazar.

description: Bir profilin dönemini sıfırlatacak bir değişikliğin farkında olmadan yayına çıkmasını engelleyen kontrol.
"""

# NOTE: Parçalar 17.11 kararlarına göre seçildi. Profil dosyası,
# talimat, istemin biçimi, hesap kuralları ve model ayarı dönemi sıfırlatır,
# bunlar parmak izine girer. Yeni araç, yeni eylem ve araç düzeltmesi
# sıfırlatmaz, bu yüzden araç listesi, piyasa araçlarının kodu ve şemanın
# eylemler kısmı parmak izine girmez.
#
# NOTE: Talimat, şema, claude komutu ve istem gerçek çıktıdan özetlenir.
# İstem canlı piyasa verisi yerine sabit bir örnek durumla ve sabit bir anla
# kurulur, böylece özet her makinede ve her saatte aynı çıkar ve yalnızca
# istemin biçimi değişince farklılaşır. Parite kartlarını kuran gösterge,
# analiz ve likidasyon modülleri de bu çıktı üzerinden kapsanır. Hesap
# motorunun gövdesi çıktıdan özetlenemediği için kaynak dosyaları özetlenir.
# Kaynak önce Python'un sözdizimi ağacına çevrilir ve docstring'ler atılır,
# yorum ve docstring değişikliği kontrolü durdurmaz.
#
# NOTE: agent.py kaynağı özetlenmez, çünkü Opus'a giden metinlerin hepsi
# zaten çıktılardan özetlenir.
#
# XXX: Örnek durum istemin bütün dallarından geçecek şekilde kuruldu: açık
# pozisyon, emir, alarm, geçmiş karar, yönetici müdahalesi ve verisi olmayan bir parite. İsteme
# yeni bir dal eklenirse örnek durum da o dalı kapsayacak şekilde
# genişletilmelidir, yoksa o dal kontrol dışında kalır.

import ast
import dataclasses
import hashlib
import json
import os
import sys
import time

# Saat dilimi modüller yüklenmeden önce sabitlenir. Talimattaki tarihler yerel
# saatle yazıldığı için makinenin saat dilimi özeti değiştirmemeli.
os.environ["TZ"] = "Europe/Istanbul"
time.tzset()

from datetime import datetime, timezone
from pathlib import Path

from . import settings
from .market import indicators
from .opus import agent

# Dönemin örnek başlangıç ve bitişi ile istemin kurulduğu an. Sayıların
# kendisinin önemi yok, yalnızca her çalıştırmada aynı kalmaları gerekir.
START, END = 1794711823, 1795921423
NOW = 1794839400 - 3600

# Hesabı yürüten kurallar bu dosyalardadır: hesap, zamanlayıcı, kapı,
# müdahaleler, gözcüler ve tablolar. Gözcüler Opus'un ne zaman çağrılacağını
# belirlediği için motorun parçası sayılır.
_SOURCES = ("engine/account.py", "engine/scheduler.py", "engine/commands.py", "engine/interventions.py",
            "engine/watchers.py", "db.py")

# Hesap kurallarının ve modelin ayarları. Katalog satırları profil parçasına
# girer, Türkçe ay ve gün adları da talimat ve istem çıktısına.
_SETTINGS = ("FEE_TAKER", "FEE_MAKER", "END_EQUITY", "MODEL", "EFFORT", "TOOL_STEP_LIMIT", "HARD_STEP_LIMIT",
             "RESEARCHER_MODEL", "ERROR_RETRY_MIN", "CALL_TIMEOUT_S", "PAST_DECISIONS", "PROMPT_CANDLES", "TIMEZONE")


def _digest(value) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# Sözdizimi ağacında yorum zaten yoktur. Modülün, sınıfların ve
# fonksiyonların ilk satırındaki docstring ayrıca atılır. Satır numaraları
# ast.dump'a girmez, boş satır eklemek de özeti değiştirmez.
def _source(name: str) -> str:
    tree = ast.parse((settings.ROOT / "opus_trade" / name).read_text())
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (isinstance(body, list) and body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str)):
            node.body = body[1:] or [ast.Pass()]
    return _digest(ast.dump(tree))


# Komutta proje ve veri klasörünün yolları geçer. Yollar makineden makineye
# değiştiği için yer tutucuyla değiştirilir, sunucuda ve yerelde aynı özet
# çıkar. Talimat ve şema kendi parçalarında özetlendiği için komuttan çıkarılır.
def _command(p: settings.Profile) -> list[str]:
    out = []
    for part in agent._command(p, "TALIMAT"):
        out.append(part.replace(str(settings.DATA_DIR), "<DATA>").replace(str(settings.ROOT), "<ROOT>"))
    out[out.index("--json-schema") + 1] = "SEMA"
    return out


# Eylemlerin listesi ve açıklamaları atılır, yeni bir eylem dönemi sıfırlatmaz.
def _schema(p: settings.Profile) -> dict:
    s = agent.schema(p)
    s["properties"]["eylemler"] = {**s["properties"]["eylemler"], "items": "EYLEMLER"}
    return s


# Gerçeğe benzeyen ama tamamen sabit mumlar. Göstergelerin hesaplanabilmesi
# için iki yüz elliden fazla mum gerekir.
def _candles(base: float, step: int) -> list[dict]:
    out = []
    for i in range(260):
        c = base * (1 + 0.01 * ((i * 7) % 13 - 6) / 6 + 0.0005 * i)
        out.append({"t": NOW - (260 - i) * step, "o": c * 0.999, "h": c * 1.004, "l": c * 0.995, "c": c, "v": 100 + i})
    return out


def _market(p: settings.Profile) -> dict:
    summary, detail = {}, {}
    for k, s in enumerate(p.symbols):
        price = 1000.0 * (k + 1) + 0.5
        lists = {i: _candles(price, 900 * (n + 1)) for n, i in enumerate(("15m", "1h", "4h", "1d"))}
        # İlk parite bütün verilerle, ikincisi verisi alınamamış gibi kurulur.
        full = k == 0
        summary[s] = {
            "price": price, "change_pct": 1.25, "change_7d": -3.5, "volume_usd": 1.5e9, "funding": 0.0001,
            "next_funding": NOW + 3600, "trend_4h": "EMA20 üstünde, EMA50 altında", "trend_1d": "EMA50 üstünde, EMA200 üstünde",
            "volatility": 2.4,
            "open_interest": {"usd": 8e9, "change_pct": 2.1} if full else None,
            "positioning": {"accounts_long": 61.2, "top_long": 55.4, "taker_last": 1.05, "taker_24h": 0.98} if full else None,
            "liquidations": {"long": {"count": 12 if full else 0, "usd": 3.4e5 if full else 0.0},
                             "short": {"count": 4 if full else 0, "usd": 9e4 if full else 0.0}},
            "liquidation_wave": {"start": NOW - 7200, "long_usd": 2e5, "short_usd": 1e4, "low": price * 0.98,
                                 "high": price * 0.99} if full else None,
            "levels_above": [{"price": price * 1.01, "touches": 3}, {"price": price * 1.03, "touches": 2}],
            "levels_below": [{"price": price * 0.97, "touches": 2}, {"price": price * 0.99, "touches": 4}],
        }
        # Ham mumlar canlı çağrıdaki gibi settings.PROMPT_CANDLES'a göre seçilir.
        detail[s] = {"indicators": {i: indicators.summary(m) for i, m in lists.items()},
                     "candles": {i: lists[i][-n:] for i in lists if (n := settings.PROMPT_CANDLES.get(i))}}
    return {"summary": summary, "detail": detail}


def _state(p: settings.Profile) -> dict:
    first = next(iter(p.symbols))
    price = 1000.5
    return {
        "prices": {s: 1000.0 * (k + 1) + 0.5 for k, s in enumerate(p.symbols)},
        "cash": 61.234, "equity": 98.765, "return_pct": -1.235, "end_time": END,
        "positions": [{"id": 3, "symbol": first, "side": "long", "leverage": 10, "qty": 0.4, "margin": 40.0,
                       "entry_price": price * 0.99, "best_price": price * 1.002, "entry_time": NOW - 5400,
                       "tp_levels": [{"price": price * 1.02, "pct": 50, "qty": 0.2}, {"price": price * 1.04, "pct": 50, "qty": None}],
                       "sl": price * 0.97, "trail_pct": 1.5, "liq_price": price * 0.9, "upnl": 4.0, "funding": 0.0123}],
        "orders": [{"id": 5, "symbol": first, "side": "short", "trigger_price": price * 1.05, "leverage": 5, "margin": 10.0,
                    "maker": 1, "tp_levels": [{"price": price * 1.0, "pct": 100}], "sl": price * 1.08, "trail_pct": None,
                    "expires_at": NOW + 3600}],
        "alerts": [{"id": 2, "symbol": first, "price": price * 0.95, "note": "destek bölgesi"}],
        "stats": {"trade_count": 4, "wins": 2, "total_net": -1.5, "total_fee": 0.42},
        "notebook": "Örnek not defteri.",
    }


# build_prompt anı time.time() ve datetime.now() ile okur. İkisi de sabit
# ana çekilir, böylece istemdeki saat, kalan süre ve piyasa saatleri hep aynı çıkar.
class _FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime.fromtimestamp(NOW, tz or timezone.utc)


def _prompt(p: settings.Profile) -> str:
    real_time, real_dt = agent.time.time, agent.datetime
    agent.time.time, agent.datetime = (lambda: NOW), _FixedDatetime
    try:
        decisions = [{"time": NOW - 7200, "summary": "Örnek karar.", "results": ["#5 emri kaydedildi."]}]
        admin = {"items": ["17.11 21:30: Yönetici soruyor: Örnek soru? Cevabını yoneticiye_cevap alanına yaz."],
                 "restrictions": ["#4, kesin: kaldıraç en fazla 5x."]}
        return agent.build_prompt(p, "İstediğin bekleme süresi doldu.", _state(p), _market(p), decisions, admin)
    finally:
        agent.time.time, agent.datetime = real_time, real_dt


def parts(p: settings.Profile) -> dict[str, str]:
    """Bir profilin dönemini sıfırlatan parçalarının özetini döndürür."""
    out = {
        "profil": _digest(dataclasses.asdict(p)),
        "talimat": _digest(agent.system_prompt(p, START, END)),
        "sema": _digest(_schema(p)),
        "komut": _digest(_command(p)),
        "istem": _digest(_prompt(p)),
        "ayarlar": _digest({n: getattr(settings, n) for n in _SETTINGS}),
    }
    for name in _SOURCES:
        out[f"kod/{name}"] = _source(name)
    return out


# Kayıt dosyasının yeri yayın düzenine aittir, kod onu bilmez ve çağıran verir.
if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--save"]
    if len(args) != 1:
        sys.exit("Kullanım: python -m opus_trade.fingerprint <kayıt dosyası> [--save]")
    BASELINE = Path(args[0])
    now = {pid: parts(p) for pid, p in settings.PROFILES.items()}
    if "--save" in sys.argv:
        BASELINE.write_text(json.dumps(now, indent=2, ensure_ascii=False) + "\n")
        print(f"Profillerin parmak izi kaydedildi: {BASELINE}")
        sys.exit(0)
    if not BASELINE.exists():
        sys.exit(f"Kayıtlı parmak izi yok: {BASELINE}. İlk kayıt için --save ile çalıştırılır.")
    saved = json.loads(BASELINE.read_text())
    lines = []
    for pid in sorted(now.keys() | saved.keys()):
        if pid not in saved:
            lines.append(f"{pid}: yeni profil, kayıtta yok.")
        elif pid not in now:
            lines.append(f"{pid}: kayıtta var ama profil dosyası yok.")
        elif changed := sorted(k for k in now[pid].keys() | saved[pid].keys() if now[pid].get(k) != saved[pid].get(k)):
            lines.append(f"{pid}: " + ", ".join(changed) + ".")
    if not lines:
        print("Hiçbir profilin dönemini sıfırlatan bir değişiklik yok.")
        sys.exit(0)
    print("Dönemi sıfırlatabilecek değişiklik var:")
    print("\n".join("  " + x for x in lines))
    print("Değişiklik dönemi etkilemiyorsa elle doğrulanır, etkiliyorsa o profillerin dönemi sıfırlanır. İki durumda da "
          "yeni hal uv run python -m opus_trade.fingerprint <kayıt dosyası> --save ile kaydedilir.")
    sys.exit(1)
