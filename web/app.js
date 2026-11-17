/**
 * file: app.js
 * importable: yok
 *
 * Sayfa açılınca profil listesini, sonra seçili profilin durumunu, mumları,
 * işlemleri ve günlüğü sunucudan alır, ardından api/<profil>/stream üzerinden
 * gelen canlı olaylarla her şeyi günceller.
 * Sol üstteki parite seçiciyle grafik ve grafik üstündeki çizgiler seçili
 * pariteye geçer. Tablolar ve günlük ise bütün pariteleri birlikte gösterir.
 *
 * description: Parite seçiciyi, grafiği, alt paneli ve günlüğü çizen arayüz kodu.
 */

// NOTE: Grafik kütüphanesi zamanları UTC olarak gösterir. Bütün zamanlara
// tarayıcının saat farkı eklenerek grafiğin yerel saatle görünmesi sağlanır.
// Grafiğe giden her zaman bu yüzden local() içinden geçmelidir.
//
// NOTE: Sunucu her parite için yalnızca dakikalık mum gönderir. Seçili aralık
// daha büyükse dakikalık güncelleme o aralığın son mumuna katılarak birleştirilir.
//
// NOTE: TP ve SL hücrelerindeki dolar tutarları sunucudaki hesap motorunun
// kurallarıyla aynı hesaplanır: TP maker komisyonu ile tam fiyatından, SL
// taker komisyonu ve kayma ile. Açılışta ödenen komisyon ve fonlama da düşülür.
//
// NOTE: Bütün adresler göreli istenir, başlarında eğik çizgi yoktur. Sayfa bir
// vekil sunucunun arkasında bir alt yolda açıldığında
// istekler de o alt yolun altına gider.
//
// NOTE: Opus'un kararı şemadaki Türkçe alan adlarıyla gelir: ozet, analiz,
// gorus, guven, not_defteri ve sonraki_cagri_dk. Bunlar kodun değil Opus'un
// kullandığı adlardır.

"use strict";

// --- Profiller ---

// Bütün profiller aynı programda çalışır. Profil listesi açılışta sunucudan
// alınır, her profilin verisi api/<profil>/ altındaki uçlardan istenir.
// Seçili profil adreste ?profile= olarak durur, böylece bir profilin
// bağlantısı doğrudan paylaşılabilir.
let PROFILES = [];
let profile = null;
const api = (path, opts) => fetch(`api/${profile.id}${path}`, opts);

// --- Biçimlendirme ---

const TZ_OFFSET = -new Date().getTimezoneOffset() * 60;
const local = (t) => t + TZ_OFFSET;

