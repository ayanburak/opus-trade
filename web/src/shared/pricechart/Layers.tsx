/**
 * file: Layers.tsx
 * importable: Filter, Legend, useHiddenLayers
 *
 * Grafiğin katmanlarını gösteren iki parça ve seçimin saklanması. Filtrele
 * düğmesi açılır menüsüyle katmanları tek tek gizler. Açıklama satırı her
 * katmanın rengini ve çizgisini gösterir, gizli katman soluk görünür. İkisi de
 * layers.ts LAYERS listesinden çizilir. Seçim tarayıcıda saklanır.
 *
 * description: Grafiğin filtre menüsü, açıklama satırı ve filtre seçimi.
 */

// NOTE: Panel ve herkese açık sayfa aynı alan adında çalışır, tarayıcıdaki
// saklama alanını paylaşırlar. Her sayfa seçimini kendi anahtarıyla saklar.

import { useEffect, useRef, useState } from "react";
import { LAYERS, LINE_DIM, readHidden, type Layer, type LayerSpec } from "./layers";

/**
 * Gizli katmanlar ve onları değiştiren fonksiyon. Seçim tarayıcıda key altında saklanır. Tarayıcı izin vermezse
 * seçim yalnızca o ziyaret boyunca geçerli olur.
 */
export function useHiddenLayers(key: string): [Set<Layer>, (hidden: Set<Layer>) => void] {
  const [hidden, setHidden] = useState(() => {
    try { return readHidden(localStorage.getItem(key)); } catch { return readHidden(null); }
  });
  const change = (h: Set<Layer>) => {
    setHidden(h);
    try { localStorage.setItem(key, JSON.stringify([...h])); } catch { /* tarayıcı saklamaya izin vermiyor */ }
  };
  return [hidden, change];
}

// Örnek çizgi grafikteki çizgiyle aynı oranda soluktur, işaretler soluklaşmaz.
function Swatch({ layer }: { layer: LayerSpec }) {
  const pct = layer.style === "marks" ? 100 : Math.round((1 - LINE_DIM) * 100);
  return <i className={`swatch ${layer.style}`} style={{ color: `color-mix(in srgb, var(${layer.color}) ${pct}%, var(--card))` }} />;
}

interface Props {
  hidden: ReadonlySet<Layer>;
  onChange: (hidden: Set<Layer>) => void;
}

/** Filtrele düğmesi ve menüsü. Menü dışarı tıklanınca ya da Esc ile kapanır. */
export function Filter({ hidden, onChange }: Props) {
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const button = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    const outside = (e: PointerEvent) => { if (!box.current?.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("pointerdown", outside);
    return () => document.removeEventListener("pointerdown", outside);
  }, [open]);

  const toggle = (k: Layer) => {
    const next = new Set(hidden);
    if (next.has(k)) next.delete(k); else next.add(k);
    onChange(next);
  };
  const allShown = hidden.size === 0;

  // Esc yalnızca menüyü kapatır. preventDefault tam ekranın onu yok saymasını sağlar.
  return (
    <div className="filter" ref={box} onKeyDown={(e) => {
      if (e.key === "Escape" && open) { e.preventDefault(); setOpen(false); button.current?.focus(); }
    }}>
      <button ref={button} className={`btn filter-btn${allShown ? "" : " on"}`} aria-expanded={open} aria-controls="layer-menu"
              onClick={() => setOpen((o) => !o)}>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"
             strokeLinejoin="round" aria-hidden="true"><path d="M3 5h18l-7 8.5V19l-4 2v-7.5z" /></svg>
        Filtrele
        {!allShown && <span className="filter-count">{LAYERS.length - hidden.size}</span>}
      </button>
      {open && (
        <div id="layer-menu" className="filter-menu" role="group" aria-label="Grafikte görünenler">
          <label className="filter-all">
            <input type="checkbox" checked={allShown} onChange={() => onChange(allShown ? new Set(LAYERS.map((l) => l.key)) : new Set())} />
            Tümü
          </label>
          {LAYERS.map((l) => (
            <label key={l.key}>
              <input type="checkbox" checked={!hidden.has(l.key)} onChange={() => toggle(l.key)} />
              <Swatch layer={l} />
              {l.label}
            </label>
          ))}
        </div>
      )}
    </div>
  );
}

/** Grafiğin altındaki açıklama satırı. */
export function Legend({ hidden }: { hidden: ReadonlySet<Layer> }) {
  return (
    <>
      {LAYERS.map((l) => (
        <span key={l.key} className={hidden.has(l.key) ? "off" : undefined}>
          <Swatch layer={l} />{l.label}
        </span>
      ))}
    </>
  );
}
