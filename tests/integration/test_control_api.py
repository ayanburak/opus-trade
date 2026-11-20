"""
file: test_control_api.py
importable: yok

Çekirdeğin kontrol API'si üzerinden uçtan uca testler. Çekirdek gerçek
parçalarıyla kurulur, yalnızca piyasa, akış ve model sahtedir. Testler admin
panelinin yapacağı işleri yapar: profil kurar, başlatır, kopyalar, sıfırlar,
müdahale eder, kısıt koyar ve model erişimini kapatır.

description: api.control ve runtime.interventions için entegrasyon testleri.
"""

import asyncio

from opus_trade.domain.models import Side
from opus_trade.domain.trading import OpenPosition
from opus_trade.strategy.base import Decision
from tests.integration.harness import ECHO, RULE, SOL, T, Api, Echo

def run(test):
    def wrapper(tmp_path):
        asyncio.run(test(Api(tmp_path)))
    wrapper.__name__, wrapper.__doc__ = test.__name__, test.__doc__
    return wrapper


@run
async def test_profile_lifecycle_routes(api):
    """Panelin profil düğmeleri doğru uçlara gider. Kopyalama ve sıfırlama genel işlem yoluna düşmemeli."""
    reg = await api.call("GET", "/registry")
    assert {"llm", "liquidation_reversal"} <= {s["key"] for s in reg["strategies"]}
    pid, _ = await api.running(RULE)
    copy = await api.call("POST", f"/profiles/{pid}/clone", json={"name": "Kopya", "short": "K"})
    assert copy["state"] == "draft"
    await api.call("POST", f"/profiles/{pid}/reset", ok=False)
    await api.call("POST", f"/profiles/{pid}/stop", json={"close_all": False})
    reset = await api.call("POST", f"/profiles/{pid}/reset")
    assert reset["account_id"] != copy["account_id"]
    await api.call("POST", f"/profiles/{pid}/explode", ok=False)


@run
async def test_rule_strategy_takes_only_firm_actions(api):
    """Kural stratejisi yönetici mesajı okuyamaz, ona metin gitmez. Grafikten gelen işlem ise uygulanır."""
    pid, r = await api.running(RULE)
    await api.call("POST", f"/profiles/{pid}/interventions", ok=False,
                   json={"kind": "message", "weight": "firm", "params": {"text": "Neden?"}})
    iv = await api.call("POST", f"/profiles/{pid}/interventions",
                        json={"kind": "command",
                              "params": {"type": "open", "symbol": SOL, "side": "long", "leverage": 10, "margin": 10}})
    assert "#1 SOLUSDT long açıldı" in iv["result"]
    acc = await api.call("GET", f"/profiles/{pid}/account")
    assert len(acc["state"]["positions"]) == 1


@run
async def test_firm_restriction_rejects_strategy_and_is_told(api):
    """Kesin kısıt stratejinin aşan komutunu reddeder ve stratejiye yürürlükte olduğu söylenir."""
    pid, r = await api.running(ECHO)
    await api.step(r)
    iv = await api.call("POST", f"/profiles/{pid}/interventions", json={
        "kind": "restrict", "weight": "firm", "params": {"limit": {"type": "max_leverage", "value": 5}},
        "call_now": True})
    Echo.script = [Decision([OpenPosition(SOL, Side.LONG, 10, 10)], ["long_ac SOLUSDT"])]
    await api.step(r, 1)
    ctx = Echo.seen[1]
    assert f"#{iv['id']}, kesin: kaldıraç en fazla 5x." in ctx.admin["restrictions"]
    entry = (await api.call("GET", f"/profiles/{pid}/journal"))[1]
    assert not entry["results"][0]["ok"] and f"#{iv['id']} numaralı kesin kısıtı" in entry["results"][0]["message"]