const decimals = (x) => (x >= 1000 ? 2 : x >= 10 ? 2 : 4);
const fmtNum = (x, d) => new Intl.NumberFormat("tr-TR", { minimumFractionDigits: d, maximumFractionDigits: d }).format(x);
const fmtPrice = (x) => fmtNum(x, decimals(Math.abs(x)));
const fmt2 = (x) => fmtNum(x, 2);
const fmtUsd = (x) => (x < 0 ? "−$" : "$") + fmt2(Math.abs(x));
const fmtSigned = (x) => (x > 0 ? "+" : x < 0 ? "−" : "") + "$" + fmt2(Math.abs(x));
const fmtPct = (x) => (x > 0 ? "+" : x < 0 ? "−" : "") + "%" + fmt2(Math.abs(x));
const fmtClock = (t) => new Date(t * 1000).toLocaleTimeString("tr-TR", { hour: "2-digit", minute: "2-digit" });
const fmtDate = (t) => new Date(t * 1000).toLocaleString("tr-TR", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
const fmtLev = (k) => `${Math.round(k * 100) / 100}x`;
const signClass = (x) => (x > 0 ? "positive" : x < 0 ? "negative" : "");
const sideSign = (side) => (side === "long" ? 1 : -1);

function duration(sec) {
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return `${sec} sn`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min} dk`;
  return `${Math.floor(min / 60)} sa ${min % 60} dk`;
}

function daysHours(sec) {
  sec = Math.max(0, sec);
  const d = Math.floor(sec / 86400);
  return d ? `${d} gün ${Math.floor((sec % 86400) / 3600)} sa` : duration(sec);
}

// Metin HTML'e eklenmeden önce kaçırılır. Opus'un yazdığı metin
// arayüze doğrudan girer, içinde etiket benzeri bir şey olabilir.
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

const $ = (id) => document.getElementById(id);

// Arayüzün bütün ikonları static/icons.svg'deki tek bir çizim setinden gelir.
// Emoji kullanılmaz, işletim sistemine göre farklı çizilip tasarımı bozuyorlar.
const icon = (name) => `<svg class="ic" aria-hidden="true"><use href="static/icons.svg#${name}"/></svg>`;

// --- Uygulama durumu ---

const INTERVAL_S = { "1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400 };

const ui = {
  symbol: "BTCUSDT",
  interval: "15m",
  acct: null,
  trades: { trades: [], orders: [], alerts: [] },
  journal: [],
  summary: {},
  lastBar: null,
  firstBarTime: 0,
  openCards: new Set(),
  details: new Map(),
  liveTools: [],
  focusTime: null,
  focusId: null,
  tab: "positions",
  connected: false,
  others: {},
  version: 0,
  source: null,
  layers: {},
  fullscreen: false,
};

const pair = (s) => ui.acct?.symbols?.[s] || { name: s, icon: "btc", slippage: 0.0005, qty_step: 0.001 };
const iconPath = (s) => `static/vendor/icons/${pair(s).icon}.svg`;
const pairLabel = (s) => `<img class="mini-icon" src="${iconPath(s)}" alt="">${esc(pair(s).name)}`;
const priceOf = (s) => ui.acct?.prices?.[s] ?? 0;

// --- Kâr ve zarar hesapları ---

function slip(s, price, buy) {
  const k = pair(s).slippage;
  return buy ? price * (1 + k) : price * (1 - k);
}

// Bir seviyeye gelindiğinde o miktarın net sonucunu hesaplar. tp ise maker
// komisyonla tam seviyeden, değilse taker komisyon ve kayma ile çıkılır.
// openFee ve funding kapanan miktarın payına düşen kısımdır.
function exitResult(s, side, entry, qty, level, tp, openFee, funding) {
  const a = ui.acct;
  const exit = tp ? level : slip(s, level, side === "short");
  const fee = qty * exit * (tp ? a.fee_maker : a.fee_taker);
  return (exit - entry) * qty * sideSign(side) - openFee - fee - funding;
}

function roundStep(s, q) {
  const step = pair(s).qty_step;
  return Math.floor(q / step + 1e-6) * step;
}

// Kademelerin her birinin kapatacağı miktarı bulur. Miktarı boş olan
// kademe kalanın tamamıdır. Bekleyen emirde miktar yerine yüzde vardır.
function levelQtys(s, levels, total) {
  let left = total;
  return levels.map((k, i) => {
    let q = k.qty !== undefined ? k.qty : null;
    if (q === null && k.qty === undefined) {
      const all = i === levels.length - 1 && levels.reduce((t, x) => t + x.pct, 0) >= 99.999;
      q = all ? null : roundStep(s, (total * k.pct) / 100);
    }
    const take = q === null ? left : Math.min(q, left);
    left -= take;
    return { ...k, closes: take };
  });
}

function tpCell(s, side, entry, qty, levels, openFee, funding) {
  if (!levels?.length) return `<td class="positive">—</td>`;
  const lines = levelQtys(s, levels, qty).map((k) => {
    const share = qty ? k.closes / qty : 0;
    const net = exitResult(s, side, entry, k.closes, k.price, true, openFee * share, funding * share);
    const pct = levels.length > 1 || k.pct < 100 ? ` <span class="level-pnl">%${fmtNum(k.pct, 0)}</span>` : "";
    return `<span class="level">${fmtPrice(k.price)}${pct} <span class="level-pnl">/ ${fmtSigned(net)}</span></span>`;
  });
  return `<td class="positive">${lines.join("")}</td>`;
}

function slCell(s, side, entry, qty, sl, trail, openFee, funding) {
  const trailText = trail ? ` <span class="badge b-gray">iz %${fmtNum(trail, 2)}</span>` : "";
  if (!sl) return `<td class="negative">${trail ? "Bekliyor" + trailText : "—"}</td>`;
  const net = exitResult(s, side, entry, qty, sl, false, openFee, funding);
  return `<td class="negative">${fmtPrice(sl)}${trailText} <span class="level-pnl">/ ${fmtSigned(net)}</span></td>`;
}

// --- Grafik ---

const COLOR = {
  green: "#22c55e", red: "#f05252", purple: "#8b7cf6", blue: "#5ea8ff", orange: "#f59e0b",
  entry: "#aab4c4", grid: "#1a202b",
};

const chart = LightweightCharts.createChart($("chart"), {
  autoSize: true,
  layout: { background: { type: "solid", color: "#10141c" }, textColor: "#8792a4", fontFamily: getComputedStyle(document.body).fontFamily, fontSize: 11 },
  grid: { vertLines: { color: COLOR.grid }, horzLines: { color: COLOR.grid } },
  rightPriceScale: { borderColor: "#1f2633", scaleMargins: { top: 0.12, bottom: 0.08 } },
  timeScale: { borderColor: "#1f2633", timeVisible: true, secondsVisible: false, rightOffset: 8 },
  crosshair: {
    mode: LightweightCharts.CrosshairMode.Normal,
    vertLine: { color: "#3a4357", labelBackgroundColor: "#2a3242" },
    horzLine: { color: "#3a4357", labelBackgroundColor: "#2a3242" },
  },
  localization: { priceFormatter: (p) => fmtPrice(p), locale: "tr-TR" },
});

const candleSeries = chart.addCandlestickSeries({
  upColor: "#26a69a", downColor: "#ef5350", borderVisible: false,
  wickUpColor: "#26a69a", wickDownColor: "#ef5350", priceLineColor: "#5b6476",
});

// Tam ekran düğmesi grafiğin sağ alt köşesindeki boş kareye oturur.
// Karenin genişliği fiyat ekseninin, yüksekliği saat ekseninin boyutudur.
// İkisi de fiyat etiketlerine ve pencereye göre değiştiği için grafik her
// boyut değiştirdiğinde ve her yeni veri çizildiğinde yeniden ölçülür.
function fitCorner() {
  const area = document.querySelector(".chart-area");
  area.style.setProperty("--corner-w", `${chart.priceScale("right").width()}px`);
  area.style.setProperty("--corner-h", `${chart.timeScale().height()}px`);
}
new ResizeObserver(() => requestAnimationFrame(fitCorner)).observe($("chart"));

let priceLines = [];
let tradeSeries = [];

// Parite ya da aralık değişince eski mumlar hemen geçersiz sayılır. Yeni
// veri gelene kadar canlı güncellemeler grafiğe yazılmaz, yoksa yeni
// paritenin fiyatı eski paritenin son mumuna eklenir ve fiyat ekseni bozulur.
async function loadCandles() {
  const wanted = `${ui.symbol}|${ui.interval}`;
  ui.lastBar = null;
  ui.loading = true;
  const candles = await (await fetch(`api/candles?interval=${ui.interval}&symbol=${ui.symbol}`)).json();
  // Yükleme sürerken parite ya da aralık değiştiyse eski cevap atılır.
  if (wanted !== `${ui.symbol}|${ui.interval}`) return;
  const last = candles[candles.length - 1]?.c ?? 1;
  const d = decimals(last);
  candleSeries.applyOptions({ priceFormat: { type: "price", precision: d, minMove: 1 / 10 ** d } });
  const bars = candles.map((m) => ({ time: local(m.t), open: m.o, high: m.h, low: m.l, close: m.c }));
  // Seri önce boşaltılır. Kütüphanenin 4.2.3 sürümünde dolu seriye, yanında
  // işlem çizgileri varken setData verilince sonraki update() olmayan bir
  // barı boyamaya çalışıyor, Value is null hatası atıyor ve grafik bir daha
  // çizilmiyordu. Profil değiştirince sayfa yenilenene kadar grafik boş kalıyordu.
  candleSeries.setData([]);
  candleSeries.setData(bars);
  // Fiyat ekseni elle sürüklendiyse kütüphane otomatik ölçeklemeyi kapatır
  // ve eksen eski paritenin fiyat bandında kalır. Her yüklemede yeniden açılır.
  candleSeries.priceScale().applyOptions({ autoScale: true });
  ui.lastBar = bars[bars.length - 1] || null;
  ui.loading = false;
  ui.firstBarTime = bars.length ? bars[0].time : 0;
  drawMarkers();
  chart.timeScale().scrollToRealTime();
  requestAnimationFrame(fitCorner);
}

// Dakikalık güncelleme seçili aralığın kovasına yerleştirilir. Kova son
// barla aynıysa bar genişletilir, yeniyse yeni bar açılır.
function updateCandle(m) {
  if (ui.loading) return;
  const sec = INTERVAL_S[ui.interval];
  const bucket = local(Math.floor(m.t / sec) * sec);
  const last = ui.lastBar;
  let bar;
  if (last && last.time === bucket) {
    bar = { ...last, high: Math.max(last.high, m.h), low: Math.min(last.low, m.l), close: m.c };
  } else if (!last || bucket > last.time) {
    bar = { time: bucket, open: m.o, high: m.h, low: m.l, close: m.c };
  } else {
    return;
  }
  candleSeries.update(bar);
  ui.lastBar = bar;
}

const align = (t) => {
  const sec = INTERVAL_S[ui.interval];
  return local(Math.floor(t / sec) * sec);
};

const EXIT_LABEL = { tp: "TP", sl: "SL", liquidation: "LİKİDASYON", agent: "Kapat", period_end: "Bitiş", admin: "Yönetici" };

// İşaretler, kapanan işlemlerin giriş ile çıkışını bağlayan kesikli çizgiler
// ve fiyat çizgileri her veri değişiminde seçili parite için baştan kurulur.
function drawMarkers() {
  const markers = [];
  const first = ui.firstBarTime;

  tradeSeries.forEach((s) => chart.removeSeries(s));
  tradeSeries = [];

  for (const t of ui.trades.trades.filter((x) => x.symbol === ui.symbol)) {
    const long = t.side === "long";
    const entry = align(t.entry_time);
    if (shown("markers") && entry >= first && !t.parent_id) {
      markers.push({
        time: entry, position: long ? "belowBar" : "aboveBar",
        color: long ? COLOR.green : COLOR.red, shape: long ? "arrowUp" : "arrowDown",
        text: `#${t.id} ${long ? "LONG" : "SHORT"} ${fmtLev(t.leverage)}`,
      });
    }
    if (t.status !== "closed") continue;

    const net = t.pnl - t.fee;
    const exit = align(t.exit_time);
    if (shown("markers") && exit >= first) {
      markers.push({
        time: exit, position: long ? "aboveBar" : "belowBar",
        color: net >= 0 ? COLOR.green : COLOR.red, shape: "circle",
        text: `#${t.parent_id || t.id} ${t.parent_id ? "Kısmi " : ""}${EXIT_LABEL[t.exit_reason] || "Kapat"} ${fmtSigned(net)}`,
      });
    }
    if (shown("tradeLines") && exit > entry && entry >= first) {
      const s = chart.addLineSeries({
        color: net >= 0 ? "rgba(34,197,94,.55)" : "rgba(240,82,82,.55)",
        lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed,
        priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
      });
      s.setData([{ time: entry, value: t.entry_price }, { time: exit, value: t.exit_price }]);
      tradeSeries.push(s);
    }
  }

  if (shown("opus") && ui.focusTime && align(ui.focusTime) >= first) {
    markers.push({ time: align(ui.focusTime), position: "aboveBar", color: COLOR.purple, shape: "square", text: "Opus" });
  }

  markers.sort((a, b) => a.time - b.time);
  candleSeries.setMarkers(markers);
  drawPriceLines();
}

