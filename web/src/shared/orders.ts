/**
 * file: orders.ts
 * importable: exitsOf, exitText, requestText
 *
 * Emirlerin tablolardaki metinleri. Pozisyonun kapatan emirleri, yani TP,
 * SL ve iz, pozisyonun satırında numarası ve miktarıyla durur. Bekleyen
 * girişin dolunca kuracağı korumalar da türünün adıyla tek satırda yazılır.
 * Herkese açık sayfa ve panel aynı metni gösterir.
 *
 * description: Koruma emirlerinin ve koruma isteğinin ortak metinleri.
 */

import { num, qty } from "./format";
import { orderName } from "./labels";
import type { Order, Position, Protection } from "./types";

/** Pozisyonun kapatan emirleri, TP'ler ve stoplar ayrı, numara sırasıyla. */
export function exitsOf(orders: Order[], p: Position): { tp: Order[]; stop: Order[] } {
  const mine = orders.filter((o) => o.reduce && o.symbol === p.symbol && o.side === p.side)
    .sort((a, b) => a.number - b.number);
  return { tp: mine.filter((o) => o.type === "take_profit"), stop: mine.filter((o) => o.type !== "take_profit") };
}

/** Kapatan emrin tablodaki kısa metni: numarası, fiyatı ya da iz yüzdesi ve miktarı. */
export function exitText(o: Order): string {
  const at = o.type === "trailing_stop" ? `iz %${num(o.trail_pct, 1)}` : num(o.price);
  return `#${o.number} ${at} · ${o.qty == null ? "tümü" : qty(o.qty)}`;
}

/** Bekleyen girişin dolunca kuracağı korumaların kısa metni. */
export function requestText(ps: Protection[]): string {
  if (!ps.length) return "—";
  return ps.map((x) => {
    const name = orderName({ type: x.type, reduce: true });
    const at = x.type === "trailing_stop" ? `%${num(x.trail_pct, 1)}` : num(x.price);
    return `${name} ${at}${x.qty == null ? "" : ` · ${qty(x.qty)}`}`;
  }).join(", ");
}
