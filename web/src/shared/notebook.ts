/**
 * file: notebook.ts
 * importable: NotebookPart, notebookParts
 *
 * Not defterinin ekranda gösterilen bölümleri. Defter şablon 6'dan beri plan,
 * seviyeler notu ve gündemden oluşur, öncesinde düz metindi. İki biçim de aynı
 * listeye çevrilir, arayüzler eski kayıtları da gösterebilir.
 *
 * description: Not defterinin iki biçimini ekran bölümlerine çeviren yardımcı.
 */

import type { Notebook } from "./types";

export interface NotebookPart { title: string; text: string }

/** Defteri başlık ve metin çiftlerine çevirir. Boş defter boş liste verir. */
export function notebookParts(nb: string | Notebook | null | undefined): NotebookPart[] {
  if (!nb) return [];
  if (typeof nb === "string") return [{ title: "Not defteri", text: nb }];
  const out: NotebookPart[] = [];
  if (nb.plan?.metin) {
    out.push({ title: "Plan", text: `${nb.plan.metin}\nBozulma koşulu: ${nb.plan.bozulma}\nGeçerlilik: ${nb.plan.gecerlilik}` });
  }
  if (nb.seviyeler_notu) out.push({ title: "Seviyeler", text: nb.seviyeler_notu });
  if (nb.gundem) out.push({ title: "Gündem", text: nb.gundem });
  return out;
}