function drawPriceLines() {
  priceLines.forEach((c) => candleSeries.removePriceLine(c));
  priceLines = [];
  const L = LightweightCharts.LineStyle;
  const add = (price, color, title, style = L.Dashed) =>
    priceLines.push(candleSeries.createPriceLine({ price, color, title, lineStyle: style, lineWidth: 1, axisLabelVisible: true }));
  const a = ui.acct;
  if (!a) return;
  const here = (x) => x.symbol === ui.symbol;

  for (const p of a.positions.filter(here)) {
    if (shown("entry")) add(p.entry_price, COLOR.entry, `#${p.id} ${p.side.toUpperCase()} ${fmtLev(p.leverage)}`, L.Solid);
    if (shown("tp")) p.tp_levels.forEach((k, i) => add(k.price, COLOR.green, `#${p.id} TP${p.tp_levels.length > 1 ? i + 1 : ""}`));
    if (shown("sl") && p.sl) add(p.sl, COLOR.red, `#${p.id} SL${p.trail_pct ? " iz" : ""}`);
    if (shown("liq")) add(p.liq_price, COLOR.orange, `#${p.id} Lik.`, L.Dotted);
  }
  for (const o of shown("orders") ? a.orders.filter(here) : []) {
    add(o.trigger_price, COLOR.blue, `#${o.id} ${o.side.toUpperCase()} ${o.leverage}x`);
    o.tp_levels.forEach((k, i) => add(k.price, "rgba(34,197,94,.7)", `#${o.id} TP${o.tp_levels.length > 1 ? i + 1 : ""}`, L.SparseDotted));
    if (o.sl) add(o.sl, "rgba(240,82,82,.7)", `#${o.id} SL`, L.SparseDotted);
  }
  for (const al of shown("alerts") ? a.alerts.filter(here) : []) {
    add(al.price, COLOR.purple, `Alarm #${al.id}`, L.Dotted);
  }
}

// --- Katmanlar ve tam ekran ---

// Grafikte gösterilebilen her şey bir katmandır. Seçim tarayıcıda
// hatırlanır, hiç seçim yapılmadıysa hepsi açıktır. Gruplar menüdeki
// başlıklardır, swatch alttaki gösterge şeridindeki çizgi biçimidir.
const LAYERS = [
  ["Açık pozisyonlar", [
    ["entry", "Giriş fiyatı", "l-entry"],
    ["tp", "TP seviyeleri", "l-tp"],
    ["sl", "SL ve iz süren stop", "l-sl"],
    ["liq", "Likidasyon fiyatı", "l-liq"],
  ]],
  ["Bekleyenler", [
    ["orders", "Emirler, TP ve SL'leriyle", "l-order"],
    ["alerts", "Fiyat alarmları", "l-alert"],
  ]],
  ["Geçmiş", [
    ["markers", "İşlem giriş ve çıkış işaretleri", null],
    ["tradeLines", "Kapanmış işlemlerin giriş çıkış çizgisi", null],
    ["opus", "Günlükte seçilen Opus kararı", null],
  ]],
];

const shown = (layer) => ui.layers[layer] !== false;

function drawLayers() {
  $("layerMenu").innerHTML = LAYERS.map(([group, items]) => `<h4>${group}</h4>` + items.map(([id, label, swatch]) =>
    `<button class="layer-item" role="menuitemcheckbox" data-layer="${id}" aria-checked="${shown(id)}">
      <span class="layer-tick">${icon("check")}</span>${label}${swatch ? `<i class="swatch ${swatch}"></i>` : ""}
    </button>`).join("")).join("");
  document.querySelectorAll("#legend [data-layer]").forEach((el) => { el.hidden = !shown(el.dataset.layer); });
}

function openLayers() {
  drawLayers();
  $("layerMenu").hidden = false;
  $("layerButton").setAttribute("aria-expanded", "true");
}

function closeLayers() {
  $("layerMenu").hidden = true;
  $("layerButton").setAttribute("aria-expanded", "false");
}

// Grafik paneli sayfanın üstüne sabitlenir. Grafik kütüphanesi autoSize ile
// kabının boyutunu izlediği için yeniden çizim kendiliğinden olur.
function toggleFullscreen() {
  ui.fullscreen = !ui.fullscreen;
  document.querySelector(".chart-panel").classList.toggle("fullscreen", ui.fullscreen);
  document.body.classList.toggle("chart-fullscreen", ui.fullscreen);
  const b = $("fullscreenButton");
  b.setAttribute("aria-pressed", String(ui.fullscreen));
  b.title = ui.fullscreen ? "Tam ekrandan çık" : "Grafiği tam ekran yap";
  b.innerHTML = icon(ui.fullscreen ? "minimize" : "maximize");
}

// Seçilen zaman grafiğin ortasına gelecek şekilde görünür aralık kaydırılır.
function focusChart(t) {
  ui.focusTime = t;
  drawMarkers();
  const sec = INTERVAL_S[ui.interval];
  const mid = align(t);
  chart.timeScale().setVisibleRange({ from: mid - sec * 60, to: mid + sec * 60 });
}

// --- Parite seçici ---

function selectPair(s) {
  if (!ui.acct?.symbols?.[s]) return;
  ui.symbol = s;
  try { localStorage.setItem("symbol", s); } catch (_) { /* depolama kapalıysa önemli değil */ }
  $("pairName").textContent = pair(s).name;
  $("pairIcon").src = iconPath(s);
  ui.focusTime = null;
  closeList();
  loadCandles();
  drawTopbar();
}

function drawList() {
  const a = ui.acct;
  if (!a) return;
  $("pairList").innerHTML = Object.keys(a.symbols).map((s) => {
    const o = ui.summary?.[s];
    const pos = a.positions.filter((p) => p.symbol === s).length;
    const sub = [pair(s).note, pos ? `${pos} açık pozisyon` : ""].filter(Boolean).join(" · ") || `en fazla ${pair(s).max_leverage}x`;
    return `<button class="pair-item${s === ui.symbol ? " selected" : ""}" data-symbol="${s}" role="option">
      <img src="${iconPath(s)}" alt="">
      <div><div class="name">${esc(pair(s).name)}</div><div class="sub">${esc(sub)}</div></div>
      <div class="right"><div>${fmtPrice(priceOf(s))}</div>
        <div class="${signClass(o?.change_pct ?? 0)}">${o ? fmtPct(o.change_pct) : ""}</div></div>
    </button>`;
  }).join("");
}

