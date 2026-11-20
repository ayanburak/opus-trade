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
# denemelerde geçici bir klasör verilir.

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppConfig:
    state_dir: Path

    @property
    def market_db(self) -> Path:
        return self.state_dir / "market.db"

    @property
    def backups(self) -> Path:
        return self.state_dir / "backups"

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
        return cls(state_dir=Path(state))
