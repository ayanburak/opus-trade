/**
 * file: orders.test.ts
 * importable: yok
 *
 * Koruma emirlerinin metnini sınar. Burada bir hata pozisyonun satırında
 * başka pozisyonun emrini ya da yanlış miktarı gösterir.
 *
 * description: shared/orders.ts testleri.
 */

import { describe, expect, it } from "vitest";
import type { Order, Position } from "./types";
import { exitText, exitsOf, requestText } from "./orders";

describe("koruma emirleri", () => {
  const pos = { id: 1, symbol: "BTCUSDT", side: "long" } as Position;
  const o = (number: number, type: string, extra: object = {}) =>
    ({ id: number, number, symbol: "BTCUSDT", side: "long", type, reduce: true, price: 60000, qty: null, trail_pct: null,
       ...extra }) as unknown as Order;

  it("pozisyonun satırına yalnızca kendi yönünün ve paritesinin kapatan emirleri gelir, TP'ler ayrı durur", () => {
    const orders = [o(4, "take_profit"), o(2, "stop"), o(3, "trailing_stop"), o(5, "stop", { side: "short" }),
                    o(6, "stop", { symbol: "ETHUSDT" }), o(7, "limit", { reduce: false })];
    const { tp, stop } = exitsOf(orders, pos);
    expect([tp.map((x) => x.number), stop.map((x) => x.number)]).toEqual([[4], [2, 3]]);
  });

  it("kapatan emir numarası, fiyatı ya da iz yüzdesi ve miktarıyla yazılır, miktarsızı tümüdür", () => {
    expect(exitText(o(4, "take_profit", { qty: 0.25 }))).toBe("#4 60.000,0 · 0,25");
    expect(exitText(o(3, "trailing_stop", { price: null, trail_pct: 1.5 }))).toBe("#3 iz %1,5 · tümü");
  });

  it("bekleyen girişin koruma isteği türünün adıyla yazılır", () => {
    expect(requestText([{ type: "take_profit", price: 70000, trail_pct: null, qty: 0.1 },
                        { type: "stop", price: 58000, trail_pct: null, qty: null }])).toBe("TP 70.000,0 · 0,1, SL 58.000,0");
    expect(requestText([])).toBe("—");
  });
});
