"""
file: web.py
importable: load_factory

opus-web servisinin giriş noktası. Çekirdeğin soketine tek bir istemci kurar,
herkese açık siteyi OPUS_WEB_SOCKET soketinde açar. OPUS_ADMIN_SOCKET ve
OPUS_ADMIN_APP verilmişse admin uygulamasını da ikinci sokette açar. SIGTERM
ya da Ctrl+C gelince süren istekleri bitirip kapanır.

    python -m opus_trade.app.web

OPUS_ADMIN_APP modul:fonksiyon biçimindedir. Fonksiyon çekirdeğe giden
istemciyi ve ortamı alıp bir ASGI uygulaması döndürür. load_factory() bu
yazıdan fonksiyonu bulur.

description: Herkese açık siteyi ve admin panelini sunan servisin kuruluş noktası.
"""

# NOTE: Admin uygulaması public depoda değildir, adını ortam değişkeni verir.
# Böylece public kod admin kodunu hiç anmaz ve public depo kendi başına
# yalnızca herkese açık siteyle çalışır.
#
# TODO: Kapanışta açık canlı akışlar SHUTDOWN_S sonunda kesilir ve uvicorn
# her biri için günlüğe ERROR ve iz yazar. İşlevsel bir sorun değil ama her
# yayında günlükte sahte hata görünür. Akışlar kapanış sinyalini dinleyip
# kendileri bitmeli.
#
# NOTE: Giriş kontrolü bu serviste değildir. Admin soketine nginx yalnızca
# admin merkezinin yetki kontrolünden geçen isteği aktarır.

import asyncio
import importlib
import logging
import os
import signal
from typing import Callable

import httpx
import uvicorn

from ..config import WebConfig
from ..web.server import create_site

log = logging.getLogger("opus_trade.web")


def load_factory(spec: str) -> Callable:
    """modul:fonksiyon yazısından fonksiyonu bulur.

    Raises:
        SystemExit: Yazının biçimi yanlışsa ya da modül veya fonksiyon bulunamazsa.
    """
    module, _, attr = spec.partition(":")
    if not module or not attr:
        raise SystemExit(f"OPUS_ADMIN_APP modul:fonksiyon biçiminde olmalı: {spec}")
    try:
        return getattr(importlib.import_module(module), attr)
    except (ImportError, AttributeError) as e:
        raise SystemExit(f"Admin uygulaması yüklenemedi: {e}") from None


# Canlı akış bağlantıları hiç kendiliğinden bitmez. Kapanışta en fazla
# SHUTDOWN_S beklenir, yoksa açık bir panel sekmesi servisin durmasını
# systemd'nin zaman aşımına kadar uzatırdı.
SHUTDOWN_S = 5


def _server(app, sock) -> uvicorn.Server:
    sock.unlink(missing_ok=True)
    server = uvicorn.Server(uvicorn.Config(app, uds=str(sock), log_level="warning",
                                           timeout_graceful_shutdown=SHUTDOWN_S))
    server.install_signal_handlers = lambda: None
    return server


async def _main(cfg: WebConfig) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    transport = httpx.AsyncHTTPTransport(uds=str(cfg.core_socket))
    async with httpx.AsyncClient(transport=transport, base_url="http://core", timeout=20) as core:
        servers = [_server(create_site(core, cfg.public_dist), cfg.web_socket)]
        if cfg.admin_socket and cfg.admin_app:
            servers.append(_server(load_factory(cfg.admin_app)(core, dict(os.environ)), cfg.admin_socket))
        tasks = [asyncio.create_task(s.serve()) for s in servers]
        log.info("Web servisi başladı, %d soket açık.", len(servers))
        await stop.wait()
        for s in servers:
            s.should_exit = True
        await asyncio.gather(*tasks, return_exceptions=True)
    log.info("Web servisi durdu.")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(_main(WebConfig.from_env()))


if __name__ == "__main__":
    main()
