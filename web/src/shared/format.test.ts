/**
 * file: format.test.ts
 * importable: yok
 *
 * Sayı okuma ve yazmanın testleri. Burada bir hata forma yazılan fiyatı bin
 * kat büyük ya da küçük okutup yanlış emir gönderir.
 *
 * description: shared/format için testler.
 */

import { describe, expect, it } from "vitest";
import { dayClock, duration, money, num, parseNum, pct, signed, when } from "./format";

describe("parseNum", () => {
  it("tek nokta ya da virgül her zaman ondalıktır, eski arayüzdeki binlik hatası tekrar etmemeli", () => {
    expect(parseNum("1.234")).toBe(1.234);
    expect(parseNum("1,234")).toBe(1.234);
    expect(parseNum("0,5")).toBe(0.5);
    expect(parseNum("62400")).toBe(62400);
  });

  it("ikisi birlikte geçerse sondaki ondalıktır, tekrarlanan işaret binliktir", () => {
    expect(parseNum("62.400,5")).toBe(62400.5);
    expect(parseNum("62,400.5")).toBe(62400.5);
    expect(parseNum("1.234.567")).toBe(1234567);
    expect(parseNum(" −3,5 ")).toBe(-3.5);
  });

  it("boş ya da sayı olmayan metin null döner, sıfır sanılmamalı", () => {
    for (const bad of ["", "  ", "abc", "1,2,3x", "12a", "--1"]) expect(parseNum(bad)).toBeNull();
  });
});

describe("gösterim", () => {
  it("Türkçe ayraçlar ve eksi işareti kullanılır", () => {
    expect(num(62099.1, 2)).toBe("62.099,10");
    expect(money(13.61)).toBe("$13,61");
    expect(money(-0.23)).toBe("−$0,23");
    expect(signed(0.31)).toBe("+0,31");
    expect(pct(36.1)).toBe("+%36,10");
    expect(pct(-3.8)).toBe("−%3,80");
  });

  it("tarih gün.ay saat:dakika yazılır, Türkiye saatiyle", () => {
    expect(dayClock(1_795_149_360)).toBe("20.11 07:36");
  });

  it("başka güne düşen saat günüyle yazılır, gün Türkiye saatine göre döner", () => {
    const now = 1_795_149_360;
    expect(when(now + 3600, now)).toBe("bugün 08:36");
    expect(when(now + 17 * 3600, now)).toBe("yarın 00:36");
    expect(when(now - 8 * 3600, now)).toBe("dün 23:36");
    expect(when(now + 3 * 86400, now)).toBe("23.11 07:36");
  });
});

describe("süre", () => {
  it("süreyi kısa yazar", () => {
    expect(duration(45)).toBe("45 sn");
    expect(duration(3 * 3600 + 20 * 60)).toBe("3 sa 20 dk");
    expect(duration(2 * 86400 + 4 * 3600 + 59)).toBe("2 gün 4 sa");
  });
});
