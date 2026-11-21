/**
 * file: layers.test.ts
 * importable: yok
 *
 * Grafiğin çizgilerini, işaretlerini ve filtre seçimini sınar. Burada bir
 * hata grafikte olmayan bir seviye, gizlenmiş bir katmanın çizgisi ya da
 * yanlış işlem işareti gösterir.
 *
 * description: shared/pricechart/layers.ts testleri.
 */

import { describe, expect, it } from "vitest";
import type { Alert, Order, Position, Trade } from "../types";
import { DRAGGABLE, LAYERS, dimColor, markers, priceLines, readHidden, type Layer } from "./layers";

const NOW = 1_795_149_360;
const trade = (n: number, reason: string, net: number, exit: number): Trade => ({ id: n, position_id: n, symbol: "BTCUSDT",
  side: "long", leverage: 10, qty: 1, margin: 10, entry_price: 1, entry_time: exit - 600, exit_price: 2, exit_time: exit,
  reason, pnl: net, fee: 0, funding: 0, net, best_price: 2, worst_price: 1, tp_prices: [], sl: null, order_id: null });

describe("grafik işaretleri", () => {
  it("yalnızca seçili paritenin ve grafiğin başlangıcından yeni işaretleri çizilir", () => {
    const ms = markers([trade(1, "sl", -2, NOW), { ...trade(2, "tp", 1, NOW), symbol: "ETHUSDT" }], "BTCUSDT", NOW - 100);
    expect(ms).toEqual([{ time: NOW, position: "aboveBar", shape: "circle", tone: "down", text: "SL −2,00" }]);
  });
});

describe("grafik katmanları", () => {
  const pos = { id: 1, symbol: "BTCUSDT", side: "long", entry_price: 100, liq_price: 80 } as unknown as Position;
  const exit = (id: number, type: string, price: number | null, qty: number | null, side = "long") =>
    ({ id, number: id, symbol: "BTCUSDT", side, type, reduce: true, price, qty }) as unknown as Order;
  const tp = exit(10, "take_profit", 110, 0.5), sl = exit(11, "stop", 95, null), trail = exit(12, "trailing_stop", null, null);
  const order = { id: 2, number: 2, symbol: "BTCUSDT", side: "short", type: "stop", reduce: false, price: 120 } as unknown as Order;
  const alert = { number: 3, symbol: "BTCUSDT", price: 130 } as unknown as Alert;
  const other = { ...alert, symbol: "ETHUSDT" };

  it("her katman kendi çizgisini verir, başka paritenin çizgisi çizilmez", () => {
    const lines = priceLines("BTCUSDT", [pos], [tp, sl, order], [alert, other], new Set());
    expect(lines.map((l) => [l.layer, l.price])).toEqual(
      [["entry", 100], ["liq", 80], ["tp", 110], ["sl", 95], ["order", 120], ["alarm", 130]]);
  });

  it("koruma çizgisi kapatan emrin numarasını, adını ve miktarını yazar, miktarsız emir kalanın tümüdür", () => {
    const lines = priceLines("BTCUSDT", [pos], [tp, sl], [], new Set());
    expect(lines.filter((l) => l.order).map((l) => l.title)).toEqual(["#10 TP 0,5", "#11 SL tümü"]);
  });

  it("stop girişte kırılım, kapatan emirde SL'dir, katmanı ve adı buna göre seçilir", () => {
    const lines = priceLines("BTCUSDT", [pos], [order, sl], [], new Set());
    expect(lines.filter((l) => l.order).map((l) => [l.layer, l.title])).toEqual(
      [["order", "#2 short kırılım"], ["sl", "#11 SL tümü"]]);
  });

  it("izin sabit fiyatı yoktur, çizgisi çizilmez", () => {
    expect(priceLines("BTCUSDT", [pos], [trail], [], new Set()).some((l) => l.order)).toBe(false);
  });

  it("giriş ve emir çizgisi yönünü renginden belli eder, diğer çizgiler katmanın rengini alır", () => {
    const short = { ...pos, id: 4, side: "short" } as Position;
    const lines = priceLines("BTCUSDT", [pos, short], [order, { ...order, id: 5, side: "long" }, tp], [alert], new Set());
    expect(lines.filter((l) => l.layer === "entry").map((l) => l.color)).toEqual(["--long", "--short"]);
    expect(lines.filter((l) => l.layer === "order").map((l) => l.color)).toEqual(["--short", "--long"]);
    expect(lines.filter((l) => l.layer !== "entry" && l.layer !== "order").every((l) => l.color === undefined)).toBe(true);
  });

  it("çizgi kaynağını taşır, koruma çizgisi kendi yönündeki pozisyona bağlanır", () => {
    const short = { ...pos, id: 4, side: "short" } as Position;
    const shortSl = exit(13, "stop", 105, null, "short");
    const lines = priceLines("BTCUSDT", [pos, short], [tp, shortSl, order], [alert], new Set());
    expect(lines.find((l) => l.order === tp)!.position).toBe(pos);
    expect(lines.find((l) => l.order === shortSl)!.position).toBe(short);
    expect(lines.find((l) => l.layer === "order")!.order).toBe(order);
    expect(lines.find((l) => l.layer === "alarm")!.alert).toBe(alert);
    expect(new Set(lines.map((l) => l.key)).size).toBe(lines.length);
  });

  it("yalnızca yöneticinin taşıyabileceği seviyeler sürüklenir", () => {
    expect([...DRAGGABLE].sort()).toEqual(["alarm", "order", "sl", "tp"]);
  });

  it("gizlenen katmanın çizgisi çıkar, diğerleri kalır", () => {
    const lines = priceLines("BTCUSDT", [pos], [tp, sl, order], [alert], new Set<Layer>(["tp", "alarm"]));
    expect(lines.map((l) => l.layer)).toEqual(["entry", "liq", "sl", "order"]);
  });

  it("ilk ziyarette işlem işaretleri gizlidir, saklanan seçimden yalnızca bilinen katmanlar okunur", () => {
    expect([...readHidden('["sl","yok","marks"]')]).toEqual(["sl", "marks"]);
    expect(readHidden("{bozuk").size).toBe(0);
    expect(readHidden('{"sl":1}').size).toBe(0);
    expect([...readHidden(null)]).toEqual(["marks"]);
    expect(readHidden("[]").size).toBe(0);
  });

  it("çizgi rengi arka plana verilen oranda karışır, sonuç saydam olmayan bir renktir", () => {
    expect(dimColor("#ffffff", "#000000", 0.3)).toBe("#b3b3b3");
    expect(dimColor("#7FD3A0", "#161A21", 0)).toBe("#7fd3a0");
  });

  it("her katmanın açıklama satırında ve menüde tek bir yeri vardır", () => {
    expect(new Set(LAYERS.map((l) => l.key)).size).toBe(LAYERS.length);
  });
});
