"""
file: conftest.py
importable: yok

Testlerin ortak hazırlığı. pytest bu dosyayı test modüllerinden önce kendisi
yükler. account fixture'ı geçici bir veritabanıyla yüz dolarlık bir hesap
kurar. market fixture'ı Binance'e giden her isteği sabit, her çalıştırmada
aynı çıkan sahte cevaplarla değiştirir ve gelen istekleri kaydeder.

description: Hesap motoru ve araç havuzu testleri için ortak fixture'lar.
"""

# NOTE: TRADE_DATA paket yüklenmeden önce var olmayan bir klasöre çevrilir.
# settings modülü veri yolunu yüklenirken okuduğu için bunun en başta olması
# gerekiyor. Testler kendi veritabanlarını tmp_path altında açar, böylece bir
# test yanlışlıkla settings.DATA_DIR'e yazmaya kalksa bile asıl veriye değil
# bu boş yola gider.
#
# XXX: Sahte cevaplar yalnızca araçların okuduğu alanları taşır. Bir araç
# Binance cevabından yeni bir alan okumaya başlarsa _fake_request'e de o alan
# eklenmeli, yoksa test KeyError ile düşer.

import math
import os
import tempfile
from pathlib import Path

os.environ["TRADE_DATA"] = str(Path(tempfile.gettempdir()) / "opus-trade-test-bos")

import pytest

from opus_trade import settings
from opus_trade.db import connect
from opus_trade.engine.account import Account
from opus_trade.market import data as market_data

# Testlerdeki bütün zamanlar bu anın etrafında kurulur. 17.11.2026 12:00 UTC.
NOW = 1794916800

_STEP_S = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "2h": 7200, "4h": 14400,
           "6h": 21600, "8h": 28800, "12h": 43200, "1d": 86400, "3d": 259200, "1w": 604800, "1M": 2592000}


# --- Hesap ---

# Profil dosyası okunmaz, testin ihtiyacı kadar alanla elde kurulur. İki
# parite yeterli: BTC büyük adımlı ve yüksek kaldıraçlı, SOL küçük fiyatlı.
@pytest.fixture
def account(tmp_path) -> Account:
    profile = settings.Profile(
        id="test", name="Test Opus", short="Test", balance=100.0,
        symbols={s: settings.SYMBOLS[s] for s in ("BTCUSDT", "SOLUSDT")},
        period_s=14 * 86400, min_wait=5, max_wait=240, style="",
    )
    return Account(connect(tmp_path / "account.db"), profile)


# --- Piyasa ---

# Fiyat yüzün etrafında yavaşça salınır. Sinüs, seviyeler ve hacim profili
# gibi araçların boş dönmemesi için yeterince tepe ve dip üretir.
def _price(t: int) -> float:
    return 100 * (1 + 0.03 * math.sin(t / 7200) + 0.01 * math.sin(t / 900))


def _klines(interval: str, limit: int, start_ms: int | None) -> list[list]:
    step = _STEP_S[interval]
    first = start_ms // 1000 if start_ms else NOW - limit * step
    out = []
    for i in range(limit):
        t = first + i * step
        # Uçlara mumun ortasındaki fiyat da katılır. Yalnızca açılış ve
        # kapanıştan kurulan uçlar komşu mumlarda birebir eşit çıkıyor,
        # seviyeler aracı da eşit tepeyi tepe saymıyor.
        o, mid, c = _price(t), _price(t + step // 2), _price(t + step)
        out.append([t * 1000, f"{o}", f"{max(o, mid, c) * 1.002}", f"{min(o, mid, c) * 0.998}", f"{c}",
                    f"{100 + i % 17}"])
    return out


def _fake_request(calls: list):
    async def request(path: str, **params):
        calls.append((path, params))
        n = params.get("limit", 30)
        times = [(NOW - (n - i) * 3600) * 1000 for i in range(n)]
        if path == "/fapi/v1/klines":
            return _klines(params["interval"], n, params.get("startTime"))
        if path == "/fapi/v1/depth":
            return {"bids": [[f"{99.9 - i / 10:.1f}", "1.5"] for i in range(n)],
                    "asks": [[f"{100.1 + i / 10:.1f}", "2.0"] for i in range(n)]}
        if path == "/fapi/v1/fundingRate":
            return [{"fundingTime": t, "fundingRate": "0.0001"} for t in times]
        if path == "/futures/data/openInterestHist":
            return [{"timestamp": t, "sumOpenInterest": "1000.5", "sumOpenInterestValue": "100050000"} for t in times]
        if path in ("/futures/data/globalLongShortAccountRatio", "/futures/data/topLongShortPositionRatio"):
            return [{"timestamp": t, "longAccount": "0.55"} for t in times]
        if path == "/futures/data/takerlongshortRatio":
            return [{"timestamp": t, "buySellRatio": "1.02"} for t in times]
        raise AssertionError(f"Testte beklenmeyen Binance isteği: {path}")
    return request


# Araçlar market_data.request'i modülün kendisinden çağırdığı için yalnızca
# o fonksiyonu değiştirmek bütün araçlara yeter.
@pytest.fixture
def market(monkeypatch) -> list:
    calls: list = []
    monkeypatch.setattr(market_data, "request", _fake_request(calls))
    return calls
