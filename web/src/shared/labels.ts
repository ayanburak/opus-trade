/**
 * file: labels.ts
 * importable: WAKES, REASONS, VIEWS, orderName, toolName
 *
 * Çekirdeğin kısa adlarının arayüzdeki Türkçe karşılıkları: uyandırma
 * nedenleri, kapanış nedenleri, Opus'un piyasa görüşü, emir türleri ve araç
 * adları.
 * Opus'a giden hiçbir şey değişmez, bu yalnızca görünüştür.
 *
 * description: Arayüzlerin ortak Türkçe etiketleri.
 */

/** Günlük kaydının uyandırma nedenleri. */
export const WAKES: Record<string, string> = {
  start: "başlangıç", timer: "zamanlayıcı", account: "hesap olayı", trigger: "tetikleyici", admin: "yönetici",
  correction: "düzeltme", restart: "yeniden başlama", resume: "devam", shutdown: "kapanış",
};

/** Kapanan işlemin nedeni. */
export const REASONS: Record<string, string> = {
  tp: "TP", sl: "SL", liquidation: "likidasyon", strategy: "strateji", admin: "yönetici", system: "sistem",
};

/** Opus'un piyasa görüşü. */
export const VIEWS: Record<string, string> = {
  yukselis: "Yükseliş", dusus: "Düşüş", yatay: "Yatay", belirsiz: "Belirsiz",
};

// stop ve take_profit girişte ve kapatan emirde başka şeylerdir, bu yüzden
// ad türden ve reduce'tan birlikte çıkar. Adlar Opus'un talimatındakilerle
// aynıdır: kırılım, geri çekilme, SL, TP, iz.
const ENTRY_ORDERS: Record<string, string> = {
  market: "piyasa", limit: "limit", stop: "kırılım", take_profit: "geri çekilme",
};
const EXIT_ORDERS: Record<string, string> = { take_profit: "TP", stop: "SL", trailing_stop: "iz" };

/** Bir emrin okunur adı. Bilinmeyen tür kendi adıyla kalır. */
export function orderName(o: { type: string; reduce: boolean }): string {
  return (o.reduce ? EXIT_ORDERS : ENTRY_ORDERS)[o.type] ?? o.type;
}

const TOOLS: Record<string, string> = {
  mumlar: "Mumlar", gostergeler_hesapla: "Göstergeler", gosterge: "Gösterge", emir_defteri: "Emir defteri",
  fonlama_gecmisi: "Fonlama geçmişi", acik_pozisyon_gecmisi: "Açık pozisyon geçmişi",
  long_short_oranlari: "Long-short oranları", seviyeler: "Destek ve direnç", hacim_profili: "Hacim profili",
  korelasyon: "Korelasyon", likidasyonlar: "Likidasyonlar", ny_acilisi: "NY açılışı", acilis_gecmisi: "Açılış geçmişi",
  gecmis_kararlarim: "Geçmiş kararları", gecmis_islemlerim: "Kapanmış işlemleri",
  WebSearch: "Web araması", WebFetch: "Sayfa okudu", Agent: "Araştırma görevi",
};

/** Bir aracın okunur adı. Bilinmeyen araç kendi adıyla kalır. */
export function toolName(name: string): string {
  const key = name.replace(/^piyasa\./, "");
  return TOOLS[key] ?? key;
}
