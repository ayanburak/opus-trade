"""
file: log.py
importable: write, collect

Alt görev kaydı. Ekip sunucusu her alt görevi çağrının çalışma klasöründeki
bir JSON satırları dosyasına write() ile yazar: an, rol, model, görev,
sonuç, hata, araçlar, tüketim, dolar karşılığı ve süre. Çağrı bitince LLM
stratejisi collect() ile dosyayı okur ve siler. Strateji dosyanın biçimini
bilmez.

description: Alt görevlerin kayıt dosyasının yazıcısı ve okuyucusu.
"""

# NOTE: Dosyayı ayrı bir süreç, ekip sunucusu yazar, çekirdek okur. Aynı
# çağrıda görevler paralel bittiği için her satır tek bir yazmayla eklenir.
# Bozuk bir satır atlanır, bir görevin kaydı okunamasa da diğerleri kalır.

import json
from pathlib import Path


def write(path: Path, rec: dict) -> None:
    """Bir alt görevin kaydını dosyanın sonuna ekler."""
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def collect(path: Path) -> list[dict]:
    """Dosyadaki kayıtları sırasıyla döndürür ve dosyayı siler. Dosya yoksa boş liste döner."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    path.unlink(missing_ok=True)
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
