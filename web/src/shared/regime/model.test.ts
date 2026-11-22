/**
 * file: model.test.ts
 * importable: yok
 *
 * Yapı ve rejim rozetinin modelini sınar. Burada bir hata yönü ters gösterir
 * ya da eski kayıtlarda boş bir rozet çizer.
 *
 * description: shared/regime/model.ts testleri.
 */

import { describe, expect, it } from "vitest";
import { badgesOf } from "./model";

describe("rejim rozeti", () => {
  it("üst ve alt aralığın yönü kısa adlarıyla, üst önce yazılır", () => {
    const [b] = badgesOf({ BTCUSDT: { structure: [{ interval: "4h", dir: "alcalan" }, { interval: "1h", dir: "yukselen" }],
                                      regime: "aşırı uzama", fit: false, waiting: { price: 60000 }, hint: "x" } });
    expect([b.symbol, b.arrows, b.regime, b.fit, b.waiting]).toEqual(["BTCUSDT", "4s ↓ · 1s ↑", "aşırı uzama", false,
                                                                      { price: 60000 }]);
  });

  it("yatay ve belirsiz de bir işaretle görünür", () => {
    const [b] = badgesOf({ X: { structure: [{ interval: "1d", dir: "yatay" }, { interval: "15m", dir: "belirsiz" }],
                                regime: "yatay", fit: false, waiting: null, hint: "" } });
    expect(b.arrows).toBe("1g → · 15d ?");
  });

  it("eski kayıtta rozet yoktur", () => {
    expect(badgesOf(undefined)).toEqual([]);
    expect(badgesOf(null)).toEqual([]);
  });
});
