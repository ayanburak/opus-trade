"""
file: test_web.py
importable: yok

opus-web'in herkese açık tarafının testleri: derlenmiş sayfanın sunulması,
API'nin /api altında çalışması, ayarların okunması ve admin uygulamasının
adından yüklenmesi. Burada bir hata ya sunucudaki herhangi bir dosyayı
dışarı açar, ya sayfa yenilenince 404 verir, ya da yeni yayından sonra eski
sayfa önbellekte kalır.

description: web.server, web.static, config.WebConfig ve app.web için testler.
"""

import asyncio

import httpx
import pytest

from opus_trade.app.web import load_factory
from opus_trade.config import WebConfig
from opus_trade.web.server import create_site
from tests.integration.harness import ECHO, Api


def site(api, dist):
    return httpx.AsyncClient(transport=httpx.ASGITransport(create_site(api.client, dist)), base_url="http://web")


def built(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>masa</html>")
    (dist / "assets" / "app-1a2b.js").write_text("console.log(1)")
    (tmp_path / "secret.txt").write_text("gizli")
    return dist


def test_pages_files_and_fallback(tmp_path):
    """Dosya olduğu gibi döner, bilinmeyen adres sayfaya düşer, klasör dışına çıkılamaz. index önbelleğe alınmaz."""
    async def go():
        api = Api(tmp_path)
        async with site(api, built(tmp_path)) as c:
            js = await c.get("/assets/app-1a2b.js")
            assert js.text == "console.log(1)" and "immutable" in js.headers["cache-control"]
            page = await c.get("/profiller/3")
            assert page.text == "<html>masa</html>" and page.headers["cache-control"] == "no-cache"
            for path in ("/../secret.txt", "/%2e%2e/secret.txt", "/assets/../../secret.txt"):
                assert "gizli" not in (await c.get(path)).text
    asyncio.run(go())


def test_api_is_mounted_and_missing_build_is_told(tmp_path):
    """/api herkese açık API'ye gider, sayfaya düşmez. Arayüz derlenmemişse boş sayfa yerine 503 ve mesaj döner."""
    async def go():
        api = Api(tmp_path)
        pid, _ = await api.running(ECHO)
        await api.call("POST", "/publish", json={"profile_id": pid})
        async with site(api, tmp_path / "yok") as c:
            assert (await c.get("/api/profile")).json()["id"] == pid
            r = await c.get("/")
            assert r.status_code == 503 and "derlenmedi" in r.text
    asyncio.run(go())


def test_web_config_needs_sockets_and_admin_is_optional():
    """Soketler verilmezse servis açılmaz. Admin ayarları yoksa yalnızca herkese açık site çalışır."""
    with pytest.raises(SystemExit):
        WebConfig.from_env({"OPUS_CORE_SOCKET": "/run/c.sock"})
    cfg = WebConfig.from_env({"OPUS_CORE_SOCKET": "/run/c.sock", "OPUS_WEB_SOCKET": "/run/w.sock"})
    assert cfg.admin_socket is None and cfg.admin_app is None and cfg.public_dist.name == "dist"


def test_admin_factory_is_loaded_by_name():
    """Admin uygulaması adıyla yüklenir. Yanlış ad sessizce yok sayılmaz, servis açılırken hata verir."""
    assert load_factory("opus_trade.web.server:create_site") is create_site
    for bad in ("opus_trade.web.server", "yok.modul:f", "opus_trade.web.server:yok"):
        with pytest.raises(SystemExit):
            load_factory(bad)