async function openList() {
  drawList();
  $("pairList").hidden = false;
  $("pairPicker").setAttribute("aria-expanded", "true");
  try {
    ui.summary = await (await fetch("api/summary")).json();
    drawList();
  } catch (_) { /* özet gelmezse liste yüzdesiz kalır */ }
}

function closeList() {
  $("pairList").hidden = true;
  $("pairPicker").setAttribute("aria-expanded", "false");
}

// --- Üst çubuk ---

function drawTopbar() {
  const a = ui.acct;
  if (!a) return;
  const pz = a.positions;
  const upnl = pz.reduce((t, p) => t + (priceOf(p.symbol) - p.entry_price) * p.qty * sideSign(p.side), 0);
  const equity = a.cash + pz.reduce((t, p) => t + p.margin, 0) + upnl;
  const ret = (equity / a.start_balance - 1) * 100;

  $("price").textContent = fmtPrice(priceOf(ui.symbol));
  $("equity").textContent = fmtUsd(equity);
  $("return").textContent = fmtPct(ret);
  $("return").className = "stat-value " + signClass(ret);
  $("upnl").textContent = pz.length ? fmtSigned(upnl) : "—";
  $("upnl").className = "stat-value " + (pz.length ? signClass(upnl) : "");
  const st = a.stats;
  $("tradeCount").innerHTML = st.trade_count
    ? `${st.trade_count} <span style="color:var(--muted);font-weight:500;font-size:12px">· ${st.wins} kazanç</span>`
    : "0";
  if (a.start_time) {
    $("subtitle").textContent = a.end_reason
      ? `Opus Trading Masası · dönem bitti`
      : `Opus Trading Masası · bitişe ${daysHours(a.end_time - Date.now() / 1000)}`;
  }
  document.title = `${pair(ui.symbol).name} ${fmtPrice(priceOf(ui.symbol))} · ${fmtPct(ret)} · ${profile.name}`;
}

// Bir profilin hesap durumundan dilimin sınıfını ve adın altındaki kısa
// satırı çıkarır. Seçili profil canlı akıştan, diğerleri yoklamadan gelir.
function statusInfo(a, connected, toolCount) {
  if (!connected) return ["offline", "Kapalı"];
  if (!a) return ["", "Bağlanıyor"];
  if (a.end_reason) return ["", "Dönem bitti"];
  if (a.shutting_down) return ["closing", a.busy ? "Son kararını veriyor" : "Kapanıyor"];
  if (a.busy) return ["thinking", toolCount ? `Düşünüyor · ${toolCount} araç` : "Düşünüyor"];
  return ["waiting", a.next_call ? `Sonraki ${fmtClock(a.next_call)}` : "—"];
}

// Seçici kapalıyken seçili profili gösterir, liste açıkken bütün profilleri.
// Liste her saniye baştan kurulmaz, yalnızca açıkken ve durum değişince
// yazılır. Yoksa fareye basıp bırakma arasında düğme değişir ve tıklama kaybolur.
function profileRow(p) {
  const sel = p.id === profile.id;
  const d = sel ? { a: ui.acct, connected: ui.connected } : ui.others[p.id] || { a: null, connected: false };
  const [cls, sub] = statusInfo(d.a, d.connected, sel ? ui.liveTools.length : 0);
  return { cls, html: `<img class="claude-icon" src="static/vendor/icons/claude.svg" alt="">
    <div class="profile-text"><div class="profile-name">${esc(p.name)}</div><div class="profile-sub">${esc(sub)}</div></div>` };
}

function drawProfiles() {
  const picker = $("profilePicker");
  const cur = profileRow(profile);
  const html = `${cur.html}<span class="caret">▾</span>`;
  if (picker.dataset.html !== html) {
    picker.innerHTML = html;
    picker.dataset.html = html;
  }
  picker.className = `profile-picker ${cur.cls}`;
  picker.title = `${profile.name} hesabı gösteriliyor`;

  const list = $("profileList");
  if (list.hidden) return;
  const items = PROFILES.map((p) => {
    const r = profileRow(p);
    return `<button class="profile-item ${r.cls}${p.id === profile.id ? " selected" : ""}" data-profile="${p.id}"
      role="option" aria-selected="${p.id === profile.id}">${r.html}<span class="check">✓</span></button>`;
  }).join("");
  if (list.dataset.html !== items) {
    list.innerHTML = items;
    list.dataset.html = items;
  }
}

function openProfiles() {
  $("profileList").hidden = false;
  $("profilePicker").setAttribute("aria-expanded", "true");
  drawProfiles();
}

function closeProfiles() {
  $("profileList").hidden = true;
  $("profilePicker").setAttribute("aria-expanded", "false");
}

// Seçili olmayan profillerin durumu birkaç saniyede bir sorulur. Onların
// canlı akışına bağlanılmaz, kutudaki tek satır için gerek yok.
async function pollOthers() {
  await Promise.all(PROFILES.filter((p) => p.id !== profile.id).map(async (p) => {
    try {
      const r = await fetch(`api/${p.id}/state`, { signal: AbortSignal.timeout(4000) });
      ui.others[p.id] = { a: await r.json(), connected: true };
    } catch (_) {
      ui.others[p.id] = { a: null, connected: false };
    }
  }));
  drawProfiles();
}

// Profil değişince seçili profilin bütün verisi bırakılır ve akış yeni
// profilin uçlarına bağlanır. version, eski profilden geç gelen cevapların
// yeni profilin ekranına yazılmasını engeller.
function selectProfile(id) {
  if (id === profile.id) return;
  profile = PROFILES.find((p) => p.id === id);
  rememberProfile();
  ui.version++;
  ui.source?.close();
  Object.assign(ui, {
    acct: null, trades: { trades: [], orders: [], alerts: [] }, journal: [], liveTools: [],
    focusTime: null, focusId: null, connected: true,
  });
  ui.openCards.clear();
  ui.details.clear();
  drawProfiles();
  loadAll();
  connectStream();
  pollOthers();
}

// --- Alt panel ---

const sideBadge = (side, lev) => `<span class="badge b-${side}">${side.toUpperCase()}${lev ? " " + fmtLev(lev) : ""}</span>`;
const pnlTitle = `title="Komisyon, kayma ve fonlama düşülmüş net sonuç"`;
const condText = (c) => (c === "below" ? "Düşüp değince" : "Yükselip değince");

