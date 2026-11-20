"""
file: static.py
importable: serve_dist

serve_dist() bir FastAPI uygulamasına Vite'ın derlediği klasörü bağlar.
Klasörde olan dosya olduğu gibi döner, olmayan her adres index.html'e düşer.
Böylece arayüzün kendi adresleri, örneğin /profiller ya da /masa/3, sayfa
yenilense de açılır.

description: Derlenmiş tek sayfalık arayüzün dosyalarının sunulması.
"""

# NOTE: İstenen yol klasörün dışına çıkıyorsa, örneğin ../ ile, dosya yokmuş
# gibi index.html döner. Klasör henüz derlenmemişse 503 ve Türkçe bir mesaj
# döner, böylece boş bir sayfa yerine neyin eksik olduğu görünür.
#
# NOTE: index.html önbelleğe alınmaz, yeni yayında hemen yenisi gelsin.
# Vite'ın assets klasöründeki dosyaların adı içerikten üretilir, onlar uzun
# süre önbellekte kalabilir.

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, PlainTextResponse


def serve_dist(app: FastAPI, dist: Path) -> None:
    root = dist.resolve()

    @app.get("/{path:path}", include_in_schema=False)
    async def page(path: str):
        index = root / "index.html"
        if not index.exists():
            return PlainTextResponse("Arayüz henüz derlenmedi.", status_code=503)
        target = (root / path).resolve()
        if path and target.is_file() and target.is_relative_to(root):
            cache = "public, max-age=31536000, immutable" if target.parent.name == "assets" else "no-cache"
            return FileResponse(target, headers={"Cache-Control": cache})
        return FileResponse(index, headers={"Cache-Control": "no-cache"})
