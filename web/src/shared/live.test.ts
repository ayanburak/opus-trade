import { describe, expect, it } from "vitest";
import { gate, movesAccount } from "./live";

describe("movesAccount", () => {
  it("yalnızca pozisyonu olan paritenin mumunda hesabı yeniler", () => {
    const ps = [{ symbol: "BTCUSDT" }];
    expect(movesAccount({ candle: { symbol: "BTCUSDT" } }, ps)).toBe(true);
    expect(movesAccount({ candle: { symbol: "ETHUSDT" } }, ps)).toBe(false);
    expect(movesAccount({ candle: { symbol: "BTCUSDT" } }, [])).toBe(false);
    expect(movesAccount({ journal: {} }, ps)).toBe(false);
  });
});

describe("gate", () => {
  it("süre dolmadan ikinci geçişe izin vermez, dolunca verir", () => {
    let t = 1000;
    const g = gate(3000, () => t);
    expect(g()).toBe(true);
    t = 3999;
    expect(g()).toBe(false);
    t = 4000;
    expect(g()).toBe(true);
  });
});
