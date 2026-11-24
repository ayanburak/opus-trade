"""
file: plugins.py
importable: load

load() bütün strateji türlerinin ve tetikleyicilerin modüllerini yükler.
Her modül yüklenirken kendini STRATEGIES ya da TRIGGERS kayıt defterine
yazar. Giriş noktaları ve testler kayıt defterini okumadan önce bunu bir kez
çağırır.

description: Eklenti modüllerinin yüklendiği tek yer.
"""

# NOTE: Yeni bir strateji türü ya da tetikleyici yazıldığında modülü buraya bir
# satırla eklenir. Kayıt defterinin geri kalanı, panel ve arayüz dahil, onu
# kendiliğinden görür.

import importlib

_MODULES = (
    "opus_trade.strategy.llm.kinds",
    "opus_trade.strategy.rules.liquidation_reversal",
    "opus_trade.strategy.rules.spike_fade",
    "opus_trade.triggers.liquidation_wave",
    "opus_trade.triggers.trade_burst",
)


def load() -> None:
    for name in _MODULES:
        importlib.import_module(name)
