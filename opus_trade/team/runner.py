"""
file: runner.py
importable: TeamRunner, PARALLEL

TeamRunner Opus'un verdiği görevleri alt ajanlara çalıştırır. Her görev bir
rol ve bir metindir. Kotayı aşan, rolü bilinmeyen ya da boş görev
çalışmaz, yerine Türkçe bir ret cümlesi döner. Kabul edilen görevler aynı
anda çalışır, her biri LLMClient ile şemasız bir çağrıdır ve kendi geçici
çalışma klasöründe koşar. Her görevin kaydı log.write() ile dosyaya düşer.
run() bütün görevlerin sonuçlarını ve kalan hakkı tek bir metin olarak
döndürür.

    runner = TeamRunner(llm, {"haiku": 10}, market_env, work_dir, log_path, deadline)
    text = await runner.run([("haiku", "Bugünkü ABD verilerini bul.")])

description: Alt görevlerin kota, eşzamanlılık ve süreyle çalıştırılması.
"""

# NOTE: Kota bu nesnenin durumudur. claude her ana çağrıda ekip sunucusunu
# yeniden başlattığı için sayaç kendiliğinden çağrı başınadır. Opus aynı
# çağrıda aracı birkaç kez çağırırsa kalan hak düşerek sürer.
#
# NOTE: Aynı anda çalışan görev sayısı PARALLEL ile sınırlıdır, kota ne olursa
# olsun. Her görev ayrı bir claude süreci ve onun piyasa sunucusunu başlatır,
# 24.11 ölçümünde ikisi birlikte 320 MB kadar. Sunucu takassız 4 GB'tır, elli
# görev birden açılırsa belleği yetmez. Sıra bekleyen görev de ana çağrının
# süresinden yer, bu yüzden her görev başlarken kalan süreye bakar.
#
# NOTE: deadline time.monotonic() cinsinden mutlak bir andır. Görevin süre
# sınırı rolün sınırı ile kalan sürenin küçüğüdür. Böylece alt görev claude'un
# araç zaman aşımından ve ana çağrının süresinden önce biter, adimlar-ve-ekip.md.

import asyncio
import shutil
import time
from pathlib import Path

from ..llm.base import LLMClient, LLMError, LLMRequest
from . import log
from .roles import ROLES, ROLE_KEYS, toolset

PARALLEL = 3

# Kalan süre bundan azsa görev hiç başlatılmaz, başlasa da yarıda kesilirdi.
_MIN_TIME_S = 30

# Bir aracın sonucundan kayda giren en uzun metin.
_RESULT_KEEP = 4000


def _tool(t: dict) -> dict:
    out = {k: v for k, v in t.items() if k != "id"}
    if isinstance(out.get("result"), str):
        out["result"] = out["result"][:_RESULT_KEEP]
    return out


class TeamRunner:
    """Bir ana çağrının alt görev çalıştırıcısı.

    Args:
        llm: Alt görevleri çalıştıran model istemcisi.
        quota: Rolden bu çağrıda kalan görev hakkına.
        market_env: Alt ajanların piyasa araçları sunucusunun ortamı.
        work_dir: Görevlerin geçici çalışma klasörlerinin yeri.
        log_path: Görev kayıtlarının yazıldığı dosya.
        deadline: Görevlerin bitmesi gereken son an, time.monotonic() cinsinden.
        parallel: Aynı anda çalışan en fazla görev.
    """

    def __init__(self, llm: LLMClient, quota: dict[str, int], market_env: dict[str, str], work_dir: Path,
                 log_path: Path, deadline: float, parallel: int = PARALLEL) -> None:
        self.llm = llm
        self.left = {r: max(0, int(quota.get(r, 0))) for r in ROLE_KEYS}
        self.market_env = market_env
        self.work_dir = work_dir
        self.log_path = log_path
        self.deadline = deadline
        self.gate = asyncio.Semaphore(parallel)
        self.count = 0

    def left_text(self) -> str:
        return "Bu çağrıda kalan görev hakkın: " + ", ".join(f"{r} {n}" for r, n in self.left.items()) + "."

    async def run(self, tasks: list[tuple[str, str]]) -> str:
        """Görevleri çalıştırır ve sonuçlarını görev sırasıyla tek metin olarak döndürür."""
        jobs, out = [], {}
        for i, (role, text) in enumerate(tasks, 1):
            if role not in ROLES:
                out[i] = f"Görev {i} çalışmadı: {role} diye bir rol yok. Roller: {', '.join(ROLE_KEYS)}."
            elif not text.strip():
                out[i] = f"Görev {i} çalışmadı: görev metni boş."
            elif self.left[role] <= 0:
                out[i] = (f"Görev {i} çalışmadı: bu çağrıda {role} için görev hakkın kalmadı. Hak çağrı başına ve "
                          f"gün başına sınırlıdır.")
            else:
                self.left[role] -= 1
                jobs.append((i, role, text))
        done = await asyncio.gather(*(self._one(role, text) for _, role, text in jobs))
        for (i, role, _), text in zip(jobs, done):
            out[i] = f"Görev {i}, {role}:\n{text}"
        return "\n\n---\n\n".join([out[i] for i in sorted(out)] + [self.left_text()])

    async def _one(self, key: str, text: str) -> str:
        role = ROLES[key]
        async with self.gate:
            self.count += 1
            work = self.work_dir / f"gorev-{self.count}"
            rec = {"time": int(time.time()), "role": key, "model": role.model, "task": text, "result": None,
                   "error": None, "tools": [], "usage": None, "cost_usd": None, "duration_s": None}
            left = self.deadline - time.monotonic()
            if left < _MIN_TIME_S:
                rec.update(error="Ana çağrının süresi bu görevi çalıştırmaya yetmedi.", skipped=True)
                log.write(self.log_path, rec)
                return f"Çalışmadı: {rec['error']}"
            req = LLMRequest(role.system, text, None, role.model, role.effort, role.max_turns, work,
                             toolset(self.market_env), timeout_s=int(min(role.timeout_s, left)))
            try:
                res = await self.llm.call(req)
                rec.update(result=res.text, tools=[_tool(t) for t in res.tools], usage=res.usage,
                           cost_usd=res.cost_usd, duration_s=res.duration_s)
                answer = res.text or "Alt ajan boş bir cevap döndürdü."
            except LLMError as e:
                rec.update(error=str(e), tools=[_tool(t) for t in e.tools])
                answer = f"Hata: {e}"
            finally:
                shutil.rmtree(work, ignore_errors=True)
            log.write(self.log_path, rec)
            return answer
