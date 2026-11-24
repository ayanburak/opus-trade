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
from opus_trade.domain.trading import OrderType, PlaceOrder
from opus_trade.market.models import Candle
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
async def test_test_profile_is_marked_and_never_published(api):
    """Test profili Genel bakışın toplamlarından işaretle ayrılır. İşaret profil bilgisinde ve genel bakışta görünmeli.
    Test profili herkese açık sayfada yayınlanamamalı, yayındaki profil de yayından kalkmadan test yapılamamalı.
    Var olan profiller test değil olarak başlamalı."""
    pid, _ = await api.running(RULE)
    assert (await api.call("GET", f"/profiles/{pid}"))["test"] is False
    await api.call("POST", "/publish", json={"profile_id": pid})
    bad = await api.call("POST", f"/profiles/{pid}/test", ok=False, json={"test": True})
    assert "yayından kaldırılmalı" in bad["detail"]
    await api.call("POST", "/publish", json={"profile_id": None})
    assert (await api.call("POST", f"/profiles/{pid}/test", json={"test": True}))["test"] is True
    [row] = (await api.call("GET", "/overview"))["profiles"]
    assert row["test"] is True
    bad = await api.call("POST", "/publish", ok=False, json={"profile_id": pid})
    assert "test profili" in bad["detail"]
    assert (await api.call("POST", f"/profiles/{pid}/test", json={"test": False}))["test"] is False


@run
async def test_rule_strategy_takes_only_firm_actions(api):
    """Kural stratejisi yönetici mesajı okuyamaz, ona metin gitmez. Grafikten gelen işlem ise uygulanır."""
    pid, r = await api.running(RULE)
    await api.call("POST", f"/profiles/{pid}/interventions", ok=False,
                   json={"kind": "message", "weight": "firm", "params": {"text": "Neden?"}})
    iv = await api.call("POST", f"/profiles/{pid}/interventions",
                        json={"kind": "command",
                              "params": {"type": "order", "order_type": "market", "symbol": SOL, "side": "long", "qty": 1}})
    assert "SOLUSDT long pozisyonu 1 SOL ile" in iv["result"]
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
    Echo.script = [Decision([PlaceOrder(SOL, Side.LONG, OrderType.MARKET, qty=1)], ["long_ac SOLUSDT"])]
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
    spec = {"type": "order", "order_type": "market", "symbol": SOL, "side": "long", "qty": 1}
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
        "kind": "command", "params": {"type": "order", "order_type": "market", "symbol": SOL, "side": "long", "qty": 1}})
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


@run
async def test_overview_counts_today_and_open_risk(api):
    """Genel bakış bugünkü çağrıları, hataları ve açık riski profil başına verir. Açık risk pozisyon ve emir marjinidir."""
    pid, r = await api.running(ECHO)
    await api.step(r)
    await api.call("POST", f"/profiles/{pid}/interventions", json={
        "kind": "command", "params": {"type": "order", "order_type": "market", "symbol": SOL, "side": "long", "qty": 1}})
    await api.call("POST", f"/profiles/{pid}/interventions", json={
        "kind": "command", "params": {"type": "order", "order_type": "limit", "symbol": SOL, "side": "long",
                                       "qty": 0.2, "price": 90}})
    r.tick(api.clock.t + 60)
    ov = await api.call("GET", "/overview")
    [p] = ov["profiles"]
    st = (await api.call("GET", f"/profiles/{pid}/account"))["state"]
    margins = sum(x["margin"] for x in st["positions"]) + sum(o["margin"] for o in st["orders"])
    assert p["today"]["calls"] == 1 and p["today"]["errors"] == 0
    assert abs(p["today"]["risk"] - margins) < 1e-9 and len(st["positions"]) == len(st["orders"]) == 1
    assert p["last"]["error"] is False and len(p["curve"]) >= 1
    assert ov["collector"] == {"last_seen": None, "down": False}
    # Masadaki açık kâr pozisyon başına çekirdekten gelir, toplamı özkaynağın nakit ve marjin dışındaki kısmıdır.
    open_pnl = st["equity"] - st["cash"] - sum(x["margin"] for x in st["positions"])
    assert abs(sum(x["open_pnl"] for x in st["positions"]) - open_pnl) < 1e-9