function drawBottom() {
  const a = ui.acct;
  if (!a) return;
  const closed = ui.trades.trades.filter((t) => t.status === "closed");
  $("posCount").textContent = a.positions.length;
  $("orderCount").textContent = a.orders.length;
  $("alertCount").textContent = a.alerts.length;
  $("historyCount").textContent = closed.length;
  const box = $("tabContent");
  const empty = (text) => (box.innerHTML = `<div class="empty">${text}</div>`);

  if (ui.tab === "positions") {
    if (!a.positions.length) return empty("Açık pozisyon yok.<br>Opus pozisyon açınca burada canlı olarak görünecek.");
    box.innerHTML = `<table><thead><tr>
      <th>#</th><th>Parite</th><th>Yön</th><th>Marjin</th><th>Büyüklük</th><th>Giriş</th><th>Güncel</th>
      <th ${pnlTitle}>TP / net</th><th ${pnlTitle}>SL / net</th><th>Likidasyon</th><th>Fonlama</th><th>Açık K/Z</th><th>Açılış</th>
      </tr></thead><tbody>${a.positions.map((p) => {
        const f = priceOf(p.symbol);
        const pnl = (f - p.entry_price) * p.qty * sideSign(p.side);
        return `<tr data-time="${p.entry_time}" data-symbol="${p.symbol}" style="cursor:pointer">
        <td>${p.id}</td><td>${pairLabel(p.symbol)}</td><td>${sideBadge(p.side, p.leverage)}</td>
        <td>${fmtUsd(p.margin)}</td><td>${fmtUsd(p.qty * p.entry_price)}</td>
        <td>${fmtPrice(p.entry_price)}</td><td>${fmtPrice(f)}</td>
        ${tpCell(p.symbol, p.side, p.entry_price, p.qty, p.tp_levels, p.fee, p.funding)}
        ${slCell(p.symbol, p.side, p.entry_price, p.qty, p.sl, p.trail_pct, p.fee, p.funding)}
        <td style="color:var(--orange)">${fmtPrice(p.liq_price)}</td>
        <td class="${signClass(-p.funding)}">${p.funding ? fmtSigned(-p.funding) : "—"}</td>
        <td class="${signClass(pnl)}"><b>${fmtSigned(pnl)}</b> <span style="opacity:.75">${fmtPct((pnl / p.margin) * 100)}</span></td>
        <td>${fmtDate(p.entry_time)}</td></tr>`;
      }).join("")}</tbody></table>`;
  } else if (ui.tab === "orders") {
    if (!a.orders.length) return empty("Bekleyen emir yok.");
    box.innerHTML = `<table><thead><tr>
      <th>#</th><th>Parite</th><th>Yön</th><th>Tetik</th><th>Koşul</th><th>Uzaklık</th><th>Marjin</th>
      <th ${pnlTitle}>TP / net</th><th ${pnlTitle}>SL / net</th><th>Geçerlilik</th><th>Verildi</th>
      </tr></thead><tbody>${a.orders.map((o) => {
        const entry = o.maker ? o.trigger_price : slip(o.symbol, o.trigger_price, o.side === "long");
        const qty = roundStep(o.symbol, (o.margin * o.leverage) / entry);
        const openFee = qty * entry * (o.maker ? a.fee_maker : a.fee_taker);
        const f = priceOf(o.symbol);
        return `<tr data-symbol="${o.symbol}">
        <td>${o.id}</td><td>${pairLabel(o.symbol)}</td><td>${sideBadge(o.side, o.leverage)}</td>
        <td style="color:var(--blue)">${fmtPrice(o.trigger_price)}</td>
        <td>${condText(o.condition)} <span class="badge b-gray">${o.maker ? "maker" : "taker"}</span></td>
        <td>${fmtPct(((o.trigger_price - f) / f) * 100)}</td><td>${fmtUsd(o.margin)}</td>
        ${tpCell(o.symbol, o.side, entry, qty, o.tp_levels, openFee, 0)}
        ${slCell(o.symbol, o.side, entry, qty, o.sl, o.trail_pct, openFee, 0)}
        <td>${o.expires_at ? fmtDate(o.expires_at) : "—"}</td>
        <td>${fmtDate(o.created_at)}</td></tr>`;
      }).join("")}</tbody></table>`;
  } else if (ui.tab === "alerts") {
    if (!a.alerts.length) return empty("Kurulu alarm yok.<br>Opus bir fiyat seviyesini izlemek istediğinde burada görünecek.");
    box.innerHTML = `<table><thead><tr>
      <th>#</th><th>Parite</th><th>Seviye</th><th>Koşul</th><th>Uzaklık</th><th>Opus'un notu</th><th>Kuruldu</th>
      </tr></thead><tbody>${a.alerts.map((al) => {
        const f = priceOf(al.symbol);
        return `<tr data-symbol="${al.symbol}">
        <td>${al.id}</td><td>${pairLabel(al.symbol)}</td><td style="color:var(--purple)">${fmtPrice(al.price)}</td>
        <td>${condText(al.condition)}</td>
        <td>${fmtPct(((al.price - f) / f) * 100)}</td>
        <td style="white-space:normal;min-width:240px">${esc(al.note || "—")}</td><td>${fmtDate(al.created_at)}</td></tr>`;
      }).join("")}</tbody></table>`;
  } else if (ui.tab === "history") {
    if (!closed.length) return empty("Henüz kapanan işlem yok.");
    const reason = {
      tp: ["TP", "b-long"], sl: ["SL", "b-short"], liquidation: ["Likidasyon", "b-orange"],
      agent: ["Opus kapattı", "b-purple"], period_end: ["Dönem sonu", "b-gray"], admin: ["Yönetici kapattı", "b-orange"],
    };
    box.innerHTML = `<table><thead><tr>
      <th>#</th><th>Parite</th><th>Yön</th><th>Marjin</th><th>Giriş</th><th>Çıkış</th><th>Neden</th>
      <th ${pnlTitle}>TP / net</th><th ${pnlTitle}>SL / net</th><th ${pnlTitle}>Net K/Z</th><th>Komisyon</th><th>Süre</th><th>Kapanış</th>
      </tr></thead><tbody>${[...closed].sort((x, y) => y.exit_time - x.exit_time).map((t) => {
        const [label, cls] = reason[t.exit_reason] || ["—", "b-gray"];
        // Kapanış komisyonu toplamdan çıkarılınca açılışta ödenen kısım kalır.
        // Likidasyonda kapanış komisyonu alınmaz.
        const closeRate = t.exit_reason === "tp" ? a.fee_maker : a.fee_taker;
        const openFee = t.exit_reason === "liquidation" ? t.fee : t.fee - t.qty * t.exit_price * closeRate;
        const levels = t.tp_levels.map((k) => ({ ...k, qty: null }));
        const net = t.pnl - t.fee;
        return `<tr data-time="${t.entry_time}" data-symbol="${t.symbol}" style="cursor:pointer">
        <td>${t.parent_id ? `${t.parent_id} <span class="badge b-gray">kısmi</span>` : t.id}</td>
        <td>${pairLabel(t.symbol)}</td><td>${sideBadge(t.side, t.leverage)}</td><td>${fmtUsd(t.margin)}</td>
        <td>${fmtPrice(t.entry_price)}</td><td>${fmtPrice(t.exit_price)}</td>
        <td><span class="badge ${cls}">${label}</span></td>
        ${tpCell(t.symbol, t.side, t.entry_price, t.qty, levels.length ? [{ price: levels[levels.length - 1].price, pct: 100 }] : [], openFee, t.funding)}
        ${slCell(t.symbol, t.side, t.entry_price, t.qty, t.sl, t.trail_pct, openFee, t.funding)}
        <td class="${signClass(net)}"><b>${fmtSigned(net)}</b></td>
        <td style="color:var(--muted)">${fmtUsd(t.fee)}</td>
        <td>${duration(t.exit_time - t.entry_time)}</td><td>${fmtDate(t.exit_time)}</td></tr>`;
      }).join("")}</tbody></table>`;
  } else {
    const n = a.notebook;
    box.innerHTML = n
      ? `<pre class="notebook">${esc(n)}</pre>`
      : `<div class="empty">Not defteri boş.<br>Opus her çağrıda kendine buraya not bırakır.</div>`;
  }
}

