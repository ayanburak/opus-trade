/**
 * file: layers.test.ts
 * importable: yok
 *
 * Grafiğin çizgilerini, işaretlerini ve filtre seçimini sınar. Burada bir
 * hata grafikte olmayan bir seviye, gizlenmiş bir katmanın çizgisi ya da
 * yanlış işlem işareti gösterir.
 *
 * description: shared/pricechart/layers.ts testleri.
 */

import { describe, expect, it } from "vitest";
import type { Alert, Order, Position, Trade } from "../types";
import { DRAGGABLE, LAYERS, dimColor, markers, priceLines, readHidden, type Layer } from "./layers";

const NOW = 1_795_149_360;
const trade = (n: number, reason: string, net: number, exit: number): Trade => ({ id: n, number: n, symbol: "BTCUSDT",
  side: "long", leverage: 10, qty: 1, margin: 10, entry_price: 1, entry_time: exit - 600, exit_price: 2, exit_time: exit,
  reason, pnl: net, fee: 0, funding: 0, net });

describe("grafik işaretleri", () => {
  it("yalnızca seçili paritenin ve grafiğin başlangıcından yeni işaretleri çizilir", () => {
    const ms = markers([trade(1, "sl", -2, NOW), { ...trade(2, "tp", 1, NOW), symbol: "ETHUSDT" }], "BTCUSDT", NOW - 100);
    expect(ms).toEqual([{ time: NOW, position: "aboveBar", shape: "circle", tone: "down", text: "#1 SL −2,00" }]);
  });
});

describe("grafik katmanları", () => {
  const pos = { id: 1, number: 1, symbol: "BTCUSDT", side: "long", entry_price: 100, tp_levels: [{ price: 110, pct: 50 }], sl: 95,
                liq_price: 80 } as unknown as Position;
  const order = { id: 2, number: 2, symbol: "BTCUSDT", side: "short", trigger: 120 } as unknown as Order;
  const alert = { number: 3, symbol: "BTCUSDT", price: 130 } as unknown as Alert;
  const other = { ...alert, symbol: "ETHUSDT" };

  it("her katman kendi çizgisini verir, başka paritenin çizgisi çizilmez", () => {
    const lines = priceLines("BTCUSDT", [pos], [order], [alert, other], new Set());
    expect(lines.map((l) => [l.layer, l.price])).toEqual([["entry", 100], ["tp", 110], ["sl", 95], ["liq", 80], ["order", 120], ["alarm", 130]]);
  });

  it("giriş ve emir çizgisi yönünü renginden belli eder, diğer çizgiler katmanın rengini alır", () => {
    const short = { ...pos, id: 4, side: "short" } as Position;
    const lines = priceLines("BTCUSDT", [pos, short], [order, { ...order, id: 5, side: "long" }], [alert], new Set());
    expect(lines.filter((l) => l.layer === "entry").map((l) => l.color)).toEqual(["--long", "--short"]);
    expect(lines.filter((l) => l.layer === "order").map((l) => l.color)).toEqual(["--short", "--long"]);
    expect(lines.filter((l) => l.layer !== "entry" && l.layer !== "order").every((l) => l.color === undefined)).toBe(true);
  });

  it("çizgi kaynağını taşır, böylece sürüklenen çizgi doğru pozisyona, emre ya da alarma bağlanır", () => {
    const lines = priceLines("BTCUSDT", [pos], [order], [alert], new Set());
    const tp = lines.find((l) => l.layer === "tp")!;
    expect([tp.position, tp.tp]).toEqual([pos, 0]);
    const two = { ...pos, tp_levels: [{ price: 110, pct: 50 }, { price: 120, pct: 50 }] } as Position;
    expect(priceLines("BTCUSDT", [two], [], [], new Set()).filter((l) => l.layer === "tp").map((l) => l.tp)).toEqual([0, 1]);
    expect(lines.find((l) => l.layer === "order")!.order).toBe(order);
    expect(lines.find((l) => l.layer === "alarm")!.alert).toBe(alert);
    expect(new Set(lines.map((l) => l.key)).size).toBe(lines.length);
  });

  it("yalnızca yöneticinin taşıyabileceği seviyeler sürüklenir", () => {
    expect([...DRAGGABLE].sort()).toEqual(["alarm", "order", "sl", "tp"]);
  });

  it("gizlenen katmanın çizgisi çıkar, diğerleri kalır", () => {
    const lines = priceLines("BTCUSDT", [pos], [order], [alert], new Set<Layer>(["tp", "alarm"]));
    expect(lines.map((l) => l.layer)).toEqual(["entry", "sl", "liq", "order"]);
  });

  it("ilk ziyarette işlem işaretleri gizlidir, saklanan seçimden yalnızca bilinen katmanlar okunur", () => {
    expect([...readHidden('["sl","yok","marks"]')]).toEqual(["sl", "marks"]);
    expect(readHidden("{bozuk").size).toBe(0);
    expect(readHidden('{"sl":1}').size).toBe(0);
    expect([...readHidden(null)]).toEqual(["marks"]);
    expect(readHidden("[]").size).toBe(0);
  });

  it("çizgi rengi arka plana verilen oranda karışır, sonuç saydam olmayan bir renktir", () => {
    expect(dimColor("#ffffff", "#000000", 0.3)).toBe("#b3b3b3");
    expect(dimColor("#7FD3A0", "#161A21", 0)).toBe("#7fd3a0");
  });

  it("her katmanın açıklama satırında ve menüde tek bir yeri vardır", () => {
    expect(new Set(LAYERS.map((l) => l.key)).size).toBe(LAYERS.length);
  });
});