@run
async def test_version_results_split_by_exit_time(api):
    """Sürümün sonucu yalnızca yürürlükte olduğu aralıkta kapanan işlemleri ve verilen kararları sayar.

    Profiller sayfası sürümleri bu sayılarla karşılaştırır. İşlem girişine göre atansa sürüm değişmeden açılıp
    yeni sürümde kapanan işlem eski sürüme yazılırdı.
    """
    pid, r = await api.running(ECHO)
    await api.step(r)
    open_ = {"kind": "command", "params": {"type": "order", "order_type": "market", "symbol": SOL, "side": "long", "qty": 1}}
    await api.call("POST", f"/profiles/{pid}/interventions", json=open_)
    await api.call("POST", f"/profiles/{pid}/interventions", json={"kind": "command", "params": {"type": "close", "symbol": SOL, "side": "long"}})
    await api.call("POST", f"/profiles/{pid}/interventions", json=open_)
    api.clock.t += 60
    await api.call("POST", f"/profiles/{pid}/config", json={"config": ECHO, "note": "İkinci"})
    api.clock.t += 60
    await api.call("POST", f"/profiles/{pid}/interventions", json={"kind": "command", "params": {"type": "close", "symbol": SOL, "side": "long"}})
    await api.step(r, 3600)
    first, second = (await api.call("GET", f"/profiles/{pid}"))["versions"]
    assert (first["result"]["trades"], second["result"]["trades"]) == (1, 1)
    assert first["result"]["decisions"] >= 1 and first["result"]["net"] < 0
    trades = await api.call("GET", f"/profiles/{pid}/trades")
    assert sum(t["net"] for t in trades) == first["result"]["net"] + second["result"]["net"]


@run
async def test_registry_tells_which_strategy_reads_messages(api):
    """Panel Opus'a yaz kartını ve not defteri sekmesini bu bayraklara göre gösterir. Kural stratejisinde ikisi de görünmemeli."""
    reg = {s["key"]: s["accepts_messages"] for s in (await api.call("GET", "/registry"))["strategies"]}
    assert reg["llm"] is True and reg["liquidation_reversal"] is False and reg["echo"] is True
    model = {s["key"]: s["uses_model"] for s in (await api.call("GET", "/registry"))["strategies"]}
    assert model["llm"] is True and model["liquidation_reversal"] is False


@run
async def test_malformed_command_is_a_plain_error(api):
    """Yanlış biçimli bir komut sunucu hatası değil, panelin gösterebileceği Türkçe bir 400 olmalı."""
    pid, _ = await api.running(RULE)
    out = await api.call("POST", f"/profiles/{pid}/interventions", ok=False, json={"kind": "command", "params": {
        "type": "order", "order_type": "market", "symbol": SOL, "side": "long", "qty": 1, "protection": [[0, 50]]}})
    assert "protection" in out["detail"]


@run
async def test_admin_moves_and_removes_alert(api):
    """Panel grafikte alarm çizgisini sürükleyince alarm taşınır, Alarmlar sekmesinden silince kalkar.

    Yeni seviye geçersizse eski alarm yerinde kalmalı, yoksa sürükleme hatası alarmı sessizce silerdi.
    """
    pid, r = await api.running(RULE)
    base = f"/profiles/{pid}/interventions"
    await api.call("POST", base, json={"kind": "alert", "params": {"symbol": SOL, "price": 90}})
    await api.call("POST", base, ok=False, json={"kind": "alert", "params": {"symbol": SOL, "price": 100, "cancel": 1}})
    assert [a["price"] for a in (await api.call("GET", f"/profiles/{pid}/account"))["alerts"]] == [90]
    moved = await api.call("POST", base, json={"kind": "alert", "params": {"symbol": SOL, "price": 95, "cancel": 1}})
    assert moved["result"] == "alarm #1 kaldırıldı, yerine alarm #2 kuruldu."
    assert [a["price"] for a in (await api.call("GET", f"/profiles/{pid}/account"))["alerts"]] == [95]
    await api.call("POST", base, json={"kind": "alert", "params": {"cancel": 2}})
    await api.call("POST", base, ok=False, json={"kind": "alert", "params": {"cancel": 2}})
    assert (await api.call("GET", f"/profiles/{pid}/account"))["alerts"] == []


