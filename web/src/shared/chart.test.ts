/**
 * file: chart.test.ts
 * importable: yok
 *
 * Canlı mumun grafiğe işlenmesini sınar. Burada bir hata grafikte yanlış
 * yükseklikte ya da yanlış zamanda bir çubuk çizer.
 *
 * description: shared/chart için testler.
 */

import { describe, expect, it } from "vitest";
import { intervalSeconds, mergeBar } from "./chart";
import type { Candle } from "./types";

const candle = (time: number, high: number, low: number, close: number): Candle =>
  ({ symbol: "BTCUSDT", time, open: 100, high, low, close, volume: 1 });

describe("mergeBar", () => {
  it("aynı aralıktaki dakikalık güncelleme son çubuğun ucunu genişletir, yeni aralık yeni çubuk açar", () => {
    const last = { time: 900, open: 100, high: 105, low: 99, close: 101 };
    expect(mergeBar(last, candle(960, 107, 100, 106), 900)).toEqual({ time: 900, open: 100, high: 107, low: 99, close: 106 });
    expect(mergeBar(last, candle(1800, 103, 101, 102), 900)).toEqual({ time: 1800, open: 100, high: 103, low: 101, close: 102 });
    expect(mergeBar(last, candle(60, 103, 101, 102), 900)).toBeNull();
  });

  it("aralıkların saniye karşılığı", () => {
    expect([intervalSeconds("1m"), intervalSeconds("4h"), intervalSeconds("1d")]).toEqual([60, 14400, 86400]);
  });
});
