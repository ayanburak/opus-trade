/**
 * file: notebook.test.ts
 * importable: yok
 *
 * Not defterinin iki biçiminin gösterimini sınar. Eski kayıtlar düz metin,
 * yenileri plan, seviyeler ve gündem taşır. Biri okunamazsa günlükte defter
 * boş görünür.
 *
 * description: shared/notebook.ts testleri.
 */

import { describe, expect, it } from "vitest";
import { notebookParts } from "./notebook";

describe("not defteri", () => {
  it("eski düz metin tek bölümdür", () => {
    expect(notebookParts("K5: uyum")).toEqual([{ title: "Not defteri", text: "K5: uyum" }]);
  });

  it("yeni biçim plan, seviyeler ve gündem olarak ayrılır, boş bölüm yazılmaz", () => {
    const parts = notebookParts({ plan: { metin: "Short ara.", bozulma: "62615 üstü", gecerlilik: "23.11.2026 18:00" },
                                  seviyeler_notu: "62320 direnç.", gundem: "" });
    expect(parts.map((p) => p.title)).toEqual(["Plan", "Seviyeler"]);
    expect(parts[0].text).toBe("Short ara.\nBozulma koşulu: 62615 üstü\nGeçerlilik: 23.11.2026 18:00");
  });

  it("defter yoksa bölüm yok", () => {
    expect(notebookParts(undefined)).toEqual([]);
  });
});
