/**
 * file: fullscreen.tsx
 * importable: useFullscreen, Expand
 *
 * Grafiğin tam ekranı. Tarayıcının tam ekranı değildir: sayfa seçtiği kartı
 * sekmenin tamamına büyütür, bunun için full değerini kendi sınıfına bağlar.
 * Tam ekranda arkadaki sayfa kaymaz, Esc küçültür.
 *
 * description: Grafiğin sekmeyi kaplayan tam ekranı ve düğmesi.
 */

// NOTE: Esc'i önce açık bir menü kullanır. Menü Esc'i aldıysa preventDefault
// çağırır, tam ekran o tuşu yok sayar. Böylece ilk Esc menüyü, ikincisi tam
// ekranı kapatır.

import { useEffect, useState } from "react";

/** Tam ekranın durumu ve onu değiştiren fonksiyon. */
export function useFullscreen(): [boolean, () => void] {
  const [full, setFull] = useState(false);
  useEffect(() => {
    if (!full) return;
    document.documentElement.classList.add("locked");
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape" && !e.defaultPrevented) setFull(false); };
    window.addEventListener("keydown", esc);
    return () => { document.documentElement.classList.remove("locked"); window.removeEventListener("keydown", esc); };
  }, [full]);
  return [full, () => setFull((f) => !f)];
}

/** Tam ekran düğmesi. Açıklama satırının sağ ucunda, fiyat ekseninin altında durur. */
export function Expand({ full, onClick }: { full: boolean; onClick: () => void }) {
  const label = full ? "Küçült" : "Tam ekran";
  return (
    <button className="expand" aria-label={label} title={label} aria-pressed={full} onClick={onClick}>
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"
           strokeLinejoin="round" aria-hidden="true">
        <path d={full ? "M9 3v6H3M15 3v6h6M9 21v-6H3M15 21v-6h6" : "M3 9V3h6M21 9V3h-6M3 15v6h6M21 15v6h-6"} />
      </svg>
    </button>
  );
}
