/**
 * file: model.ts
 * importable: Summary, summary, status, EVENTS, wakeText, toolCounts, toolLine, Marker, markers, Row, timeline
 *
 * Sayfanın saf mantığı. Üst satırdaki rakamlar, profilin durum satırı,
 * günlükteki uyandırma metni ve araç sayıları, grafikteki işlem işaretleri ve
 * günlükte kararlarla işlem olaylarının tek bir zaman akışında birleşmesi.
 *
 * description: Herkese açık sayfanın hesapları ve metinleri.
 */

// NOTE: Günlükte Opus'un kararı dışında yalnızca kararı olmadan olan işlem
// olayları ayrıca satır olur: TP, SL, likidasyon ve yöneticinin kapattığı
// pozisyon. Stratejinin kendi kapattığı işlem zaten kararın sonuçlarında
// yazılıdır, ikinci kez görünmez.

import { REASONS, WAKES, toolName } from "../shared/labels";
import { num, signed, when } from "../shared/format";
import type { Trade, Wake } from "../shared/types";
import type { Account, Entry, Profile, ToolRecord } from "../api/types";

export interface Summary { equity: number | null; ret: number | null; open: number | null; trades: number; wins: number }

/** Üst satırın rakamları. Açık pozisyon yoksa açık K/Z null'dır. */
export function summary(p: Profile | null, a: Account | null, trades: Trade[]): Summary {
  const equity = a?.state.equity ?? p?.equity ?? null;
  const start = p?.start_balance;
  const pos = a?.state.positions ?? [];
  return {
    equity,
    ret: equity != null && start ? (equity / start - 1) * 100 : null,
    open: pos.length ? pos.reduce((s, x) => s + (x.open_pnl ?? 0), 0) : null,
    trades: trades.length,
    wins: trades.filter((t) => t.net > 0).length,
  };
}

const STATES: Record<string, string> = { paused: "Duraklatıldı", stopped: "Durduruldu", draft: "Henüz başlamadı", archived: "Arşivde" };

/**
 * Profil adının altındaki kısa durum satırı.
 * @param thinking Akıştan araç çağrısı gelmeye başladı mı. Profil özetindeki busy geç yenilenebilir.
 */
export function status(p: Profile, thinking: boolean, tools: number, now: number): { text: string; tone: "busy" | "wait" | "off" } {
  if (p.state !== "running") return { text: STATES[p.state] ?? p.state, tone: "off" };
  if (p.busy || thinking) return { text: tools ? `Düşünüyor · ${tools} araç` : "Düşünüyor", tone: "busy" };
  return { text: p.next_wake ? `Sonraki çağrı ${when(p.next_wake, now)}` : "Bekliyor", tone: "wait" };
}

export const EVENTS: Record<string, string> = {
  opened: "Pozisyon açıldı", added: "Pozisyona eklendi", closed: "Pozisyon kapandı", protection: "Koruma değişti",
  order_placed: "Emir kuruldu", order_filled: "Emir gerçekleşti", order_failed: "Emir açılamadı",
  order_expired: "Emrin süresi doldu", order_cancelled: "Emir iptal edildi", funding: "Fonlama",
};

/** Bir uyandırmanın günlükteki satırı: nedeni ve varsa ayrıntısı. */
export function wakeText(w: Wake): { label: string; detail: string } {
  const label = WAKES[w.kind] ?? w.kind;
  let d: Record<string, unknown> | null = null;
  try {
    d = typeof w.data === "string" ? JSON.parse(w.data) : (w.data as Record<string, unknown> | null);
  } catch {
    d = null;
  }
  if (!d) return { label, detail: "" };
  if (d.type === "text") return { label, detail: String(d.text ?? "") };
  if (d.type === "event") {
    const reason = d.reason ? `, ${REASONS[String(d.reason)] ?? d.reason}` : "";
    const price = typeof d.price === "number" ? ` · ${num(d.price)}` : "";
    return { label, detail: `${EVENTS[String(d.kind)] ?? d.kind}${reason} · #${d.number} ${d.symbol} ${d.side}${price}` };
  }
  if (d.type === "fire") {
    const inner = (d.data ?? {}) as Record<string, unknown>;
    if (d.inner === "alert") return { label, detail: `Alarm ${d.symbol} ${num(inner.price as number)}` };
    return { label, detail: `Likidasyon dalgası ${d.symbol}` };
  }
  return { label, detail: "" };
}

/** Kararın araçlarını türüne göre sayar: piyasa verisi, web araması, okunan sayfa, araştırma görevi. */
export function toolCounts(tools: ToolRecord[] | undefined): string {
  if (!tools?.length) return "";
  const top = tools.filter((t) => !t.sub);
  const n = (f: (t: ToolRecord) => boolean) => top.filter(f).length;
  const parts: [number, string][] = [
    [n((t) => t.name.startsWith("piyasa.")), "piyasa verisi"], [n((t) => t.name === "WebSearch"), "arama"],
    [n((t) => t.name === "WebFetch"), "sayfa"], [n((t) => t.name === "Agent"), "araştırma görevi"],
  ];
  const other = top.length - parts.reduce((s, [c]) => s + c, 0);
  if (other > 0) parts.push([other, "diğer araç"]);
  return parts.filter(([c]) => c > 0).map(([c, w]) => `${c} ${w}`).join(" · ");
}

/** Bir aracın günlükteki adı ve kısa ayrıntısı. */
export function toolLine(t: ToolRecord): { name: string; detail: string } {
  return { name: toolName(t.name), detail: t.title ?? "" };
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

export type Row = { kind: "entry"; time: number; entry: Entry } | { kind: "trade"; time: number; trade: Trade };

const OWN_CLOSE = new Set(["strategy"]);

/** Kararlarla kararın dışında kapanan işlemleri yeniden eskiye tek akışta birleştirir. */
export function timeline(entries: Entry[], trades: Trade[]): Row[] {
  const oldest = entries.length ? entries[entries.length - 1].time : Infinity;
  const rows: Row[] = [
    ...entries.map((e) => ({ kind: "entry" as const, time: e.time, entry: e })),
    ...trades.filter((t) => !OWN_CLOSE.has(t.reason) && t.exit_time >= oldest)
      .map((t) => ({ kind: "trade" as const, time: t.exit_time, trade: t })),
  ];
  return rows.sort((a, b) => b.time - a.time);
}
