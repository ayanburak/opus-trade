/**
 * file: useHeightVar.ts
 * importable: useHeightVar
 *
 * Bir öğenin yüksekliğini ölçüp sayfanın kök öğesinde bir CSS değişkenine
 * yazar. Sayfalar yapışkan üst çubuğun yüksekliğini böyle bilir, altındaki
 * parçaları ekranın kalanına göre boyutlar. Çubuk dar ekranda kırılınca ölçü
 * kendiliğinden yenilenir.
 *
 * description: Öğe yüksekliğini CSS değişkenine yazan kanca.
 */

import { useEffect, useRef, type RefObject } from "react";

/** @param name Yazılacak CSS değişkeni, örneğin --top-h. */
export function useHeightVar<T extends HTMLElement>(name: string): RefObject<T> {
  const ref = useRef<T>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(() => document.documentElement.style.setProperty(name, `${el.offsetHeight}px`));
    ro.observe(el);
    return () => ro.disconnect();
  }, [name]);
  return ref;
}
