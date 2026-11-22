/**
 * file: types.ts
 * importable: Side, OrderType, Protection, ExitOutlook, Position, EntryNote, Order, AccountState, Alert, Trade, EquityPoint, Candle, ToolCall,
 *   Wake, ActionResult, Notebook, Structure, PairView, Decision
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

/**
 * Bir koruma emri tetiklenirse ne olacağının çekirdekteki tahmini. net kapanınca işlemin net'i olacak tutardır.
 * order_id null ise adım likidasyondur, bir stop likidasyonun ötesinde kaldığı için sıraya girmiştir.
 */
export interface ExitOutlook { order_id: number | null; level: number; qty: number; net: number }

/**
 * Parite ve yön başına tek pozisyon. Kaldıracı yoktur, paritenin ayarı AccountState.leverages'tadır. realized
 * pozisyonun kapanan parçalarının net toplamıdır. outlook TP'ler ve stoplar ayrı ayrı, fiyata yakından uzağa
 * sıralı tahminlerdir. open_pnl ve notional son fiyat gelmediyse null'dır.
 */
export interface Position {
  id: number; symbol: string; side: Side; qty: number; margin: number; entry_price: number; entry_time: number;
  best_price: number; worst_price: number; liq_price: number; fee: number; funding: number; open_pnl: number | null;
  notional: number | null; realized: number; realized_count: number; outlook: ExitOutlook[];
  /** Panelde gelir: girişlerin notları ve pozisyonu yöneticinin açıp açmadığı. */
  notes?: EntryNote[]; by_admin?: boolean;
}

/** Bir girişin gerekçesi ve geçersizliği, yalnızca panelde. */
export interface EntryNote {
  id: number; symbol: string; side: Side; invalid_price: number; invalid_text: string; created_at: number;
  reason: { karsi_kanit?: string; risk_beyani?: { yuzde: number; gerekce: string }; son_stoptan_farki?: string };
}

/**
 * Bekleyen emir. reduce kapatan emirdir, yani koruma. price limitte emrin fiyatı, stop ve take-profit'te tetiktir,
 * izde yoktur. Kapatan emirde qty yoksa pozisyonun kalanının tamamını kapatır. margin girişin ayırdığı tutardır.
 */
export interface Order {
  id: number; number: number; symbol: string; side: Side; type: OrderType; reduce: boolean; qty: number | null;
  price: number | null; trail_pct: number | null; best: number | null; margin: number; protection: Protection[];
  expires_at: number | null; created_at: number;
  /** Panelde gelir: bekleyen girişin notu ve emri yöneticinin verip vermediği. */
  note?: EntryNote | null; by_admin?: boolean;
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

/** Not defteri. Şablon 6'dan önce düz metindi. */
export interface Notebook {
  plan: { metin: string; bozulma: string; gecerlilik: string };
  seviyeler_notu: string;
  gundem: string;
}

/** Bir aralığın yapısı: yön ve son iki tepe ile dip, eskiden yeniye. */
export interface Structure { yon: string; tepeler: number[]; dipler: number[] }

/** Bir paritenin değerlendirmesi. Şablon 6'dan önce gorus ve degerlendirme, sonra yapi ve rejim taşır. */
export interface PairView {
  gorus?: string;
  degerlendirme?: string;
  yapi?: { ust: Structure; alt: Structure };
  rejim?: { rejim: string; kanit?: string; uygun: string; bekledigi?: { tur: string; fiyat: number; saat: string } };
}

/** Opus'un kararı. Alan adları karar şemasındaki Türkçe adlardır. gorus ve guven şablon 6'dan önceki kayıtlardadır. */
export interface Decision {
  analiz?: string;
  pariteler?: Record<string, PairView>;
  ozet?: string;
  gorus?: string;
  guven?: number;
  eylemler?: unknown[];
  not_defteri?: string | Notebook;
  sonraki_cagri_dk?: number;
  defter_sinamasi?: {
    plan: { durum: string; gerekce: string }; yapi: { durum: string; gerekce: string };
    gundem: { durum: string; gerekce: string };
    emirler: { numara: number; sembol: string; yon: string; durum: string; gerekce: string }[];
    pozisyonlar: { sembol: string; yon: string; durum: string; gerekce: string }[];
  };
  kapanan_islemler?: { pozisyon_no: number; sinif: string; aciklama: string }[];
  yoneticiye_cevap?: string;
  yoneticiye_oneri?: string;
}