@run
async def test_live_candle_reaches_stream_at_most_once_a_second(api):
    """Grafiğin canlı fiyatı akıştaki candle olayıdır. Saniye içindeki güncellemeler tek olaya iner, kapanış hep gider.

    Kısıtlama olmasa Binance'in saniyede birkaç güncellemesi her panel bağlantısının kuyruğunu doldururdu.
    """
    await api.running(RULE)
    q = api.core.bus.subscribe()
    c = lambda close: Candle(SOL, T // 60 * 60, 100, 101, 99, close, 1, 1, 1, 1)
    for close, closed in ((100.1, False), (100.2, False), (100.3, True)):
        await api.core.supervisor.on_candle(c(close), closed)
    api.clock.t += 1
    await api.core.supervisor.on_candle(c(100.4), False)
    got = [e for e in (q.get_nowait() for _ in range(q.qsize())) if "candle" in e]
    assert [(e["candle"].close, e["closed"]) for e in got] == [(100.1, False), (100.3, True), (100.4, False)]


@run
async def test_preview_matches_the_real_entry(api):
    """Onay kartındaki tahmin, aynı komutla açılan pozisyonla aynı olmalı. Önizleme hesaba hiçbir şey yazmamalı.

    Eski panel bu hesabı JS'te kopyalıyordu ve kurallar değişince tahmin gerçekten ayrışıyordu.
    """
    pid, r = await api.running(RULE)
    spec = {"type": "order", "order_type": "market", "symbol": SOL, "side": "short", "qty": 1,
            "protection": [{"type": "stop", "price": 105}]}
    pre = await api.call("POST", f"/profiles/{pid}/preview", json={"params": spec})
    acc = await api.call("GET", f"/profiles/{pid}/account")
    assert acc["state"]["positions"] == [] and acc["state"]["cash"] == 100
    await api.call("POST", f"/profiles/{pid}/interventions", json={"kind": "command", "params": spec})
    acc = await api.call("GET", f"/profiles/{pid}/account")
    [p] = acc["state"]["positions"]
    assert (pre["entry"], pre["qty"], pre["margin"], pre["fee"], pre["liq_price"]) == \
        (p["entry_price"], p["qty"], p["margin"], p["fee"], p["liq_price"])
    assert abs(pre["cash_after"] - acc["state"]["available"]) < 1e-9
    bad = await api.call("POST", f"/profiles/{pid}/preview", ok=False,
                         json={"params": {**spec, "protection": [{"type": "stop", "price": 90}]}})
    assert "hemen tetiklenirdi" in bad["detail"]


@run
async def test_merging_entry_preview_goes_through_the_gate(api):
    """Aynı yöndeki ikinci giriş pozisyonla birleşir. Önizleme birleşmiş pozisyonun ortalama girişini ve
    likidasyonunu gerçek girişle aynı vermeli. Emir iptali gibi önizlemesi olmayan komut anlaşılır bir hatayla
    reddedilmeli."""
    pid, r = await api.running(RULE)
    entry = {"type": "order", "order_type": "market", "symbol": SOL, "side": "long", "qty": 1}
    await api.call("POST", f"/profiles/{pid}/interventions", json={"kind": "command", "params": entry})
    spec = {**entry, "qty": 2}
    pre = await api.call("POST", f"/profiles/{pid}/preview", json={"params": spec})
    await api.call("POST", f"/profiles/{pid}/interventions", json={"kind": "command", "params": spec})
    acc = await api.call("GET", f"/profiles/{pid}/account")
    [p] = acc["state"]["positions"]
    assert (pre["avg_entry"], pre["liq_price"]) == (p["entry_price"], p["liq_price"])
    assert pre["leverage"] == round(p["entry_price"] * p["qty"] / p["margin"], 2)
    assert abs(pre["cash_after"] - acc["state"]["available"]) < 1e-9
    bad = await api.call("POST", f"/profiles/{pid}/preview", ok=False,
                         json={"params": {"type": "cancel_order", "number": 1, "symbol": SOL, "side": "long"}})
    assert "Önizleme" in bad["detail"]


@run
async def test_close_preview_matches_the_real_close(api):
    """Kapatma kartı çıkışı, kârı, komisyonu ve neti bu uçtan alır. Net, kapanınca İşlemler sekmesinde görünen net'le
    aynı olmalı, nakit de tahmin edilen nakit olmalı. Önizleme pozisyona dokunmamalı."""
    pid, r = await api.running(RULE)
    entry = {"type": "order", "order_type": "market", "symbol": SOL, "side": "long", "qty": 1}
    await api.call("POST", f"/profiles/{pid}/interventions", json={"kind": "command", "params": entry})
    spec = {"type": "close", "symbol": SOL, "side": "long", "qty": 0.5}
    pre = await api.call("POST", f"/profiles/{pid}/preview", json={"params": spec})
    before = (await api.call("GET", f"/profiles/{pid}/account"))["state"]["positions"][0]["qty"]
    await api.call("POST", f"/profiles/{pid}/interventions", json={"kind": "command", "params": spec})
    acc = await api.call("GET", f"/profiles/{pid}/account")
    [t] = await api.call("GET", f"/profiles/{pid}/trades")
    assert (pre["exit"], pre["qty"]) == (t["exit_price"], t["qty"])
    assert abs(pre["net"] - t["net"]) < 1e-9 and abs(pre["cash_after"] - acc["state"]["cash"]) < 1e-9
    assert pre["remaining"] == acc["state"]["positions"][0]["qty"] == round(before - t["qty"], 10)


@run
async def test_chain_sets_leverage_then_enters(api):
    """Panel kaldıracı girişle birlikte tek onayda gönderir. Önizleme yeni kaldıraçla hesaplamalı ama paritenin
    ayarını değiştirmemeli, gerçek giriş de önizlemedeki marjinle açılmalı.

    Önizleme kaldıracı yok sayarsa Onay kartı eski kaldıracın marjinini ve likidasyonunu gösterirdi. Açık pozisyon
    varken kaldıracı düşüren zincir önizlemede de reddedilmeli. Hesap ucu ayarlanmamış paritenin varsayılanını da
    vermeli, işlem kaydı kaldıracını taşımalı, arayüzler bu kuralları kopyalamasın.
    """
    pid, _ = await api.running(RULE)
    entry = {"type": "order", "order_type": "market", "symbol": SOL, "side": "long", "qty": 1}
    chain = {"chain": [{"type": "leverage", "symbol": SOL, "leverage": 5}, entry]}
    pre = await api.call("POST", f"/profiles/{pid}/preview", json={"params": chain})
    plain = await api.call("POST", f"/profiles/{pid}/preview", json={"params": entry})
    assert pre["margin"] > plain["margin"] and pre["liq_price"] < plain["liq_price"]
    assert (await api.call("GET", f"/profiles/{pid}/account"))["state"]["leverages"] == {SOL: 20}
    iv = await api.call("POST", f"/profiles/{pid}/interventions", json={"kind": "command", "params": chain})
    state = (await api.call("GET", f"/profiles/{pid}/account"))["state"]
    [p] = state["positions"]
    assert state["leverages"] == {SOL: 5} and (pre["margin"], pre["liq_price"]) == (p["margin"], p["liq_price"])
    assert "kaldıra" in iv["result"] and "açıldı" in iv["result"]
    lower = {"chain": [{"type": "leverage", "symbol": SOL, "leverage": 2}, entry]}
    bad = await api.call("POST", f"/profiles/{pid}/preview", ok=False, json={"params": lower})
    assert "düşürülemez" in bad["detail"]
    bad = await api.call("POST", f"/profiles/{pid}/preview", ok=False,
                         json={"params": {"chain": [entry, {"type": "leverage", "symbol": SOL, "leverage": 5}]}})
    assert "Önizleme" in bad["detail"]
    await api.call("POST", f"/profiles/{pid}/interventions", json={"kind": "command", "params": {
        "type": "close", "symbol": SOL, "side": "long"}})
    [t] = await api.call("GET", f"/profiles/{pid}/trades")
    assert t["leverage"] == p["entry_price"] * p["qty"] / p["margin"]


@run
async def test_chain_stops_at_first_rejection(api):
    """Zincirde bir adım reddedilince sonrakiler gönderilmez.

    İlk adım reddedilirse hesap değişmemeli ve müdahale kaydedilmemeli. Sonraki bir adım reddedilirse önceki adım
    hesabı değiştirmiştir, müdahale Opus öğrensin diye kaydedilmeli ve yöneticiye yine hata dönmeli. Koruma taşıma
    bu yüzden önce yeni korumayı kurar: yenisi reddedilirse eskisi iptal edilmez, pozisyon korumasız kalmaz.
    """
    pid, _ = await api.running(RULE)
    base = f"/profiles/{pid}/interventions"
    await api.call("POST", base, json={"kind": "command", "params": {
        "type": "order", "order_type": "market", "symbol": SOL, "side": "long", "qty": 1,
        "protection": [{"type": "stop", "price": 90}]}})
    [sl] = (await api.call("GET", f"/profiles/{pid}/account"))["state"]["orders"]
    move = lambda price: {"chain": [
        {"type": "order", "order_type": "stop", "symbol": SOL, "side": "long", "reduce": True, "price": price},
        {"type": "cancel_order", "number": sl["number"], "symbol": SOL, "side": "long"}]}
    count = len(await api.call("GET", base))
    bad = await api.call("POST", base, ok=False, json={"kind": "command", "params": move(110)})
    assert "Önceki adımlar" not in bad["detail"] and len(await api.call("GET", base)) == count
    assert [o["price"] for o in (await api.call("GET", f"/profiles/{pid}/account"))["state"]["orders"]] == [90]
    moved = await api.call("POST", base, json={"kind": "command", "params": move(92)})
    assert [o["price"] for o in (await api.call("GET", f"/profiles/{pid}/account"))["state"]["orders"]] == [92]
    assert "reddedildi" not in moved["result"]
    broken = {"chain": [{"type": "order", "order_type": "stop", "symbol": SOL, "side": "long", "reduce": True,
                         "price": 94},
                        {"type": "cancel_order", "number": 99, "symbol": SOL, "side": "long"},
                        {"type": "close", "symbol": SOL, "side": "long"}]}
    bad = await api.call("POST", base, ok=False, json={"kind": "command", "params": broken})
    assert "2. adımı reddedildi" in bad["detail"] and "Önceki adımlar uygulandı" in bad["detail"]
    [iv] = (await api.call("GET", base))[:1]
    assert "2. adımı reddedildi" in iv["result"] and "Sonraki adımlar uygulanmadı" in iv["result"]
    state = (await api.call("GET", f"/profiles/{pid}/account"))["state"]
    assert len(state["positions"]) == 1 and sorted(o["price"] for o in state["orders"]) == [92, 94]
    empty = await api.call("POST", base, ok=False, json={"kind": "command", "params": {"chain": []}})
    assert "chain" in empty["detail"]


@run
async def test_llm_registry_lists_what_opus_really_gets(api):
    """Stratejiler sayfası araçları ve eylemleri buradan gösterir. Liste Opus'a gerçekten verilenle aynı olmalı.

    Araç sunucusuna yeni bir araç eklenince ya da karar şemasına yeni bir eylem girince sayfa elle güncellenmeden görmeli.
    """
    from opus_trade.strategy.llm.actions import ACTIONS
    from opus_trade.tools.market import server
    out = await api.call("GET", "/registry/llm")
    assert [t["name"] for t in out["tools"]] == [t.name for t in await server.list_tools()]
    assert [a["type"] for a in out["actions"]] == [a[0] for a in ACTIONS]
    mumlar = next(t for t in out["tools"] if t["name"] == "mumlar")
    assert {"name": "sembol", "type": "string", "default": None, "required": True} in mumlar["params"]
    assert out["builtin"] == ["WebSearch", "WebFetch"]
    assert [a["name"] for a in out["agents"]] == ["opus", "sonnet", "haiku"]
    assert out["agents"][2]["tools"] == ["WebSearch", "WebFetch", "piyasa"]


@run
async def test_system_reports_services_errors_and_merged_events(api):
    """Sistem uçu akışın, toplayıcının ve dinleyicilerin durumunu verir. Karar hataları ayrı listededir,
    profilsiz bildirimler ve diğer sistem olayları zamana göre tek listede birleşir."""
    api.core.state.system_event(1, T - 30, "decide_error", "Karar alınamadı.", {"error": "zaman aşımı"})
    api.core.state.system_event(None, T - 20, "migration", "Taşıma uygulandı.")
    api.core.notifications.add(T - 10, None, "critical", "stream_down", "Piyasa akışı koptu")
    out = await api.call("GET", "/system")
    assert [e["detail"]["error"] for e in out["errors"]] == ["zaman aşımı"]
    assert [(e["kind"], e["level"]) for e in out["events"]] == [("stream_down", "critical"), ("migration", "info")]
    assert out["stream"] == {"active": False, "down": False, "last_candle": T}
    assert out["collector"] == {"last_seen": None, "down": False}
    assert out["listeners"] == api.core.bus.listeners >= 1


@run
async def test_usage_sums_calls_from_journal(api):
    """Tüketim günlüğe yazılan usage ve cost_usd alanlarından gelir. Gün sayısı sınırlanır, aşırı istek büyük sorgu açmaz."""
    pid, r = await api.running(ECHO)
    record = {"usage": {"usage": {"input_tokens": 1200, "output_tokens": 80}}, "cost_usd": 0.25, "duration_s": 12}
    api.core.journal.add(pid, r.version.id, T - 60, [], [], record, None, None)
    out = await api.call("GET", "/usage", params={"days": 999})
    assert len(out["days"]) == 60
    w = out["window"]["profiles"][str(pid)]
    assert (w["calls"], w["input"], w["output"], w["cost_usd"]) == (1, 1200, 80, 0.25)


def test_startup_log_counts_profiles_after_they_are_built(tmp_path, caplog):
    """Açılış kaydındaki profil sayısı profiller kurulduktan sonra yazılır, çalışan profil 0 sayılmaz."""
    async def go():
        first = Api(tmp_path)
        p = await first.call("POST", "/profiles", json={"name": "Test", "short": "T", "config": ECHO})
        await first.call("POST", f"/profiles/{p['id']}/start")
        again = Api(tmp_path)
        with caplog.at_level("INFO", logger="opus_trade.runtime.supervisor"):
            task = asyncio.create_task(again.core.supervisor.run())
            await asyncio.sleep(0.05)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(go())
    assert "1 profil kuruldu." in caplog.messages


BOOK = {"plan": {"metin": "62615 altında short ara.", "bozulma": "62615 üstü", "gecerlilik": "23.11.2026 18:00"},
        "seviyeler_notu": "Direnç 57.600", "gundem": "Salı FOMC."}


def test_admin_edits_notebook_and_strategy_reads_it_next(tmp_path, monkeypatch):
    """Panelden düzeltilen not defteri bir sonraki kararda stratejiye gider, eski biçimdeki metin olayda kalır.
    Sistemin taşıdığı tepe ve dipler panelden yazılmaz, korunur."""
    monkeypatch.setattr(Echo, "uses_model", True)

    async def go(api):
        pid, r = await api.running(ECHO)
        api.core.journal.set_memory(pid, {"notebook": "DÖNEM: eski\nDirenç 57.600"})
        await api.call("POST", f"/profiles/{pid}/memory", json={"notebook": BOOK, "before": "DÖNEM: eski\nDirenç 57.600"})
        await api.call("POST", f"/profiles/{pid}/interventions", json={"kind": "look_now"})
        await api.step(r, 1)
        book = Echo.seen[-1].memory["notebook"]
        assert book["plan"] == BOOK["plan"] and book["yapi"] == {"seviyeler_notu": "Direnç 57.600", "rakamlar": {}}
        ev = [e for e in api.core.state.system_events(pid) if e["kind"] == "memory_edit"]
        assert len(ev) == 1 and "DÖNEM: eski" in ev[0]["detail"]
        too_long = {**BOOK, "gundem": "x" * 501}
        err = await api.call("POST", f"/profiles/{pid}/memory", ok=False, json={"notebook": too_long, "before": book})
        assert "gündem 501 karakter" in err["detail"]
    asyncio.run(go(Api(tmp_path)))


def test_notebook_edit_is_refused_while_deciding_or_without_model(tmp_path, monkeypatch):
    """Karar sürerken yazma reddedilir, yoksa çağrının sonu düzeltmeyi siler. Kural profilinde defter yoktur."""
    async def go(api):
        rule = await api.call("POST", "/profiles", json={"name": "Kural", "short": "K", "config": RULE})
        err = await api.call("POST", f"/profiles/{rule['id']}/memory", ok=False, json={"notebook": BOOK, "before": None})
        assert "not defteri tutmaz" in err["detail"]
        monkeypatch.setattr(Echo, "uses_model", True)
        pid, r = await api.running(ECHO)
        api.core.journal.set_memory(pid, {"notebook": "Opus'un"})
        r.busy = True
        err = await api.call("POST", f"/profiles/{pid}/memory", ok=False,
                             json={"notebook": BOOK, "before": "Opus'un"})
        assert "karar veriyor" in err["detail"] and api.core.journal.memory(pid) == {"notebook": "Opus'un"}
    asyncio.run(go(Api(tmp_path)))


def test_notebook_edit_is_refused_when_a_decision_rewrote_it(tmp_path, monkeypatch):
    """Panel düzenlerken gelen karar defteri yeniden yazdıysa eski metinle yapılan kayıt Opus'un yeni defterini ezmemeli."""
    monkeypatch.setattr(Echo, "uses_model", True)

    async def go(api):
        pid, _ = await api.running(ECHO)
        api.core.journal.set_memory(pid, {"notebook": "yeni karar"})
        err = await api.call("POST", f"/profiles/{pid}/memory", ok=False,
                             json={"notebook": BOOK, "before": "eski"})
        assert "değişti" in err["detail"] and api.core.journal.memory(pid) == {"notebook": "yeni karar"}
    asyncio.run(go(Api(tmp_path)))


@run
async def test_account_carries_the_position_detail(api):
    """Masadaki pozisyon detayı hesap ucundan gelir: kapanan parçaların net'i ve sayısı, büyüklük ve her koruma
    emrinin tahmini. Arayüz bunları kendisi hesaplamaz, giriş emri tahmine girmez."""
    pid, r = await api.running(RULE)
    entry = {"type": "order", "order_type": "market", "symbol": SOL, "side": "long", "qty": 1,
             "protection": [{"type": "stop", "price": 97}]}
    await api.call("POST", f"/profiles/{pid}/interventions", json={"kind": "command", "params": entry})
    close = {"type": "close", "symbol": SOL, "side": "long", "qty": 0.5}
    await api.call("POST", f"/profiles/{pid}/interventions", json={"kind": "command", "params": close})
    await api.call("POST", f"/profiles/{pid}/interventions", json={"kind": "command", "params": {
        "type": "order", "order_type": "limit", "symbol": SOL, "side": "long", "qty": 0.2, "price": 80}})
    acc = await api.call("GET", f"/profiles/{pid}/account")
    [t] = await api.call("GET", f"/profiles/{pid}/trades")
    [p] = acc["state"]["positions"]
    sl = next(o for o in acc["state"]["orders"] if o["reduce"])
    assert (p["realized_count"], p["realized"]) == (1, t["net"])
    assert p["notional"] == p["qty"] * acc["prices"][SOL]
    [x] = p["outlook"]
    assert (x["order_id"], x["level"], x["qty"]) == (sl["id"], 97, p["qty"]) and x["net"] < 0
    assert p["by_admin"] is True and p["notes"] == []
    limit = next(o for o in acc["state"]["orders"] if not o["reduce"])
    assert limit["by_admin"] is True and limit["note"] is None
