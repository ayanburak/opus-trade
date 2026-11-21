/**
 * file: layers.ts
 * importable: Layer, LayerSpec, LAYERS, DRAGGABLE, LINE_DIM, dimColor, readHidden, PriceLine, priceLines, Marker, markers
 *
 * Fiyat grafiğinin saf mantığı. Grafiğin katmanları ve açıklama satırındaki
 * görünüşleri, pozisyon, emir ve alarmlardan çıkan fiyat çizgileri, kapanmış
 * işlemlerin işaretleri, çizgi renginin soluklaştırılması ve tarayıcıda
 * saklanan filtre seçiminin okunması.
 *
 * description: Fiyat grafiğinin katmanları, çizgileri ve işaretleri.
 */

import { signed, num } from "../format";
import { REASONS } from "../labels";
import type { Alert, Order, Position, Trade } from "../types";

/** Grafikte ayrı ayrı gizlenebilen katmanlar. */
export type Layer = "entry" | "tp" | "sl" | "liq" | "order" | "alarm" | "marks";

/**
 * Katmanın açıklama satırındaki adı, rengini veren CSS değişkeni ve çizgi biçimi. marks fiyat çizgisi değil işarettir.
 * colors verilmişse katmanın çizgileri bu renklerden birini alır, açıklama satırı hepsini gösterir.
 */
export interface LayerSpec {
  key: Layer; label: string; color: string; colors?: string[]; style: "solid" | "dotted" | "dashed" | "sparse" | "marks";
}

/** Grafiğin katmanları. Açıklama satırı ve filtre menüsü bu sırayla çizilir. */
export const LAYERS: LayerSpec[] = [
  { key: "entry", label: "giriş", color: "--entry", colors: ["--long", "--short"], style: "solid" },
  { key: "tp", label: "TP", color: "--tp", style: "solid" },
  { key: "sl", label: "SL", color: "--sl", style: "solid" },
  { key: "liq", label: "likidasyon", color: "--dim", style: "dotted" },
  { key: "order", label: "emir", color: "--order", colors: ["--long", "--short"], style: "dashed" },
  { key: "alarm", label: "alarm", color: "--alarm", style: "sparse" },
  { key: "marks", label: "işlem işaretleri", color: "--accent", style: "marks" },
];

/** Düzenlenebilen grafikte sürüklenebilen katmanlar. Giriş ve likidasyon yöneticinin taşıyabileceği bir seviye değildir. */
export const DRAGGABLE: ReadonlySet<Layer> = new Set<Layer>(["tp", "sl", "order", "alarm"]);

/** Fiyat çizgilerinin rengi arka plana bu oranda karıştırılır, çizgiler mumların önüne geçmesin diye. */
export const LINE_DIM = 0.3;

/**
 * #RRGGBB rengini arka plana karıştırır. Sonuç saydam değildir, eksendeki etiket okunur kalır.
 * @param share Arka planın payı, 0 ile 1 arası.
 */
export function dimColor(color: string, background: string, share: number): string {
  const rgb = (c: string) => [1, 3, 5].map((i) => parseInt(c.slice(i, i + 2), 16));
  const [a, b] = [rgb(color), rgb(background)];
  return "#" + a.map((v, i) => Math.round(v * (1 - share) + b[i] * share).toString(16).padStart(2, "0")).join("");
}

/** Ziyaretçi henüz seçim yapmadıysa gizli gelen katmanlar. */
const DEFAULT_HIDDEN: Layer[] = ["marks"];

/**
 * Tarayıcıda saklanan gizli katmanları okur. Hiç saklanmadıysa varsayılan gizliler gelir, bilinmeyen ya da bozuk
 * değer yok sayılır.
 * @param raw Saklanan JSON dizisi, hiç saklanmadıysa null.
 */