// --- Günlük ---

const TRIGGER = {
  start: ["Başlangıç", "b-purple", "diamond"],
  timer: ["Zamanlayıcı", "b-gray", "clock"],
  retry: ["Yeniden deneme", "b-gray", "refresh"],
  restart: ["Açılış", "b-gray", "refresh"],
  offline_events: ["Açılış", "b-orange", "refresh"],
  shutdown: ["Kapanış", "b-orange", "power"],
  order_filled: ["Emir gerçekleşti", "b-blue", "bolt"],
  order_failed: ["Emir açılamadı", "b-blue", "warning"],
  order_expired: ["Emir süresi doldu", "b-blue", "hourglass"],
  alert: ["Alarm", "b-purple", "bell"],
  tp: ["TP", "b-long", "target"],
  sl: ["SL", "b-short", "shield"],
  liquidation: ["Likidasyon", "b-orange", "warning"],
};
const VIEW = {
  yukselis: ["▲ Yükseliş", "b-long"],
  dusus: ["▼ Düşüş", "b-short"],
  yatay: ["◆ Yatay", "b-gray"],
  belirsiz: ["? Belirsiz", "b-gray"],
};

// Günlük, Opus kararlarıyla sistem olaylarını tek bir zaman akışında
// birleştirir. Sistem olayları ayrıca saklanmaz, işlem, emir ve alarm
// kayıtlarından ve kapalı dönemlerden türetilir.
function systemEvents() {
  const out = [];
  const name = (s) => pair(s).name;
  for (const t of ui.trades.trades) {
    if (t.order_id && !t.parent_id) {
      out.push({ time: t.entry_time, kind: "order", text: `#${t.order_id} emri gerçekleşti · ${name(t.symbol)} ${t.side.toUpperCase()} ${fmtLev(t.leverage)} @ ${fmtPrice(t.entry_price)}` });
    }
    if (t.status === "closed" && t.exit_reason !== "agent") {
      const n = { tp: "TP gerçekleşti", sl: "SL tetiklendi", liquidation: "Likidasyon", period_end: "Dönem sonu kapanışı", admin: "Yönetici kapattı" }[t.exit_reason];
      out.push({ time: t.exit_time, kind: t.exit_reason, text: `#${t.parent_id || t.id} ${name(t.symbol)} ${n} · ${fmtPrice(t.exit_price)} · ${fmtSigned(t.pnl - t.fee)}` });
    }
  }
  for (const o of ui.trades.orders.filter((x) => x.status === "expired")) {
    out.push({ time: o.resolved_at, kind: "order", text: `#${o.id} ${name(o.symbol)} emrinin süresi doldu` });
  }
  for (const al of ui.trades.alerts.filter((x) => x.status === "triggered")) {
    out.push({ time: al.resolved_at, kind: "alert", text: `#${al.id} ${name(al.symbol)} ${fmtPrice(al.price)} alarmı tetiklendi` });
  }
  for (const k of ui.acct?.offline_periods || []) {
    out.push({ time: k.end, kind: "offline", text: `Sistem kapalıydı · ${fmtDate(k.start)} – ${fmtDate(k.end)} · ${daysHours(k.end - k.start)} · ${k.planned ? "planlı" : "beklenmedik"}` });
  }
  return out;
}

// Günlükteki sistem olaylarının ikonları, olay türüne göre.
const EVENT_ICON = { order: "bolt", tp: "target", sl: "shield", liquidation: "warning", period_end: "flag",
  alert: "bell", offline: "power", admin: "flag" };

// Piyasa araçlarının ve profile özel araçların Opus'un çağırdığı fonksiyon
// adları arayüzde okunur adlara çevrilir. unit, adet parametresinin neyi saydığıdır. Opus'a giden
// hiçbir şey değişmez, bu yalnızca günlükteki görünüştür.
const TOOLS = {
  mumlar: ["Mumlar", "mum"],
  gostergeler_hesapla: ["Göstergeler", null],
  gosterge: ["Gösterge", "değer"],
  emir_defteri: ["Emir defteri", null],
  fonlama_gecmisi: ["Fonlama geçmişi", "kayıt"],
  acik_pozisyon_gecmisi: ["Açık pozisyon geçmişi", "kayıt"],
  long_short_oranlari: ["Long short oranları", "kayıt"],
  seviyeler: ["Destek ve direnç", "mum"],
  hacim_profili: ["Hacim profili", "mum"],
  korelasyon: ["Korelasyon", "mum"],
  likidasyonlar: ["Likidasyonlar", null],
  gecmis_kararlarim: ["Geçmiş kararları", "karar"],
  gecmis_islemlerim: ["Kapanmış işlemleri", "işlem"],
  cikis_karnesi: ["Çıkış karnesi", "işlem"],
  ny_acilisi: ["NY açılışı", null],
  acilis_gecmisi: ["Açılış geçmişi", null],
};

const INTERVAL_TEXT = {
  "1m": "1 dakikalık", "3m": "3 dakikalık", "5m": "5 dakikalık", "15m": "15 dakikalık", "30m": "30 dakikalık",
  "1h": "1 saatlik", "2h": "2 saatlik", "4h": "4 saatlik", "6h": "6 saatlik", "8h": "8 saatlik", "12h": "12 saatlik",
  "1d": "günlük", "3d": "3 günlük", "1w": "haftalık", "1M": "aylık",
};

const symbolText = (s) => String(s).toUpperCase().replace(/USDT$/, "/USDT");

// Araç adları arayüzde kısa ve anlaşılır bir etikete çevrilir.
function toolLabel(t) {
  if (t.name === "WebSearch") return ["search", "Web araması"];
  if (t.name === "WebFetch") return ["page", "Sayfa okudu"];
  if (t.name === "Agent") return ["sparkle", "Haiku'ya araştırma"];
  const key = t.name.replace(/^piyasa\./, "");
  return ["chart", TOOLS[key]?.[0] || key];
}

// Bir piyasa aracının parametreleri okunur bir satıra çevrilir, örneğin
// BTC/USDT · 4 saatlik · 42 kayıt. Web araçlarında arama metni ya da adres
// olduğu gibi kalır.
function toolDetail(t) {
  const key = t.name.replace(/^piyasa\./, "");
  const x = t.input;
  if (!TOOLS[key] || !x) return t.title || "";
  const parts = [];
  if (x.sembol) parts.push(symbolText(x.sembol));
  if (x.semboller) parts.push(x.semboller.map((s) => symbolText(s).replace("/USDT", "")).join(", "));
  if (x.ad) parts.push([x.ad.toUpperCase(), ...(x.periyotlar || [])].join(" "));
  const iv = x.aralik || x.periyot;
  if (iv) parts.push(INTERVAL_TEXT[iv] || iv);
  if (x.adet && TOOLS[key][1]) parts.push(`${x.adet} ${TOOLS[key][1]}`);
  if (x.saat) parts.push(`son ${x.saat} saat`);
  if (x.gun) parts.push(`son ${x.gun} işlem günü`);
  if (x.derinlik) parts.push(`${x.derinlik} kademe`);
  if (x.kademe) parts.push(`${x.kademe} fiyat kademesi`);
  if (x.pencere) parts.push(`pencere ${x.pencere}`);
  if (x.atla) parts.push(`en yeni ${x.atla} atlandı`);
  if (x.analiz_dahil) parts.push("analizleriyle");
  return parts.join(" · ");
}

