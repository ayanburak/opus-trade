/**
 * file: types.ts
 * importable: Profile, Account, Decision, ToolRecord, Entry, LiveTool
 *
 * Herkese açık API'nin cevaplarının biçimleri. Alanlar sunucudaki beyaz
 * listeyle aynıdır, opus_trade/api/public.py. Hesap, işlem ve mum biçimleri
 * shared/types'tadır.
 *
 * description: Herkese açık API cevaplarının türleri.
 */

import type { AccountState, ActionResult, Alert, Wake } from "../shared/types";

export type State = "draft" | "running" | "paused" | "stopped" | "archived";

export interface Profile {
  id: number; name: string; short: string; strategy: string | null; state: State; version: number | null;
  equity: number | null; start_balance: number | null; positions: number; orders: number; busy: boolean;
  next_wake: number | null; symbols: string[];
}

export interface Account {
  state: AccountState;
  prices: Record<string, number>;
  triggers: Record<string, unknown>[];
  alerts: Alert[];
}

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

/** Kararın içinde saklanan araç çağrısının kısa kaydı. */
export interface ToolRecord { name: string; title?: string | null; sub?: boolean | number | null }

export interface Entry {
  id: number; time: number; version_id: number; next_wake: number | null;
  wakes: Wake[];
  results: ActionResult[];
  record: { summary?: string; decision?: Decision; tools?: ToolRecord[]; model?: string; effort?: string;
            duration_s?: number; waves?: unknown };
  error: string | null;
}

/** Opus düşünürken akıştan gelen araç çağrısı. */
export interface LiveTool { name: string; title?: string | null; sub?: boolean | number | null }
