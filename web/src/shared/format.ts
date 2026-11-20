/**
 * file: format.ts
 * importable: parseNum, num, money, signed, pct, clock, dayClock, when, ago, duration
 *
 * Sayıların ve zamanların Türkçe gösterimi ile formdaki sayıların okunması.
 * Gösterimde binlik ayıracı nokta, ondalık virgüldür. Formda kullanıcı
 * ondalığı nokta ya da virgülle yazabilir. Saatler her zaman Türkiye
 * saatiyle yazılır.
 *
 * description: Arayüzlerin ortak sayı ve zaman biçimleri.
 */

// NOTE: parseNum tek bir nokta ya da virgülü her zaman ondalık sayar. Eski
// arayüz "1.234" yazısını binlik sanıp 1234 okuyordu, XRP gibi küçük
// fiyatlarda bu yanlış emre yol açar. Formlar sayıyı zaten binlik ayıracı
// olmadan doldurur. İkisi birlikte geçiyorsa sondaki ondalıktır, öndekiler
// binliktir. Aynı işaret birden çok geçiyorsa binlik ayıracıdır.

const TZ = "Europe/Istanbul";

/**
 * Formdaki bir sayıyı okur.
 * @param text Kullanıcının yazdığı metin.
 * @returns Sayı, boş ya da geçersizse null.
 */
export function parseNum(text: string): number | null {
  let s = text.trim().replace(/\s/g, "").replace("−", "-");
  if (!s) return null;
  const dot = s.lastIndexOf("."), comma = s.lastIndexOf(",");
  if (dot >= 0 && comma >= 0) {
    const dec = Math.max(dot, comma);
    s = s.slice(0, dec).replace(/[.,]/g, "") + "." + s.slice(dec + 1);
  } else {
    const sep = dot >= 0 ? "." : ",";
    const count = s.split(sep).length - 1;
    s = count > 1 ? s.split(sep).join("") : s.replace(",", ".");
  }
  if (!/^-?\d+(\.\d+)?$/.test(s) && !/^-?\.\d+$/.test(s)) return null;
  return Number(s);
}

/** Sayıyı Türkçe biçimde yazar, ondalık hane sayısı verilmezse fiyatın büyüklüğüne göre seçilir. */
export function num(x: number | null | undefined, digits?: number): string {
  if (x == null || Number.isNaN(x)) return "—";
  const d = digits ?? (Math.abs(x) >= 1000 ? 1 : Math.abs(x) >= 10 ? 2 : 4);
  return x.toLocaleString("tr-TR", { minimumFractionDigits: d, maximumFractionDigits: d }).replace("-", "−");
}

/** Dolar tutarı, iki hane. */
export function money(x: number | null | undefined): string {
  return x == null ? "—" : (x < 0 ? "−$" : "$") + num(Math.abs(x), 2);
}

/** İşaretli tutar, artıda + ile. */
export function signed(x: number | null | undefined, digits = 2): string {
  if (x == null) return "—";
  return (x > 0 ? "+" : x < 0 ? "−" : "") + num(Math.abs(x), digits);
}

/** İşaretli yüzde, Türkçedeki gibi % önde. */
export function pct(x: number | null | undefined): string {
  if (x == null || !Number.isFinite(x)) return "—";
  return (x > 0 ? "+" : x < 0 ? "−" : "") + "%" + num(Math.abs(x), 2);
}

/** Unix saniyesini SS:DD yazar. */
export function clock(t: number): string {
  return new Date(t * 1000).toLocaleTimeString("tr-TR", { hour: "2-digit", minute: "2-digit", timeZone: TZ });
}

/** Unix saniyesini GG.AA SS:DD yazar. */
// Tarayıcının Türkçe ayarı gün ve ayı tek başına yazarken eğik çizgi
// kullanabiliyor, bu yüzden parçalar ayrı alınıp noktayla birleştirilir.
export function dayClock(t: number): string {
  const parts = new Intl.DateTimeFormat("tr-TR", { day: "2-digit", month: "2-digit", timeZone: TZ })
    .formatToParts(new Date(t * 1000));
  const get = (type: string) => parts.find((x) => x.type === type)?.value ?? "";
  return `${get("day")}.${get("month")} ${clock(t)}`;
}

function day(t: number): string {
  return new Date(t * 1000).toLocaleDateString("en-CA", { timeZone: TZ });
}

/**
 * Bir anı bugüne göre yazar: bugün 03:51, yarın 03:51, dün 23:10, başka günse 22.11 03:51.
 * Gelecekteki bir çağrının saati başka bir güne düştüğünde gün de görünsün diye vardır.
 */
export function when(t: number, now: number = Date.now() / 1000): string {
  const d = day(t);
  if (d === day(now)) return `bugün ${clock(t)}`;
  if (d === day(now + 86400)) return `yarın ${clock(t)}`;
  if (d === day(now - 86400)) return `dün ${clock(t)}`;
  return dayClock(t);
}

/** Geçen süreyi kısa yazar: az önce, 5 dk önce, 3 sa önce, 2 gün önce. */
export function ago(t: number, now: number = Date.now() / 1000): string {
  const s = Math.max(0, now - t);
  if (s < 60) return "az önce";
  if (s < 3600) return `${Math.floor(s / 60)} dk önce`;
  if (s < 86400) return `${Math.floor(s / 3600)} sa önce`;
  return `${Math.floor(s / 86400)} gün önce`;
}

/** Süreyi kısa yazar: 45 sn, 12 dk, 3 sa 20 dk, 2 gün 4 sa. */
export function duration(s: number): string {
  s = Math.max(0, Math.round(s));
  if (s < 60) return `${s} sn`;
  const m = Math.floor(s / 60), h = Math.floor(m / 60), d = Math.floor(h / 24);
  if (h === 0) return `${m} dk`;
  if (d === 0) return m % 60 ? `${h} sa ${m % 60} dk` : `${h} sa`;
  return h % 24 ? `${d} gün ${h % 24} sa` : `${d} gün`;
}
