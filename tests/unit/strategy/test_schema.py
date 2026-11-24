"""
file: test_schema.py
importable: yok

Karar şemasının testleri. Şema bozuksa claude komutu her çağrıyı reddeder ve
profil karar veremez. Alanların sırası Opus'un düşünme sırasıdır, kontroller
eylemden sonra gelirse Opus önce karar verip sonra gerekçe uydurur.

description: strategy.llm.schema için birim testleri.
"""

import jsonschema

from opus_trade.domain.fees import BINANCE_STANDARD
from opus_trade.strategy.llm import schema
from opus_trade.strategy.llm.steps.base import Steps
from opus_trade.strategy.llm.steps.book import BookStep
from opus_trade.strategy.llm.steps.closed import ClosedStep
from opus_trade.strategy.llm.steps.entry import EntryStep
from opus_trade.strategy.llm.steps.structure import StructureStep
from tests.unit.strategy.test_checks import decision


def steps(structure=("4h", "1h")) -> Steps:
    return Steps([BookStep(), StructureStep(structure, ("duzenli_trend",)), ClosedStep(), EntryStep(BINANCE_STANDARD)])


def test_schema_is_valid_json_schema_for_one_and_many_pairs():
    """Tek ve çok pariteli profilin şeması geçerli bir JSON şemasıdır."""
    for symbols in (["BTCUSDT"], ["BTCUSDT", "XAUUSDT"]):
        jsonschema.Draft202012Validator.check_schema(schema.schema(symbols, steps()))


def test_a_full_decision_validates_and_a_missing_step_does_not():
    """Kontrol adımları zorunludur. Bir adımı atlayan karar şemadan geçmez."""
    s = schema.schema(["BTCUSDT"], steps())
    k = decision()
    jsonschema.validate(k, s)
    del k["defter_sinamasi"]
    assert list(jsonschema.Draft202012Validator(s).iter_errors(k))


def test_fields_follow_the_order_of_thinking():
    """Kontroller eylemden önce, defter eylemden sonra gelir."""
    order = list(schema.schema(["BTCUSDT"], steps())["properties"])
    assert order == ["defter_sinamasi", "pariteler", "kapanan_islemler", "analiz", "eylemler", "not_defteri", "ozet",
                     "sonraki_cagri_dk", "yoneticiye_cevap", "yoneticiye_oneri"]


def test_removed_fields_are_gone():
    """Genel görüş, güven ve paritelerin görüşü kalktı. Hiçbir şeyi bağlamıyorlardı."""
    s = schema.schema(["BTCUSDT"], steps())
    assert "gorus" not in s["properties"] and "guven" not in s["properties"]
    assert set(s["properties"]["pariteler"]["properties"]["BTCUSDT"]["properties"]) == {"yapi", "rejim"}


def test_entry_actions_require_the_check_fields():
    """Giriş eylemi karşı kanıt, risk beyanı ve geçersizlik olmadan şemadan geçmez."""
    s = schema.schema(["BTCUSDT"], steps())
    k = decision(eylemler=[{"tip": "piyasa_giris", "sembol": "BTCUSDT", "yon": "short", "marjin_usd": 5}])
    assert list(jsonschema.Draft202012Validator(s).iter_errors(k))
    k["eylemler"][0].update(karsi_kanit="x" * 20, risk_beyani={"yuzde": 3, "gerekce": "x"},
                            gecersizlik={"metin": "x", "fiyat": 62000})
    jsonschema.validate(k, s)


def test_structure_levels_name_their_interval():
    """Opus ust ve alt'ın hangi aralık olduğunu açıklamadan okur."""
    pair = schema.schema(["BTCUSDT"], steps(("4h", "15m")))["properties"]["pariteler"]["properties"]["BTCUSDT"]
    levels = pair["properties"]["yapi"]["properties"]
    assert levels["ust"]["description"].startswith("4 saatlik") and levels["alt"]["description"].startswith("15 dakikalık")
