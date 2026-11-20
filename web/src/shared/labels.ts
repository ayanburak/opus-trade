/**
 * file: labels.ts
 * importable: WAKES, REASONS, VIEWS, toolName
 *
 * Çekirdeğin kısa adlarının arayüzdeki Türkçe karşılıkları: uyandırma
 * nedenleri, kapanış nedenleri, Opus'un piyasa görüşü ve araç adları.
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
