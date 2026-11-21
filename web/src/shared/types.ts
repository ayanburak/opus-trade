/**
 * file: types.ts
 * importable: Side, TpLevel, Position, Order, AccountState, Alert, Trade, EquityPoint, Candle, ToolCall, Wake,
 *   ActionResult, Decision
 *
 * Hesabın, işlemlerin, mumların ve günlüğün ortak veri biçimleri. Alan adları
 * çekirdeğin JSON anahtarlarıyla aynıdır.
 *
 * description: Arayüzlerin ortak veri türleri.
 */

export type Side = "long" | "short";
export interface TpLevel { price: number; pct: number; qty?: number | null }

export interface Position {
  id: number; number: number; symbol: string; side: Side; leverage: number; qty: number; margin: number;
  entry_price: number; entry_time: number; tp_levels: TpLevel[]; sl: number | null; trail_pct: number | null;
  best_price: number; worst_price: number; liq_price: number; fee: number; funding: number; open_pnl: number | null;
}

export interface Order {
  id: number; number: number; symbol: string; side: Side; trigger: number; below: boolean; maker: boolean;
  leverage: number; margin: number; tp_levels: TpLevel[]; sl: number | null; trail_pct: number | null;
  expires_at: number | null; created_at: number;
}

export interface AccountState { account_id: number; cash: number; equity: number; positions: Position[]; orders: Order[] }

export interface Alert { number: number; symbol: string; price: number; below: boolean; note: string; actor: string; created_at: number }

export interface Trade {
  id: number; number: number; symbol: string; side: Side; leverage: number; qty: number; margin: number;
  entry_price: number; entry_time: number; exit_price: number; exit_time: number; reason: string;
  pnl: number; fee: number; funding: number; net: number;
}

export interface EquityPoint { time: number; cash: number; equity: number; margin: number }

export interface Candle { symbol: string; time: number; open: number; high: number; low: number; close: number; volume: number }

export interface ToolCall {
  id: number; seq: number; name: string; title: string | null; input: string | null; result: string | null;
  is_error: number | null; duration_s: number | null; sub: number;
}

export interface Wake { kind: string; time: number; data: unknown }

export interface ActionResult { label: string; ok: boolean; message: string }

/** Opus'un kararı. Alan adları karar şemasındaki Türkçe adlardır. */
export interface Decision {
  analiz?: string;
  pariteler?: Record<string, { gorus: string; degerlendirme: string }>;
  ozet?: string;
  gorus?: string;
  guven?: number;
  eylemler?: unknown[];
  not_defteri?: string;
  sonraki_cagri_dk?: number;
}