function toolSummary(tools) {
  if (!tools?.length) return "";
  const count = { search: 0, page: 0, sparkle: 0, chart: 0 };
  tools.filter((t) => !t.sub).forEach((t) => count[toolLabel(t)[0]]++);
  const haiku = tools.filter((t) => t.sub).length;
  const parts = [];
  if (count.search) parts.push(`${icon("search")}${count.search} arama`);
  if (count.page) parts.push(`${icon("page")}${count.page} sayfa`);
  if (count.sparkle) parts.push(`${icon("sparkle")}${count.sparkle} Haiku görevi${haiku ? `, ${haiku} adım` : ""}`);
  if (count.chart) parts.push(`${icon("chart")}${count.chart} piyasa verisi`);
  return `<div class="tool-summary">${parts.map((x) => `<span>${x}</span>`).join("")}</div>`;
}

function toolList(tools) {
  return `<ol class="tool-list">${tools.map((t) => {
    const [glyph, name] = toolLabel(t);
    return `<li class="${t.sub ? "sub" : ""}">${icon(glyph)}<span><b>${t.sub ? "Haiku · " : ""}${esc(name)}</b> ${esc(toolDetail(t))}</span></li>`;
  }).join("")}</ol>`;
}

function cardHtml(j) {
  const [tName, tCls, tIcon] = TRIGGER[j.trigger] || [j.trigger, "b-gray", "info"];
  const open = ui.openCards.has(j.id);
  const focus = ui.focusId === j.id ? " focus" : "";
  const button = `<button class="detail-button">${open ? "Gizle ▴" : "Ayrıntı ▾"}</button>`;

  if (j.error) {
    return `<article class="card error${focus}" data-id="${j.id}" data-time="${j.time}">
      <div class="card-top"><span class="card-time">${fmtDate(j.time)}</span>
      <span class="badge ${tCls}">${icon(tIcon)}${tName}</span><span class="badge b-short">Hata</span></div>
      <p class="card-summary" style="color:#f3a6a6">${esc(j.error)}</p>
      <div class="card-bottom">Tekrar denenecek: ${j.next_call ? fmtClock(j.next_call) : "—"}${button}</div>
      ${open ? detailHtml(j.id) : ""}</article>`;
  }

  const k = j.decision;
  const [vName, vCls] = VIEW[k.gorus] || [k.gorus, "b-gray"];
  const actions = j.action_results?.length
    ? j.action_results.map((r) => `<li class="${r.ok ? "ok" : "rejected"}">${esc(r.message)}</li>`).join("")
    : `<li class="none">Eylem yok, bekliyor.</li>`;

  return `<article class="card${focus}" data-id="${j.id}" data-time="${j.time}">
    <div class="card-top">
      <span class="card-time">${fmtDate(j.time)}</span>
      <span class="badge ${tCls}">${icon(tIcon)}${tName}</span>
      <span class="badge ${vCls}">${vName}</span>
      <span class="confidence" title="Güven">${k.guven}<span class="confidence-bar"><i style="width:${Math.max(0, Math.min(100, k.guven))}%"></i></span></span>
    </div>
    <p class="card-summary">${esc(k.ozet)}</p>
    ${toolSummary(j.tools)}
    <ul class="actions">${actions}</ul>
    <div class="card-bottom">
      <span>${j.duration_s} sn</span>
      <span>· sonraki ${fmtClock(j.next_call)}</span>
      ${button}
    </div>
    ${open ? detailHtml(j.id) : ""}
  </article>`;
}

function detailHtml(id) {
  const d = ui.details.get(id);
  if (!d) return `<div class="detail"><span class="loading">Yükleniyor…</span></div>`;
  const k = d.decision;
  const section = (title, body) => `<section><h4>${title}</h4>${body}</section>`;
  const parts = [section("Neden çağrıldı", `<div class="text">${esc(d.trigger_detail)}</div>`)];
  if (k) {
    if (d.tools?.length) parts.push(section(`Kullandığı araçlar · ${d.tools.length}`, toolList(d.tools)));
    parts.push(section("Analiz", `<div class="text">${esc(k.analiz)}</div>`));
    parts.push(section("Not defterine yazdığı", `<div class="text">${esc(k.not_defteri) || "—"}</div>`));
    parts.push(section("İstediği bekleme", `<div class="text">${k.sonraki_cagri_dk} dakika</div>`));
  }
  if (d.prompt) parts.push(`<details><summary>Opus'a giden tam metin</summary><pre>${esc(d.prompt)}</pre></details>`);
  if (d.raw_response) {
    let raw = d.raw_response;
    try { raw = JSON.stringify(JSON.parse(raw).structured_output, null, 2); } catch (_) { /* ham haliyle gösterilir */ }
    parts.push(`<details><summary>Ham cevap</summary><pre>${esc(raw)}</pre></details>`);
  }
  if (d.cost_usd != null) {
    parts.push(`<div class="small">API liste fiyatıyla karşılığı $${d.cost_usd.toFixed(3)}. Abonelikten ayrıca ücret alınmaz.</div>`);
  }
  return `<div class="detail">${parts.join("")}</div>`;
}

function drawJournal() {
  const items = [
    ...ui.journal.map((j) => ({ time: j.time, card: j })),
    ...systemEvents(),
  ].sort((a, b) => b.time - a.time);

  const busy = ui.acct?.busy;
  let html = busy
    ? `<div class="thinking-card"><div class="thinking-title"><span class="opus-dot" style="background:var(--purple)"></span>${ui.acct.shutting_down ? "Opus kapanış öncesi son kararını veriyor…" : "Opus düşünüyor, piyasayı inceliyor…"}</div>
       ${ui.liveTools.length ? toolList(ui.liveTools.slice(-8)) : ""}</div>`
    : "";
  if (!items.length && !busy) {
    html = `<div class="empty" style="height:auto;padding:48px 16px">Henüz karar yok.<br>Opus'un ilk kararı birazdan burada görünecek.</div>`;
  }
  html += items.map((o) => o.card
    ? cardHtml(o.card)
    : `<div class="system-event ${o.kind}" data-time="${o.time}"><span>${fmtClock(o.time)}</span>${icon(EVENT_ICON[o.kind] || "info")}<span>${esc(o.text)}</span></div>`).join("");

  const list = $("journal");
  const scroll = list.scrollTop;
  list.innerHTML = html;
  list.scrollTop = scroll;
  $("journalCount").textContent = `${ui.journal.filter((j) => !j.error).length} karar`;
}

async function toggleDetail(id) {
  if (ui.openCards.has(id)) {
    ui.openCards.delete(id);
  } else {
    ui.openCards.add(id);
    if (!ui.details.has(id)) {
      drawJournal();
      ui.details.set(id, await (await api(`/journal/${id}`)).json());
    }
  }
  drawJournal();
}

// --- Olaylar ---

