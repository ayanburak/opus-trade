/**
 * file: RegimeBadge.tsx
 * importable: RegimeBadge
 *
 * Kararın yapı ve rejim rozeti: "4s ↓ · 1s ↑" ve yanında rejimin etiketi.
 * Rejim stratejiye uygunsa etiket renklidir ve uygun yazar, değilse gridir
 * ve beklenen fiyatı ya da saati yazar. Üzerine gelince rejimin kısa
 * açıklaması çıkar. Herkese açık sayfanın günlüğü ve panelin Masa sayfası
 * aynı rozeti gösterir. Profilde birden çok parite varsa her birinin önünde
 * adı yazar.
 *
 * description: Kararın yapı ve rejim rozeti.
 */

import { num } from "../format";
import { badgesOf, type Badge, type Waiting } from "./model";
import "./regime.css";

function waitText(w: Waiting | null): string {
  if (w?.price != null) return `bekliyor ${num(w.price)}`;
  if (w?.time) return `bekliyor ${w.time}`;
  return "uygun değil";
}

/** badge kaydın badge alanıdır. Yoksa hiçbir şey çizilmez, eski kayıtlar kendi etiketleriyle kalır. */
export function RegimeBadge({ badge }: { badge: Record<string, Badge> | undefined | null }) {
  const views = badgesOf(badge);
  if (!views.length) return null;
  const named = views.length > 1;
  return (
    <span className="regime-badges">
      {views.map((v) => (
        <span key={v.symbol} className="regime-badge" title={v.hint}>
          {named && <span className="rb-symbol">{v.symbol}</span>}
          <span className="rb-arrows mono">{v.arrows}</span>
          <span className={`rb-label${v.fit ? " fit" : ""}`}>{v.regime} · {v.fit ? "uygun" : waitText(v.waiting)}</span>
        </span>
      ))}
    </span>
  );
}
