/**
 * file: model.test.ts
 * importable: yok
 *
 * Pozisyon detayının gruplamasını ve toplamlarını sınar. Burada bir hata
 * TP'nin tutarını stopa yazar ya da senaryoda kapanmış parçaları unutur.
 *
 * description: shared/position/model.ts testleri.
 */

import { describe, expect, it } from "vitest";
import { detailOf, liqDistance } from "./model";
import type { Order, Position } from "../types";

const order = (id: number, type: string) => ({ id, number: id, type, reduce: true }) as unknown as Order;
const orders = [order(1, "take_profit"), order(2, "take_profit"), order(3, "stop"), order(4, "trailing_stop")];
const pos = (outlook: Position["outlook"], realized = 0) =>
  ({ id: 9, qty: 3, liq_price: 95, realized, outlook }) as unknown as Position;

describe("pozisyon detayı", () => {
  it("TP'ler ve stoplar ayrılır, senaryo kapanmış parçaları da sayar", () => {
    const d = detailOf(pos([
      { order_id: 1, level: 110, qty: 1, net: 10 }, { order_id: 2, level: 120, qty: 2, net: 40 },
      { order_id: 4, level: 98, qty: 1, net: -2 }, { order_id: 3, level: 97, qty: 2, net: -6 },
    ], 5), orders);
    expect([d.tps.map((l) => l.order?.id), d.stops.map((l) => l.order?.id)]).toEqual([[1, 2], [4, 3]]);
    expect([d.ifTp, d.ifStop, d.bareTp, d.bareStop]).toEqual([55, -3, 0, 0]);
  });

  it("likidasyon adımı stopların arasında emirsiz durur, kapatılmayan miktar açıkta sayılır", () => {
    const d = detailOf(pos([{ order_id: null, level: 95, qty: 3, net: -15 }, { order_id: 3, level: 90, qty: 0, net: 0 },
                            { order_id: 1, level: 110, qty: 1, net: 10 }]), orders);
    expect(d.stops.map((l) => l.order?.id ?? null)).toEqual([null, 3]);
    expect([d.ifStop, d.bareStop, d.ifTp, d.bareTp]).toEqual([-15, 0, 10, 2]);
  });

  it("emri olmayan yolun senaryosu yoktur", () => {
    const d = detailOf(pos([]), orders);
    expect([d.ifTp, d.ifStop, d.bareTp]).toEqual([null, null, 3]);
  });

  it("likidasyona uzaklık son fiyata göre yüzdedir", () => {
    expect(liqDistance(pos([]), 100)).toBeCloseTo(5);
    expect(liqDistance(pos([]), undefined)).toBeNull();
  });
});
