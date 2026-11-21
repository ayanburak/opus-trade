/**
 * file: stream.ts
 * importable: StreamEvent, subscribe, useStream, useConnected
 *
 * Sayfanın canlı akışı. Bütün sekme için tek bir EventSource açılır, her
 * bileşen useStream ile kendi dinleyicisini ekler. Bağlantı koparsa tarayıcı
 * kendisi yeniden bağlanır, useConnected bağlantının durumunu verir.
 *
 * description: Yayındaki profilin canlı olaylarının sayfaya dağıtılması.
 */

// NOTE: Sunucu ziyaretçi sınırı dolunca 503 döner. EventSource 503'te yeniden
// denemeyi bırakır, bu yüzden hata olunca kaynak kapatılıp yarım dakika
// sonra baştan açılır.

import { useEffect, useRef, useSyncExternalStore } from "react";
import { apiUrl } from "./client";

export type StreamEvent = Record<string, unknown>;
type Listener = (e: StreamEvent) => void;

const RETRY_MS = 30_000;
const listeners = new Set<Listener>();
const watchers = new Set<() => void>();
let source: EventSource | null = null;
let connected = false;

function set(next: boolean): void {
  if (next === connected) return;
  connected = next;
  watchers.forEach((w) => w());
}

function open(): void {
  if (source) return;
  const s = new EventSource(apiUrl("/stream"));
  source = s;
  s.onopen = () => set(true);
  s.onmessage = (m) => {
    const e = JSON.parse(m.data) as StreamEvent;
    listeners.forEach((l) => l(e));
  };
  s.onerror = () => {
    set(false);
    if (s.readyState === EventSource.CLOSED) {
      source = null;
      setTimeout(open, RETRY_MS);
    }
  };
}

/** Canlı akışa bağlı olup olunmadığı. */
export function useConnected(): boolean {
  return useSyncExternalStore((cb) => (watchers.add(cb), () => watchers.delete(cb)), () => connected);
}

/**
 * Akıştaki her olayda listener'ı çağırır, akış kapalıysa açar.
 * @returns Aboneliği kapatan fonksiyon.
 */
export function subscribe(listener: Listener): () => void {
  open();
  listeners.add(listener);
  return () => void listeners.delete(listener);
}

/** Akıştaki her olayda handler'ı çağırır. Handler her çizimde değişse de abonelik bir kez açılır. */
export function useStream(handler: Listener): void {
  const ref = useRef(handler);
  ref.current = handler;
  useEffect(() => subscribe((e) => ref.current(e)), []);
}
