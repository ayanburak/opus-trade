/**
 * file: model.ts
 * importable: Leg, Detail, detailOf, liqDistance
 *
 * Pozisyon detayının verisi. Tutarların hepsi çekirdekten gelir: kapanan
 * parçaların net'i realized, her koruma emrinin tetiklenirse bırakacağı net
 * outlook'tadır. Burada yalnızca bunlar TP ve stop diye ayrılır ve toplanır,
 * hesap kuralı yoktur. Senaryo pozisyonun bütün net'idir, kapanmış parçalar
 * dahil.
 *
 * description: Pozisyon detayının TP, stop ve senaryo satırları.
 */

import type { Order, Position } from "../types";

/** Senaryonun bir adımı. order null ise adım likidasyondur. */
export interface Leg { order: Order | null; level: number; qty: number; net: number }

/**
 * TP'ler ve stoplar ayrı, fiyata yakından uzağa. ifTp ve ifStop o yoldan çıkılırsa pozisyonun bütün net'idir, o
 * yolda emir yoksa null. bareTp ve bareStop o yolun kapatmadığı, açıkta kalan miktardır.
 */
export interface Detail {
  tps: Leg[]; stops: Leg[]; ifTp: number | null; ifStop: number | null; bareTp: number; bareStop: number;
}

// Kayan nokta artığı açıkta kalan miktar sayılmasın.
const EPS = 1e-9;

export function detailOf(p: Position, orders: Order[]): Detail {
  const byId = new Map(orders.map((o) => [o.id, o]));
  const legs = p.outlook.map((x) => ({ order: x.order_id == null ? null : byId.get(x.order_id) ?? null, level: x.level,
                                       qty: x.qty, net: x.net }));
  const tps = legs.filter((l) => l.order?.type === "take_profit");
  const stops = legs.filter((l) => l.order?.type !== "take_profit");
  const sum = (ls: Leg[], k: "qty" | "net") => ls.reduce((s, l) => s + l[k], 0);
  const bare = (ls: Leg[]) => (p.qty - sum(ls, "qty") > EPS ? p.qty - sum(ls, "qty") : 0);
  return {
    tps, stops,
    ifTp: tps.length ? p.realized + sum(tps, "net") : null,
    ifStop: stops.length ? p.realized + sum(stops, "net") : null,
    bareTp: bare(tps), bareStop: bare(stops),
  };
}

/** Son fiyatın likidasyona yüzde uzaklığı, fiyat yoksa null. */
export function liqDistance(p: Position, price: number | undefined): number | null {
  return price ? (Math.abs(price - p.liq_price) / price) * 100 : null;
}