export function readHidden(raw: string | null): Set<Layer> {
  if (raw == null) return new Set(DEFAULT_HIDDEN);
  try {
    const keys = JSON.parse(raw);
    return new Set(Array.isArray(keys) ? LAYERS.map((l) => l.key).filter((k) => keys.includes(k)) : []);
  } catch {
    return new Set();
  }
}

/**
 * Grafikteki bir fiyat çizgisi. key çizimler arasında aynı çizgiyi tanır. Çizgi ait olduğu pozisyonu, emri ya da
 * alarmı taşır, tp kaçıncı TP kademesi olduğudur. Düzenlenebilen grafik sürüklemeyi bunlarla öneriye çevirir. color
 * verilmişse çizgi katmanın rengi yerine onu alır, giriş ve emir çizgisi yönünü böyle gösterir.
 */
export interface PriceLine {
  key: string; layer: Layer; price: number; title: string; color?: string;
  position?: Position; tp?: number; order?: Order; alert?: Alert;
}

/** Seçili paritenin pozisyon, emir ve alarm çizgileri, gizli katmanlar çıkarılmış olarak. */
export function priceLines(symbol: string, positions: Position[], orders: Order[], alerts: Alert[],
                           hidden: ReadonlySet<Layer>): PriceLine[] {
  const out: PriceLine[] = [];
  for (const p of positions.filter((x) => x.symbol === symbol)) {
    out.push({ key: `e${p.id}`, layer: "entry", price: p.entry_price, title: `#${p.number} ${p.side} giriş`, position: p,
              color: p.side === "long" ? "--long" : "--short" });
    p.tp_levels.forEach((l, i) => out.push({ key: `t${p.id}-${i}`, layer: "tp", price: l.price,
                                              title: `#${p.number} TP %${num(l.pct, 0)}`, position: p, tp: i }));
    if (p.sl != null) out.push({ key: `s${p.id}`, layer: "sl", price: p.sl, title: `#${p.number} SL`, position: p });
    out.push({ key: `l${p.id}`, layer: "liq", price: p.liq_price, title: `#${p.number} likidasyon`, position: p });
  }
  for (const o of orders.filter((x) => x.symbol === symbol)) {
    out.push({ key: `o${o.id}`, layer: "order", price: o.trigger, title: `#${o.number} ${o.side} emir`, order: o,
              color: o.side === "long" ? "--long" : "--short" });
  }
  for (const a of alerts.filter((x) => x.symbol === symbol)) {
    out.push({ key: `a${a.number}`, layer: "alarm", price: a.price, title: `alarm #${a.number}`, alert: a });
  }
  return out.filter((l) => !hidden.has(l.layer));
}

export interface Marker { time: number; position: "aboveBar" | "belowBar"; shape: "arrowUp" | "arrowDown" | "circle"; tone: "up" | "down"; text: string }

/**
 * Seçili paritenin kapanmış işlemlerinin grafikteki işaretleri, zamana göre sıralı. Giriş yönün,
 * çıkış sonucun rengini alır.
 * @param from Grafikteki ilk çubuğun zamanı. Daha eski işaret çizilmez.
 */
export function markers(trades: Trade[], symbol: string, from: number): Marker[] {
  const out: Marker[] = [];
  for (const t of trades.filter((x) => x.symbol === symbol)) {
    const long = t.side === "long";
    if (t.entry_time >= from) {
      out.push({ time: t.entry_time, position: long ? "belowBar" : "aboveBar", shape: long ? "arrowUp" : "arrowDown",
                 tone: long ? "up" : "down", text: `#${t.number} ${t.side}` });
    }
    if (t.exit_time >= from) {
      out.push({ time: t.exit_time, position: long ? "aboveBar" : "belowBar", shape: "circle", tone: t.net >= 0 ? "up" : "down",
                 text: `#${t.number} ${REASONS[t.reason] ?? t.reason} ${signed(t.net)}` });
    }
  }
  return out.sort((a, b) => a.time - b.time);
}
