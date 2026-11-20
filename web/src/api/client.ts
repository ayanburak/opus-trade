/**
 * file: client.ts
 * importable: ApiError, get, apiUrl
 *
 * Herkese açık API'ye sayfanın istekleri. Adresler sayfanın kendi kökünün
 * altındaki api/ klasörüne gider. Sunucuya ulaşılamazsa ya da sunucu 5xx
 * dönerse hata kodu program kapalı demektir, sayfa kapalı ekranını gösterir.
 *
 * description: Herkese açık API istemcisi.
 */

const BASE = import.meta.env.BASE_URL + "api";

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }

  /** Program ya da bağlantı kapalı mı. 404 gibi bir cevap programın çalıştığını gösterir. */
  get down(): boolean {
    return this.status === 0 || this.status >= 500;
  }
}

/** api/ altındaki bir yolun tam adresi. */
export function apiUrl(path: string): string {
  return BASE + path;
}

/**
 * Bir GET isteği yapar ve JSON cevabı döndürür.
 * @param path / ile başlayan yol, örneğin /profile.
 * @throws ApiError Sunucu hata döndürürse ya da ulaşılamazsa.
 */
export async function get<T>(path: string): Promise<T> {
  let r: Response;
  try {
    r = await fetch(apiUrl(path));
  } catch {
    throw new ApiError("Sunucuya ulaşılamadı.", 0);
  }
  const data = await r.json().catch(() => null);
  if (!r.ok) {
    const detail = data?.detail;
    throw new ApiError(typeof detail === "string" ? detail : `İstek başarısız oldu, kod ${r.status}.`, r.status);
  }
  return data as T;
}