$("pairPicker").addEventListener("click", (e) => {
  e.stopPropagation();
  $("pairList").hidden ? openList() : closeList();
});
$("pairList").addEventListener("click", (e) => {
  const b = e.target.closest("[data-symbol]");
  if (b) selectPair(b.dataset.symbol);
});
document.addEventListener("click", (e) => {
  if (!e.target.closest(".pair-box")) closeList();
  if (!e.target.closest(".profile-box")) closeProfiles();
  if (!e.target.closest(".layer-box")) closeLayers();
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  closeList(); closeProfiles(); closeLayers();
  if (ui.fullscreen) toggleFullscreen();
});

$("layerButton").addEventListener("click", (e) => {
  e.stopPropagation();
  $("layerMenu").hidden ? openLayers() : closeLayers();
});
// Menü her tıklamada yeniden çizildiği için tıklanan öğe sayfadan kopar ve
// belgedeki genel dinleyici onu menünün dışında sanıp menüyü kapatırdı.
$("layerMenu").addEventListener("click", (e) => {
  e.stopPropagation();
  const b = e.target.closest("[data-layer]");
  if (!b) return;
  ui.layers[b.dataset.layer] = !shown(b.dataset.layer);
  try { localStorage.setItem("layers", JSON.stringify(ui.layers)); } catch (_) { /* depolama kapalıysa önemli değil */ }
  drawLayers();
  drawMarkers();
});
$("fullscreenButton").addEventListener("click", toggleFullscreen);

$("journal").addEventListener("click", (e) => {
  const card = e.target.closest(".card");
  if (e.target.closest(".detail-button")) {
    toggleDetail(Number(card.dataset.id));
    return;
  }
  if (e.target.closest(".detail")) return;
  const item = e.target.closest("[data-time]");
  if (!item) return;
  ui.focusId = card ? Number(card.dataset.id) : null;
  focusChart(Number(item.dataset.time));
  drawJournal();
});

// Tablodaki bir satıra tıklanınca grafik o satırın paritesine geçer ve
// zamanı varsa o ana gider.
$("tabContent").addEventListener("click", async (e) => {
  const row = e.target.closest("tr[data-symbol]");
  if (!row) return;
  if (row.dataset.symbol !== ui.symbol) {
    selectPair(row.dataset.symbol);
    await new Promise((r) => setTimeout(r, 600));
  }
  if (row.dataset.time) focusChart(Number(row.dataset.time));
});

$("intervals").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b || b.dataset.interval === ui.interval) return;
  document.querySelectorAll("#intervals button").forEach((x) => x.classList.toggle("selected", x === b));
  ui.interval = b.dataset.interval;
  try { localStorage.setItem("interval", ui.interval); } catch (_) { /* depolama kapalıysa önemli değil */ }
  loadCandles();
});

$("profilePicker").addEventListener("click", (e) => {
  e.stopPropagation();
  $("profileList").hidden ? openProfiles() : closeProfiles();
});
$("profileList").addEventListener("click", (e) => {
  const b = e.target.closest("[data-profile]");
  if (!b) return;
  closeProfiles();
  selectProfile(b.dataset.profile);
});

$("tabs").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  document.querySelectorAll("#tabs button").forEach((x) => x.classList.toggle("selected", x === b));
  ui.tab = b.dataset.tab;
  drawBottom();
});

// --- Canlı akış ---

function connectStream() {
  const source = new EventSource(`api/${profile.id}/stream`);
  ui.source = source;
  source.onopen = async () => {
    ui.connected = true;
    // Bağlantı koptuğu sırada kaçan her şey yeniden yüklenir.
    await loadAll();
  };
  source.onerror = () => {
    ui.connected = false;
    drawProfiles();
  };
  source.addEventListener("candle", (e) => {
    const m = JSON.parse(e.data);
    if (!ui.acct) return;
    ui.acct.prices[m.s] = m.c;
    if (m.s === ui.symbol) updateCandle(m);
    drawTopbar();
    if (ui.tab !== "history" && ui.tab !== "notebook") drawBottom();
  });
  source.addEventListener("state", (e) => {
    const next = JSON.parse(e.data);
    // Yeni bir çağrı başlarken önceki çağrının canlı araç listesi temizlenir.
    if (next.busy && !ui.acct?.busy) ui.liveTools = [];
    ui.acct = next;
    drawTopbar(); drawProfiles(); drawBottom(); drawPriceLines(); drawJournal();
  });
  source.addEventListener("trades", (e) => {
    ui.trades = JSON.parse(e.data);
    drawMarkers(); drawBottom(); drawJournal();
  });
  source.addEventListener("tool", (e) => {
    ui.liveTools.push(JSON.parse(e.data));
    drawProfiles(); drawJournal();
  });
  source.addEventListener("journal", (e) => {
    const j = JSON.parse(e.data);
    ui.journal = [j, ...ui.journal.filter((x) => x.id !== j.id)];
    drawJournal();
  });
  // Kapanış bütün programı kapsar, bir profil kapandıysa hepsi kapanıyordur.
  source.addEventListener("closed", () => {
    source.close();
    $("closedScreen").hidden = false;
  });
}

async function loadAll() {
  const version = ui.version;
  const [a, t, j] = await Promise.all(["/state", "/trades", "/journal"].map((u) => api(u).then((r) => r.json())));
  if (version !== ui.version) return;
  ui.acct = a;
  ui.trades = t;
  ui.journal = j;
  if (!a.symbols[ui.symbol]) ui.symbol = Object.keys(a.symbols)[0];
  $("pairName").textContent = pair(ui.symbol).name;
  $("pairIcon").src = iconPath(ui.symbol);
  // Hesap hemen çizilir, grafik bin mum beklediği için arkadan gelir.
  // Yoksa sayfa ilk açılışta birkaç saniye boş görünüyor.
  drawTopbar(); drawProfiles(); drawBottom(); drawJournal();
  await loadCandles();
}

// Seçili profil hem adrese hem tarayıcıya yazılır. Adresteki profil
// paylaşılan bağlantılar için, tarayıcıdaki ise adressiz açılışlar içindir.
function rememberProfile() {
  const url = new URL(location.href);
  url.searchParams.set("profile", profile.id);
  history.replaceState(null, "", url);
  try { localStorage.setItem("profile", profile.id); } catch (_) { /* depolama kapalıysa önemli değil */ }
}

// --- Başlangıç ---

async function boot() {
  PROFILES = await (await fetch("api/profiles")).json();
  let stored = null;
  try {
    const interval = localStorage.getItem("interval");
    if (interval && INTERVAL_S[interval]) {
      ui.interval = interval;
      document.querySelectorAll("#intervals button").forEach((x) => x.classList.toggle("selected", x.dataset.interval === interval));
    }
    ui.symbol = localStorage.getItem("symbol") || ui.symbol;
    stored = localStorage.getItem("profile");
    ui.layers = JSON.parse(localStorage.getItem("layers") || "{}") || {};
  } catch (_) { /* depolama kapalıysa varsayılanlar kullanılır */ }
  drawLayers();
  const wanted = new URLSearchParams(location.search).get("profile") || stored;
  profile = PROFILES.find((p) => p.id === wanted) || PROFILES[0];
  rememberProfile();

  // Veri akış bağlantısını beklemeden hemen yüklenir. Akış açılınca bir kez
  // daha yüklenir ki iki istek arasında kaçan bir şey kalmasın.
  setInterval(drawProfiles, 1000);
  setInterval(pollOthers, 5000);
  pollOthers();
  ui.connected = true;
  loadAll();
  connectStream();
}

boot();