@run
async def test_withdrawn_message_is_never_shown(api):
    """Gösterilmeden geri çekilen müdahale stratejiye hiç gitmez. Gösterilmiş olan ise artık geri çekilemez."""
    pid, r = await api.running(ECHO)
    await api.step(r)
    msg = {"kind": "message", "weight": "offer"}
    first = await api.call("POST", f"/profiles/{pid}/interventions", json={**msg, "params": {"text": "A"}})
    second = await api.call("POST", f"/profiles/{pid}/interventions", json={**msg, "params": {"text": "B"}})
    await api.call("POST", f"/profiles/{pid}/interventions/{first['id']}/withdraw")
    r.wake("admin")
    await api.step(r, 1)
    items = Echo.seen[-1].admin["items"]
    assert len(items) == 1 and "teklifi: B" in items[0]
    await api.call("POST", f"/profiles/{pid}/interventions/{second['id']}/withdraw", ok=False)


@run
async def test_offer_and_firm_message_reach_strategy_differently(api):
    """Teklif ile kesin talimat aynı metinle gelse de stratejiye farklı söylenir. Karışırsa Opus teklife uyar ya da talimatı tartar."""
    pid, r = await api.running(ECHO)
    await api.step(r)
    for weight in ("offer", "firm"):
        await api.call("POST", f"/profiles/{pid}/interventions",
                       json={"kind": "message", "weight": weight, "params": {"text": "SOL'u kapat."}})
    await api.call("POST", f"/profiles/{pid}/interventions", ok=False,
                   json={"kind": "message", "params": {"text": "Ağırlıksız."}})
    r.wake("admin")
    await api.step(r, 1)
    offer, firm = Echo.seen[-1].admin["items"]
    assert "teklifi: SOL'u kapat. Değerlendir" in offer and "kesin talimatı: SOL'u kapat. Buna uy" in firm


@run
async def test_look_now_wakes_strategy_at_once(api):
    """Şimdi bak düğmesi call_now beklemeden stratejiyi çağırır. Çağırmazsa düğme hiçbir şey yapmamış olur."""
    pid, r = await api.running(ECHO)
    await api.step(r)
    before = len(Echo.seen)
    await api.call("POST", f"/profiles/{pid}/interventions", json={"kind": "look_now"})
    await api.step(r, 1)
    assert len(Echo.seen) == before + 1 and "duruma bakmanı" in Echo.seen[-1].admin["items"][0]


@run
async def test_account_command_has_no_offer(api):
    """Hesap işlemi her zaman kesindir. Teklif olarak gönderilen işlem sessizce uygulanmamalı, reddedilmeli."""
    pid, _ = await api.running(ECHO)
    spec = {"type": "open", "symbol": SOL, "side": "long", "leverage": 10, "margin": 10}
    await api.call("POST", f"/profiles/{pid}/interventions", ok=False,
                   json={"kind": "command", "weight": "offer", "params": spec})
    iv = await api.call("POST", f"/profiles/{pid}/interventions", json={"kind": "command", "params": spec})
    assert iv["weight"] == "firm"
    assert (await api.call("GET", f"/profiles/{pid}/account"))["state"]["positions"][0]["symbol"] == SOL


@run
async def test_stop_with_close_all_closes_positions_and_records_it(api):
    """Durdururken kapatma istenirse pozisyonlar kapanır ve bu bir kesin müdahale olarak kayda geçer."""
    pid, r = await api.running(RULE)
    await api.call("POST", f"/profiles/{pid}/interventions", json={
        "kind": "command",
        "params": {"type": "open", "symbol": SOL, "side": "long", "leverage": 10, "margin": 10}})
    await api.call("POST", f"/profiles/{pid}/stop", json={"close_all": True})
    acc = await api.call("GET", f"/profiles/{pid}/account")
    assert acc["state"]["positions"] == []
    ivs = await api.call("GET", f"/profiles/{pid}/interventions")
    assert ivs[0]["params"] == {"type": "close_all"} and ivs[0]["note"] == "Durdururken kapatıldı."


@run
async def test_model_access_time_must_be_in_the_future(api):
    """Geçmiş bir saat girilirse erişim hiç kapanmaz ve kullanıcı bunu fark etmez, bu yüzden reddedilir."""
    await api.call("POST", "/system/model", ok=False, json={"until": T - 1})
    out = await api.call("POST", "/system/model", json={"until": T + 3600})
    assert out["model_closed_until"] == T + 3600
    assert (await api.call("GET", "/system"))["model_closed_until"] == T + 3600
