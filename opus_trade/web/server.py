"""
file: server.py
importable: create_site

create_site() herkese açık siteyi kurar: /api altında yayındaki profilin
süzülmüş verisi, geri kalan her adreste derlenmiş sayfa. Çekirdeğe giden
istemciyi dışarıdan alır, opus-web'in giriş noktası onu çekirdeğin soketine
bağlar.

description: Herkese açık sayfanın ve API'sinin sunucusu.
"""

# NOTE: Bu uygulamada yazma ucu yoktur. Herkese açık API yalnızca GET taşır,
# nginx de bu adrese GET dışındaki istekleri hiç geçirmez.

from pathlib import Path

import httpx
from fastapi import FastAPI

from ..api.public import create_public_app
from .static import serve_dist


def create_site(core: httpx.AsyncClient, dist: Path, max_clients: int = 50) -> FastAPI:
    app = FastAPI()
    app.mount("/api", create_public_app(core, max_clients))
    serve_dist(app, dist)
    return app
