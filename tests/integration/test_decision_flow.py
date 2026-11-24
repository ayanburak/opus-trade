"""
file: test_decision_flow.py
importable: yok

Opus stratejisinin uçtan uca akışı. Çekirdek gerçek parçalarıyla kurulur,
yalnızca model, piyasa ve parite kartları sahtedir. Kararın kontrol hatası,
düzeltme çağrısı, geçen karar, girişin notu, geçersizlik iptali, sonraki
istemde iptalin görünmesi, stop sonrası sınıflandırma ve sınıflandırmanın
kayda bağlanması tek bir senaryoda sınanır. Burada bir kopukluk varsa
parçalar tek tek doğru olsa da Opus yanlış bir hesap görür.

description: Kontrollü karar akışının entegrasyon testi.
"""

import asyncio
import copy

from opus_trade.domain.models import Side
from opus_trade.market.models import Candle
from opus_trade.strategy.llm import strategy as llm_strategy
from opus_trade.llm.base import LLMResult
from tests.integration.harness import SOL, T, Api

LLM = {"strategy": "llm", "params": {"min_wait": 15}, "symbols": [SOL], "balance": 100}
PLAN = {"metin": "Düşüşte 1 saatlik ilk alçalan tepeden short.", "bozulma": "103 üstü", "gecerlilik": "01.01.2030 00:00"}


def decision(eylemler=(), emirler=(), pozisyonlar=(), kapanan=(), alt_tops=(101, 100.8), plan="bozuldu") -> dict:
    return {
        "defter_sinamasi": {"plan": {"durum": plan, "gerekce": "Sınandı."},
                            "yapi": {"durum": "guncellendi", "gerekce": "x"},
                            "gundem": {"durum": "etkisi_yok", "gerekce": "yok"},
                            "emirler": list(emirler), "pozisyonlar": list(pozisyonlar)},
        "pariteler": {SOL: {"yapi": {"ust": {"yon": "alcalan", "tepeler": [106, 104], "dipler": [98, 96]},
                                     "alt": {"yon": "alcalan", "tepeler": list(alt_tops), "dipler": [99, 98.5]}},
                            "rejim": {"rejim": "duzenli_trend", "kanit": "4 saatlikte alçalan tepeler.", "uygun": "evet",
                                      "bekledigi": {"tur": "yok", "fiyat": 0, "saat": ""}}}},
        "kapanan_islemler": list(kapanan), "analiz": "", "eylemler": list(eylemler),
        "not_defteri": {"plan": PLAN, "seviyeler_notu": "101 direnç.", "gundem": ""},
        "ozet": "özet", "sonraki_cagri_dk": 60, "yoneticiye_cevap": "", "yoneticiye_oneri": "",
    }


SHORT = {"tip": "emir_ver", "sembol": SOL, "yon": "short", "tur": "kirilim", "fiyat": 99.5, "marjin_usd": 5, "sl": 101.2,
         "karsi_kanit": "Fonlama negatif, short kalabalık, sıkışma olabilir ama hacim zayıf.",
         "risk_beyani": {"yuzde": 3, "gerekce": "Yapı net."},
         "gecersizlik": {"metin": "1 saatlikte 100,9 üstü", "fiyat": 100.9}}


class FakeLLM:
    def __init__(self):
        self.script, self.requests = [], []

    async def call(self, req, on_tool=None):
        self.requests.append(req)
        return LLMResult(self.script.pop(0), "{}", [], {}, 1.0, 0.1)


def candle(t, high, low, close):
    return Candle(SOL, t // 60 * 60, close, high, low, close, 1, 1, 1, 1)


def test_rejection_correction_entry_invalidation_and_review(tmp_path, monkeypatch):
    """Ret, düzeltme, not, geçersizlik iptali ve sınıflandırma baştan sona birbirine bağlı çalışır."""
    async def cards(*a):
        return "# Parite kartları"
    monkeypatch.setattr(llm_strategy, "cards_text", cards)
    llm = FakeLLM()

    async def go():
        api = Api(tmp_path, llm)
        pid, r = await api.running(LLM)

        # 1. Yön rakamlarla çelişiyor, karar reddedilir. Düzeltmede giriş geçer, notu emre bağlanır.
        bad = decision(eylemler=[SHORT], alt_tops=(100.8, 101))
        llm.script = [bad, decision(eylemler=[SHORT])]
        await api.step(r)
        first, second = api.core.journal.entries(pid)[::-1][:2]
        assert first["record"]["rejection"] and "adım 2" in first["record"]["rejection"][0]
        assert "hiçbir eylemin uygulanmadı" in llm.requests[1].prompt and "1 düzeltme hakkın kaldı" in llm.requests[1].prompt
        [order] = r.broker.state().orders
        assert r.notes.for_order(order.id).invalid_price == 100.9
        assert second["results"][0]["ok"]

        # 2. Son fiyat geçersizliğe değer, emir sistemce iptal edilir, Opus çağrılmaz.
        r.on_candle(candle(api.clock.t + 60, 100.95, 100.2, 100.4), False)
        await api.step(r, 60)
        assert r.broker.state().orders == () and len(llm.requests) == 2

        # 3. Sonraki çağrıda iptal hesap bölümünde görünür. Bu kez piyasa girişi açılır.
        market = {**{k: v for k, v in SHORT.items() if k not in ("tur", "fiyat")}, "tip": "piyasa_giris"}
        llm.script = [decision(eylemler=[market], alt_tops=(101.5, 100.8), plan="gecerli")]
        r.wake("timer")
        await api.step(r, 1)
        assert "sistemin iptal ettiği girişler:\n- SOLUSDT short, geçersizlik 100.90" in llm.requests[2].prompt
        pos = r.broker.state().position(SOL, Side.SHORT)
        assert pos is not None and r.notes.for_position(pos.id)[0].invalid_price == 100.9

        # 4. SL çalışır, sonraki çağrıda pozisyon sınıflandırılmayı bekler. Sınıflandırma kayda bağlanır.
        r.on_mark(SOL, 101.3, api.clock.t + 2)
        assert r.broker.state().positions == ()
        review = [{"pozisyon_no": pos.id, "sinif": "zamanlama_yanlisti", "aciklama": "Erken girdim."}]
        llm.script = [copy.deepcopy(decision(kapanan=review, plan="gecerli"))]
        await api.step(r, 2)
        prompt = llm.requests[3].prompt
        assert f"Sınıflandırılmayı bekleyen pozisyonlar, kapanan_islemler alanında:\n- #{pos.id} SOLUSDT short" in prompt
        assert r.notes.reviews([pos.id])[pos.id]["verdict"] == "zamanlama_yanlisti"
        assert api.core.journal.memory(pid)["notebook"]["plan"] == PLAN
    asyncio.run(go())
