/**
 * file: vite.config.ts
 * importable: yok
 *
 * Herkese açık sayfanın derleme ayarı. Sayfa bir alt adreste sunulacaksa o
 * adres derlemede OPUS_PUBLIC_BASE ile verilir, örneğin /site/. Verilmezse
 * sayfa kök adreste sunulur. base mutlaktır, böylece dosyalar her adreste
 * aynı yerden yüklenir.
 *
 * Geliştirirken npm run dev /api isteklerini yerelde çalışan opus-web'in
 * herkese açık soketine aktarır. Soketin yolu OPUS_WEB_SOCKET ile verilir.
 *
 * description: Herkese açık sayfanın Vite derleme ve geliştirme sunucusu ayarı.
 */

import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

const socket = process.env.OPUS_WEB_SOCKET;

export default defineConfig(({ command }) => ({
  base: command === "build" ? (process.env.OPUS_PUBLIC_BASE ?? "/") : "/",
  plugins: [react()],
  server: socket
    ? { proxy: { "/api": { target: { socketPath: socket } as never, changeOrigin: false } } }
    : undefined,
  test: { environment: "node" },
}));
