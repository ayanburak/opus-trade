"""
file: refresh_brackets.py
importable: fetch, write, main

Binance'in imzalı leverageBracket ucundan bütün paritelerin kaldıraç kademe
tablosunu alır ve paketin brackets.json dosyasına yazar. Anahtar ve gizli
anahtar, yolu BINANCE_KEY_FILE ile verilen dosyanın ilk iki satırından okunur.
Yalnızca okuma izinli bir anahtar yeter.

Kullanım: BINANCE_KEY_FILE=<dosya> python -m opus_trade.market.binance.refresh_brackets

description: Kaldıraç kademe tablosunun Binance'ten yenilenmesi.
"""

# NOTE: Anahtar programın hiçbir yerinde saklanmaz, ekrana ve dosyaya
# yazılmaz. Komut bitince anahtar dosyası silinebilir, anahtar da Binance'ten
# kaldırılabilir. Tablonun tek kullanıcısı katalogdur.

import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx

from .rest import BASE_URL
from .specs import BRACKETS_FILE

_PATH = "/fapi/v1/leverageBracket"


# Binance imzalı istekte sorgu metninin gizli anahtarla HMAC SHA256 özetini ister.
def _signed(params: dict, secret: str) -> dict:
    query = urlencode(params)
    return {**params, "signature": hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()}


def fetch(client: httpx.Client, key: str, secret: str, now_ms: int) -> dict[str, list[dict]]:
    """Bütün paritelerin kademelerini sembolden kademe listesine döndürür.

    Raises:
        RuntimeError: Binance isteği reddederse.
    """
    r = client.get(_PATH, params=_signed({"timestamp": now_ms}, secret), headers={"X-MBX-APIKEY": key})
    if r.status_code != 200:
        raise RuntimeError(f"Binance {r.status_code} döndürdü: {r.text[:200]}")
    return {row["symbol"]: row["brackets"] for row in r.json()}


def write(brackets: dict[str, list[dict]], path: Path, now: int) -> None:
    path.write_text(json.dumps({"source": f"Binance GET {_PATH}, imzalı uç, bütün pariteler", "time": now,
                                "brackets": dict(sorted(brackets.items()))}, ensure_ascii=False) + "\n")


def main() -> None:
    where = os.environ.get("BINANCE_KEY_FILE")
    if not where:
        sys.exit("BINANCE_KEY_FILE verilmedi. Anahtarın ve gizli anahtarın yazılı olduğu dosyanın yolu verilmeli.")
    key, secret = Path(where).read_text().split()[:2]
    with httpx.Client(base_url=BASE_URL, timeout=20) as client:
        brackets = fetch(client, key, secret, int(time.time() * 1000))
    write(brackets, BRACKETS_FILE, int(time.time()))
    print(f"{len(brackets)} paritenin kademesi {BRACKETS_FILE} dosyasına yazıldı.")


if __name__ == "__main__":
    main()
