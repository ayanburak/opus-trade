/**
 * file: model.ts
 * importable: Badge, Waiting, BadgeView, badgesOf, ARROWS, INTERVALS
 *
 * Kararın yapı ve rejim rozetinin verisi. Rozet çekirdeğin kayda yazdığı
 * badge alanından okunur: parite başına üst ve alt aralığın yönü, rejimin
 * etiketi, uygun olup olmadığı ve uygun değilse beklenen fiyat ya da saat.
 * Etiketler çekirdekte bir kez kurulur, burada yalnızca oklar ve kısa aralık
 * adları vardır. Şablon 6'dan önceki kayıtlarda badge yoktur, badgesOf boş
 * liste döner.
 *
 * description: Yapı ve rejim rozetinin modeli.
 */

/** Uygun olmayan rejimde beklenen fiyat ya da saat. */
export interface Waiting { price?: number; time?: string }

/** Kaydın badge alanında bir paritenin rozeti. */
export interface Badge {
  structure: { interval: string; dir: string }[];
  regime: string;
  fit: boolean;
  waiting: Waiting | null;
  hint: string;
}

/** Ekranda bir paritenin rozeti. arrows "4s ↓ · 1s ↑" gibi yapıdır. */
export interface BadgeView { symbol: string; arrows: string; regime: string; fit: boolean; waiting: Waiting | null; hint: string }

/** Yönün oku. Yatay ve belirsiz de görünür, okun yokluğu veri eksik sanılmasın. */
export const ARROWS: Record<string, string> = { yukselen: "↑", alcalan: "↓", yatay: "→", belirsiz: "?" };

/** Aralığın rozetteki kısa adı. */
export const INTERVALS: Record<string, string> = { "15m": "15d", "1h": "1s", "4h": "4s", "1d": "1g" };

/** Kaydın badge alanını parite sırasıyla rozetlere çevirir. Alan yoksa boş liste döner. */
export function badgesOf(badge: Record<string, Badge> | undefined | null): BadgeView[] {
  if (!badge) return [];
  return Object.entries(badge).map(([symbol, b]) => ({
    symbol,
    arrows: b.structure.map((s) => `${INTERVALS[s.interval] ?? s.interval} ${ARROWS[s.dir] ?? s.dir}`).join(" · "),
    regime: b.regime, fit: b.fit, waiting: b.waiting, hint: b.hint,
  }));
}
