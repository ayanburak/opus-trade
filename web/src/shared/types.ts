/**
 * file: types.ts
 * importable: Side, OrderType, Protection, Position, Order, AccountState, Alert, Trade, EquityPoint, Candle, ToolCall,
 *   Wake, ActionResult, Decision
 *
 * Hesabın, işlemlerin, mumların ve günlüğün ortak veri biçimleri. Alan adları
 * çekirdeğin JSON anahtarlarıyla aynıdır. Hesap Binance'in modelidir: parite
 * ve yön başına tek pozisyon, kaldıraç paritenin ayarı, koruma pozisyona
 * bağlı numaralı kapatan emirlerdir.
 *
 * description: Arayüzlerin ortak veri türleri.
 */

export type Side = "long" | "short";

/** Emrin türü. stop ve take_profit girişte kırılım ve geri çekilme, kapatan emirde SL ve TP'dir. */
export type OrderType = "market" | "limit" | "stop" | "take_profit" | "trailing_stop";

/** Bekleyen girişin dolunca kuracağı koruma. qty yoksa girişin dolan miktarının tamamıdır. */
export interface Protection { type: OrderType; price: number | null; trail_pct: number | null; qty: number | null }

/** Parite ve yön başına tek pozisyon. Kaldıracı yoktur, paritenin ayarı AccountState.leverages'tadır. */
export interface Position {
  id: number; symbol: string; side: Side; qty: number; margin: number; entry_price: number; entry_time: number;
  best_price: number; worst_price: number; liq_price: number; fee: number; funding: number; open_pnl: number | null;
}

/**
 * Bekleyen emir. reduce kapatan emirdir, yani koruma. price limitte emrin fiyatı, stop ve take-profit'te tetiktir,
 * izde yoktur. Kapatan emirde qty yoksa pozisyonun kalanının tamamını kapatır. margin girişin ayırdığı tutardır.
 */
export interface Order {
  id: number; number: number; symbol: string; side: Side; type: OrderType; reduce: boolean; qty: number | null;
  price: number | null; trail_pct: number | null; best: number | null; margin: number; protection: Protection[];
  expires_at: number | null; created_at: number;
}

/** available bekleyen girişlerin ayırdığı marjin düşülmüş nakittir. leverages profilin her paritesinin kaldıracıdır. */
export interface AccountState {
  account_id: number; cash: number; equity: number; available: number; positions: Position[]; orders: Order[];
  leverages: Record<string, number>;
}

export interface Alert { number: number; symbol: string; price: number; below: boolean; note: string; actor: string; created_at: number }

/**
 * Pozisyonun kapanan bir parçası. leverage büyüklüğün marjine oranıdır, farklı kaldıraçlı girişler birleşmiş
 * olabilir. tp_prices ve sl kapanış anında pozisyonu koruyan emirlerin fiyatlarıdır.
 */
export interface Trade {
  id: number; position_id: number; symbol: string; side: Side; leverage: number; qty: number; margin: number;
  entry_price: number; entry_time: number; exit_price: number; exit_time: number; reason: string;
  pnl: number; fee: number; funding: number; net: number; best_price: number; worst_price: number;
  tp_prices: number[]; sl: number | null; order_id: number | null;
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
