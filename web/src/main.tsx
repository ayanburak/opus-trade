/**
 * file: main.tsx
 * importable: yok
 *
 * Herkese açık sayfanın giriş noktası. Uygulamayı sayfaya bağlar.
 *
 * description: Sayfanın başlangıcı.
 */

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./shared/base.css";
import "./app.css";

createRoot(document.getElementById("root")!).render(<StrictMode><App /></StrictMode>);
