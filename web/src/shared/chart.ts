/**
 * file: chart.ts
 * importable: INTERVALS, SHIFT, Bar, intervalSeconds, digits, roundPrice, mergeBar, chartTime
 *
 * Fiyat grafiğinin saf yardımcıları. Mum aralıkları ve saniye karşılıkları,
 * fiyatın kaç haneyle gösterildiği ve canlı akıştaki dakikalık mumun
 * grafiğin seçili aralığındaki son çubuğa işlenmesi.
 *
 * description: Grafiğin aralık, hane ve mum birleştirme hesapları.
 */

// NOTE: Lightweight Charts saatleri UTC gösterir. Saatler Türkiye saatiyle
// görünsün diye zaman damgaları SHIFT kadar kaydırılır. Türkiye yaz saati
// uygulamadığı için kayma sabittir.

import type { Candle } from "./types";

/** Grafikteki aralıklar ve düğmedeki kısa adları. */
export const INTERVALS: [string, string][] = [["1m", "1d"], ["5m", "5d"], ["15m", "15d"], ["1h", "1s"], ["4h", "4s"], ["1d", "1g"]];

export const SHIFT = 3 * 3600;

export interface Bar { time: number; open: number; high: number; low: number; close: number }

/** Aralığın saniye karşılığı, örneğin 15m için 900. */
export function intervalSeconds(iv: string): number {
  const n = parseInt(iv, 10);
  return n * ({ m: 60, h: 3600, d: 86400 } as Record<string, number>)[iv.slice(-1)];
}

/** Fiyatın gösterildiği ondalık hane sayısı, format.ts num() ile aynı kural: bir doların altında beş anlamlı hane. */
export function digits(price: number): number {
  const a = Math.abs(price);
  if (a > 0 && a < 1) return Math.max(4, 4 - Math.floor(Math.log10(a)));
  return a >= 1000 ? 1 : a >= 10 ? 2 : 4;
}

/** Grafikten okunan fiyatı gösterildiği hane sayısına yuvarlar. */
export function roundPrice(price: number): number {
  const f = 10 ** digits(price);
  return Math.round(price * f) / f;
}

/** Unix saniyesini grafiğin Türkiye saatiyle gösterdiği zamana çevirir. */
export function chartTime(t: number): number {
  return t + SHIFT;
}

/**
 * Dakikalık mum güncellemesini grafiğin son çubuğuna işler.
 * @returns Güncellenmiş ya da yeni açılan çubuk. Mum son çubuktan eskiyse null.
 */
export function mergeBar(last: Bar | null, c: Candle, sec: number): Bar | null {
  const bucket = Math.floor(c.time / sec) * sec;
  if (!last || bucket > last.time) return { time: bucket, open: c.open, high: c.high, low: c.low, close: c.close };
  if (bucket < last.time) return null;
  return { ...last, high: Math.max(last.high, c.high), low: Math.min(last.low, c.low), close: c.close };
}
