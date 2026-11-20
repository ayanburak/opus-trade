"""
file: config.py
importable: AppConfig

AppConfig programın ortamdan okunan ayarlarıdır. Yalnızca app paketindeki
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
