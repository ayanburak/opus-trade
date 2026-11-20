"""
file: config.py
importable: AppConfig, WebConfig

AppConfig çekirdeğin ve toplayıcının, WebConfig opus-web'in ortamdan okunan
ayarlarıdır. Yalnızca app paketindeki
giriş noktaları from_env() ile kurar ve ihtiyacı olan parçaya verir. Hiçbir
modül import edilirken ortam okumaz.

description: Ortam değişkenlerinden okunan program ayarları.
"""

# NOTE: OPUS_STATE zorunludur ve varsayılanı yoktur. Varsayılan bir klasör
# olsaydı yerelde unutulan bir değişken programı sessizce gerçek veriye
# benzeyen bir yere yazdırabilirdi. Sunucuda ayrı bir durum klasörü, yerel
# denemelerde geçici bir klasör verilir. OPUS_CORE_SOCKET çekirdeğin kontrol
# API'sinin Unix soketidir, verilmezse çekirdek API açmadan çalışır.

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppConfig:
    state_dir: Path
    core_socket: Path | None = None

    @property
    def market_db(self) -> Path:
        return self.state_dir / "market.db"

    @property
    def backups(self) -> Path:
        return self.state_dir / "backups"

    @property
    def core_db(self) -> Path:
        return self.state_dir / "core.db"

    # Her LLM profilinin claude komutu kendi klasöründe çalışır.
    def work_dir(self, profile_id: int) -> Path:
        return self.state_dir / "work" / str(profile_id)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "AppConfig":
        """Ayarları ortam değişkenlerinden okur.

        Raises:
            SystemExit: OPUS_STATE verilmemişse.
        """
        env = os.environ if env is None else env
        state = env.get("OPUS_STATE")
        if not state:
            raise SystemExit("OPUS_STATE verilmedi. Verinin yazılacağı klasör bu değişkenle verilir.")
        sock = env.get("OPUS_CORE_SOCKET")
        return cls(state_dir=Path(state), core_socket=Path(sock) if sock else None)


# opus-web veriye hiç dokunmaz, OPUS_STATE istemez. Her şeyi çekirdeğin
# soketinden alır. Admin soketi ve admin uygulaması verilmezse yalnızca
# herkese açık site açılır, public depo kendi başına böyle çalışır.
@dataclass(frozen=True)
class WebConfig:
    core_socket: Path
    web_socket: Path
    public_dist: Path
    admin_socket: Path | None = None
    admin_app: str | None = None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "WebConfig":
        """Ayarları ortam değişkenlerinden okur.

        Raises:
            SystemExit: OPUS_CORE_SOCKET ya da OPUS_WEB_SOCKET verilmemişse.
        """
        env = os.environ if env is None else env
        core, web = env.get("OPUS_CORE_SOCKET"), env.get("OPUS_WEB_SOCKET")
        if not core or not web:
            raise SystemExit("OPUS_CORE_SOCKET ve OPUS_WEB_SOCKET verilmeli.")
        dist = env.get("OPUS_PUBLIC_DIST") or str(Path(__file__).resolve().parents[1] / "web" / "dist")
        admin = env.get("OPUS_ADMIN_SOCKET")
        return cls(core_socket=Path(core), web_socket=Path(web), public_dist=Path(dist),
                   admin_socket=Path(admin) if admin else None, admin_app=env.get("OPUS_ADMIN_APP") or None)
