/**
 * file: live.ts
 * importable: movesAccount, gate
 *
 * Hesabın canlı fiyatla yenilenmesi. Açık kâr, özkaynak ve getiri çekirdekte
 * son fiyatla hesaplanır, arayüz onları kendisi hesaplamaz. Bu yüzden
 * pozisyonu olan bir paritenin mumu akıştan gelince hesap yeniden istenir,
 * gate de bunu belirli bir sıklıkla sınırlar. Herkese açık sayfa ve panel
 * aynı kuralı kullanır, yalnızca sıklıkları farklıdır.
 *
 * description: Canlı fiyatla hesap yenilemenin ortak kuralı.
 */

/** Akıştaki olay pozisyonu olan bir paritenin mumuysa true, yani hesabın açık kârını değiştirir. */
export function movesAccount(e: Record<string, unknown>, positions: { symbol: string }[]): boolean {
  const c = e.candle as { symbol?: string } | undefined;
  return !!c && positions.some((p) => p.symbol === c.symbol);
}

/**
 * Sıklık sınırı. Döndürdüğü fonksiyon son geçişten bu yana ms geçtiyse true döner ve sayacı sıfırlar.
 * Mumlar her saniye geldiği için sondaki güncelleme kaybolmaz, bir sonraki mum onu getirir.
 */
export function gate(ms: number, now: () => number = Date.now): () => boolean {
  let last = -Infinity;
  return () => {
    const t = now();
    if (t - last < ms) return false;
    last = t;
    return true;
  };
}
