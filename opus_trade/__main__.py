"""
file: __main__.py
importable: yok

Proje klasöründe uv run python -m opus_trade komutuyla bütün profilleri tek programda
başlatır. Kaldığı yerden devam etmek için aynı komut tekrar çalıştırılır.

uv run python -m opus_trade shutdown komutu çalışan programa planlı kapanış
isteği gönderir. Her profilin Opus'u bir kez çağrılıp kapanış bildirilir,
hepsinin kararı uygulanınca program kendini kapatır. --return ile tahmini
dönüş anı verilebilir, örneğin --return "2026-11-17 09:00". Ctrl+C de
programı kapatır ama o zaman Opus'a önceden haber verilmez.

TRADE_SOCKET ortam değişkeni verilirse program port yerine o Unix soket
dosyasını dinler, shutdown komutu da isteğini o dosya üzerinden gönderir.

description: Programı başlatan ve çalışan programı kapatan giriş noktası.
"""

import argparse
import logging
import os
import socket
import sys
from datetime import datetime

import httpx

from . import settings


# Kapatma anahtarını program açılışta dosyaya yazar. Dosya yoksa program
# çalışmıyor demektir.
def _shutdown(return_at: str | None) -> None:
    try:
        token = settings.TOKEN_PATH.read_text()
    except FileNotFoundError:
        sys.exit("Program çalışmıyor, kapatma anahtarı bulunamadı.")
    ts = int(datetime.strptime(return_at, "%Y-%m-%d %H:%M").timestamp()) if return_at else None
    if settings.WEB_SOCKET:
        client = httpx.Client(transport=httpx.HTTPTransport(uds=settings.WEB_SOCKET), base_url="http://localhost")
    else:
        client = httpx.Client(base_url=f"http://{settings.WEB_HOST}:{settings.WEB_PORT}")
    r = client.post("/api/shutdown", json={"return_at": ts}, headers={"Authorization": f"Bearer {token}"}, timeout=10)
    r.raise_for_status()
    print("Planlı kapanış başladı. Bütün profillerin Opus'u son kararını verince program kapanır.")


def _run() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    # Kütüphanelerin her istek için yazdığı satırlar programın kendi kayıtlarını boğmasın diye susturulur.
    for name in ("httpx", "uvicorn.access", "websockets"):
        logging.getLogger(name).setLevel(logging.WARNING)

    names = ", ".join(p.name for p in settings.PROFILES.values())
    # Tarayıcıdaki canlı akış bağlantısı hiç kendiliğinden bitmez. Kapanışta
    # beklenecek süre sınırlanmazsa Ctrl+C programı hiç durduramaz.
    opts = {"log_level": "warning", "timeout_graceful_shutdown": 3}
    if settings.WEB_SOCKET:
        print(f"{names} · Soket: {settings.WEB_SOCKET}")
        uvicorn.run("opus_trade.web.server:app", fd=_bind_socket(settings.WEB_SOCKET), **opts)
    else:
        print(f"{names} · Arayüz: http://{settings.WEB_HOST}:{settings.WEB_PORT}")
        uvicorn.run("opus_trade.web.server:app", host=settings.WEB_HOST, port=settings.WEB_PORT, **opts)


# Soket dosyası herkese yazılabilir açılır, çünkü nginx başka bir kullanıcıyla
# çalışır. Bu, 127.0.0.1'deki bir portla aynı erişimdir: sunucudaki yerel
# kullanıcılar bağlanabilir, dışarıdan yalnızca nginx'in açtığı yol görünür.
# Önceki çalışmadan kalmış bir dosya varsa önce silinir.
def _bind_socket(path: str) -> int:
    if os.path.exists(path):
        os.unlink(path)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(path)
    os.chmod(path, 0o666)
    s.listen(2048)
    os.set_inheritable(s.fileno(), True)
    _bound.append(s)
    return s.fileno()


# Soket nesnesi uvicorn çalıştığı sürece yaşamalı, yoksa çöp toplayıcı kapatır.
_bound: list[socket.socket] = []


parser = argparse.ArgumentParser(prog="python -m opus_trade")
sub = parser.add_subparsers(dest="command")
stop = sub.add_parser("shutdown", help="çalışan programa planlı kapanış isteği gönderir")
stop.add_argument("--return", dest="return_at", help='tahmini dönüş, örneğin "2026-11-17 09:00"')
args = parser.parse_args()

if args.command == "shutdown":
    _shutdown(args.return_at)
else:
    _run()
