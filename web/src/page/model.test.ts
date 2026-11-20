/**
 * file: model.test.ts
 * importable: yok
 *
 * Sayfanın hesaplarını ve metinlerini sınar. Burada bir hata ziyaretçiye
 * yanlış getiri, yanlış günün saati ya da olmamış bir işlem gösterir.
 *
 * description: page/model.ts testleri.
 */

import { describe, expect, it } from "vitest";
import type { Trade } from "../shared/types";
import type { Account, Entry, Profile } from "../api/types";
import { markers, status, summary, timeline, toolCounts, wakeText } from "./model";

const NOW = 1_795_149_360;
const PROFILE: Profile = { id: 1, name: "Opus", short: "op", strategy: "llm", state: "running", version: 2, equity: 110,
  start_balance: 100, positions: 0, orders: 0, busy: false, next_wake: null, symbols: ["BTCUSDT"] };
const trade = (n: number, reason: string, net: number, exit: number): Trade => ({ id: n, number: n, symbol: "BTCUSDT",
  side: "long", leverage: 10, qty: 1, margin: 10, entry_price: 1, entry_time: exit - 600, exit_price: 2, exit_time: exit,
  reason, pnl: net, fee: 0, funding: 0, net });
const entry = (id: number, time: number): Entry => ({ id, time, version_id: 1, next_wake: null, wakes: [], results: [],
  record: {}, error: null });

describe("üst satır", () => {
  it("getiri hesabın özkaynağından, açık K/Z açık pozisyonlardan gelir", () => {
    const acc = { state: { account_id: 1, cash: 90, equity: 120, positions: [{ open_pnl: 3 }, { open_pnl: -1 }], orders: [] },
                  prices: {}, triggers: [], alerts: [] } as unknown as Account;
    const s = summary(PROFILE, acc, [trade(1, "tp", 2, NOW), trade(2, "sl", -1, NOW)]);
    expect({ ...s, ret: 0 }).toEqual({ equity: 120, ret: 0, open: 2, trades: 2, wins: 1 });
    expect(s.ret).toBeCloseTo(20);
  });

  it("açık pozisyon yoksa açık K/Z sıfır değil boştur", () => {
    expect(summary(PROFILE, null, []).open).toBeNull();
  });
});

describe("durum satırı", () => {
  it("sonraki çağrı başka güne düşüyorsa gün de yazılır", () => {
    expect(status({ ...PROFILE, next_wake: NOW + 17 * 3600 }, false, 0, NOW).text).toBe("Sonraki çağrı yarın 00:36");
    expect(status({ ...PROFILE, next_wake: NOW + 3600 }, false, 0, NOW).text).toBe("Sonraki çağrı bugün 08:36");
  });

  it("düşünürken araç sayısı, çalışmıyorsa durumu yazılır", () => {
    expect(status(PROFILE, true, 3, NOW)).toEqual({ text: "Düşünüyor · 3 araç", tone: "busy" });
    expect(status({ ...PROFILE, state: "paused" }, false, 0, NOW).text).toBe("Duraklatıldı");
  });
});

describe("günlük", () => {
  it("uyandırmanın verisi okunur cümleye çevrilir", () => {
    const ev = JSON.stringify({ type: "event", kind: "closed", reason: "tp", number: 4, symbol: "BTCUSDT", side: "long", price: 62000 });
    expect(wakeText({ kind: "account", time: 0, data: ev })).toEqual({ label: "hesap olayı", detail: "Pozisyon kapandı, TP · #4 BTCUSDT long · 62.000,0" });
    expect(wakeText({ kind: "timer", time: 0, data: null })).toEqual({ label: "zamanlayıcı", detail: "" });
    expect(wakeText({ kind: "timer", time: 0, data: "{bozuk" }).detail).toBe("");
  });

  it("araçlar türüne göre sayılır, alt görevin adımları ayrıca sayılmaz", () => {
    const tools = [{ name: "piyasa.mumlar" }, { name: "piyasa.seviyeler" }, { name: "WebSearch" }, { name: "Agent" },
                   { name: "WebFetch", sub: true }];
    expect(toolCounts(tools)).toBe("2 piyasa verisi · 1 arama · 1 araştırma görevi");
    expect(toolCounts([])).toBe("");
  });

  it("kararla kapanan işlem ikinci kez görünmez, TP ve SL ayrı satır olur, yüklenen kararlardan eskisi gelmez", () => {
    const rows = timeline([entry(2, NOW), entry(1, NOW - 1000)],
                          [trade(1, "strategy", 1, NOW - 10), trade(2, "tp", 1, NOW - 20), trade(3, "sl", -1, NOW - 5000)]);
    expect(rows.map((r) => (r.kind === "entry" ? `k${r.entry.id}` : `i${r.trade.number}`))).toEqual(["k2", "i2", "k1"]);
  });
});

describe("grafik işaretleri", () => {
  it("yalnızca seçili paritenin ve grafiğin başlangıcından yeni işaretleri çizilir", () => {
    const ms = markers([trade(1, "sl", -2, NOW), { ...trade(2, "tp", 1, NOW), symbol: "ETHUSDT" }], "BTCUSDT", NOW - 100);
    expect(ms).toEqual([{ time: NOW, position: "aboveBar", shape: "circle", tone: "down", text: "#1 SL −2,00" }]);
  });
});
