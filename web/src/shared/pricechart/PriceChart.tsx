/**
 * file: PriceChart.tsx
 * importable: PriceChart, Candles, Subscribe
 *
 * Herkese açık sayfanın ve panelin ortak fiyat grafiği, Lightweight Charts ile.
 * Mumlar sayfanın verdiği load ile yüklenir, canlı akıştaki candle olaylarıyla
 * son çubuk güncellenir. Pozisyonun girişi, TP kademeleri, SL ve likidasyonu,
 * bekleyen emirler ve alarmlar fiyat çizgisidir, kapanmış işlemlerin girişi ve
 * çıkışı mumların üstünde işarettir. Gizlenen katmanlar çizilmez.
 *
 * Grafik kendiliğinden salt okunurdur. Sayfa onDrag verirse TP, SL, emir ve
 * alarm çizgileri sürüklenebilir, onContext verirse sağ tık tıklanan fiyatı
 * bildirir. Grafik hiçbir işlemi kendisi göndermez.
 *
 * description: Ortak fiyat grafiği, çizgileri, işaretleri ve düzenleme olayları.
 */

// NOTE: Lightweight Charts fiyat çizgisini sürüklemeyi bilmez. Fare basılınca
// imlecin altında altı pikselden yakın sürüklenebilir bir çizgi aranır, varsa
// sürükleme süresince grafiğin kaydırması kapatılır ve çizgi fareyi izler.
// Bırakınca çizgi eski yerine döner, sayfa önerilen fiyatı ghost çizgisi
// olarak gösterir.
//
// NOTE: Canlı mum sayfanın yeniden çizilmesinden geçmez. Grafik subscribe ile
// akışı kendisi dinler, böylece saniyede bir gelen mum yalnızca grafiği
// günceller.

import { createChart, CrosshairMode, LineStyle, type IChartApi, type IPriceLine, type ISeriesApi,
         type SeriesMarker, type Time, type UTCTimestamp } from "lightweight-charts";
import { useEffect, useRef, useState, type MouseEvent } from "react";
import { chartTime, digits, intervalSeconds, mergeBar, roundPrice, type Bar } from "../chart";
import { num } from "../format";
import type { Alert, Candle, Order, Position, Trade } from "../types";
import { DRAGGABLE, LAYERS, LINE_DIM, dimColor, markers, priceLines, type Layer, type LayerSpec,
         type PriceLine } from "./layers";
import "./chart.css";

/** Seçili parite ve aralığın mumlarını yükler. */
export type Candles = (symbol: string, interval: string) => Promise<Candle[]>;

/** Canlı akışa abone olur, aboneliği kapatan fonksiyonu döndürür. */
export type Subscribe = (listener: (e: Record<string, unknown>) => void) => () => void;

interface Props {
  symbol: string;
  interval: string;
  load: Candles;
  subscribe: Subscribe;
  positions: Position[];
  orders: Order[];
  alerts: Alert[];
  trades: Trade[];
  hidden: ReadonlySet<Layer>;
  onPrice: (price: number) => void;
  /** Önerilen fiyat, onaylanana kadar kesik çizgiyle gösterilir. */
  ghost?: number | null;
  /** Sürüklenen çizgi ve bırakıldığı fiyat. Verilmezse çizgiler sürüklenmez. */
  onDrag?: (line: PriceLine, price: number) => void;
  /** Sağ tıklanan fiyat ve pencereye göre konumu. Verilmezse sağ tık tarayıcıya kalır. */
  onContext?: (price: number, x: number, y: number) => void;
}

const GRAB_PX = 6;

const STYLES: Record<LayerSpec["style"], LineStyle> = {
  solid: LineStyle.Solid, dotted: LineStyle.Dotted, dashed: LineStyle.Dashed, sparse: LineStyle.SparseDotted, marks: LineStyle.Solid,
};

function css(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

export function PriceChart({ symbol, interval, load, subscribe, positions, orders, alerts, trades, hidden, onPrice, ghost = null,
                             onDrag, onContext }: Props) {
  const box = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi>();
  const series = useRef<ISeriesApi<"Candlestick">>();
  const last = useRef<Bar | null>(null);
  const drawn = useRef<{ spec: PriceLine; line: IPriceLine }[]>([]);
  const ghostLine = useRef<IPriceLine>();
  const drag = useRef<{ spec: PriceLine; line: IPriceLine; price: number } | null>(null);
  const [first, setFirst] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [cursor, setCursor] = useState("crosshair");
  const props = useRef({ onPrice, onDrag, load });
  props.current = { onPrice, onDrag, load };

  // Grafik bir kez kurulur, parite ya da aralık değişince yalnızca veri yenilenir.
  useEffect(() => {
    const c = createChart(box.current!, {
      autoSize: true,
      layout: { background: { color: css("--card") }, textColor: css("--axis"), fontFamily: "JetBrains Mono, monospace", fontSize: 11 },
      grid: { vertLines: { color: css("--line-soft") }, horzLines: { color: css("--line-soft") } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: css("--line") },
      timeScale: { borderColor: css("--line"), timeVisible: true, secondsVisible: false },
      localization: { locale: "tr-TR", priceFormatter: (p: number) => num(p) },
    });
    series.current = c.addCandlestickSeries({
      upColor: css("--up"), downColor: css("--down"), wickUpColor: css("--up"), wickDownColor: css("--down"),
      borderVisible: false,
    });
    chart.current = c;
    return () => { c.remove(); chart.current = undefined; drawn.current = []; ghostLine.current = undefined; };
  }, []);

  useEffect(() => {
    let alive = true;
    setError(null);
    setLoading(true);
    props.current.load(symbol, interval).then((rows) => {
      if (!alive || !series.current) return;
      const bars = rows.map((c) => ({ time: c.time, open: c.open, high: c.high, low: c.low, close: c.close }));
      const p = bars.at(-1)?.close ?? 1;
      series.current.applyOptions({ priceFormat: { type: "price", precision: digits(p), minMove: 10 ** -digits(p) } });
      series.current.setData(bars.map((b) => ({ ...b, time: chartTime(b.time) as UTCTimestamp })));
      last.current = bars.at(-1) ?? null;
      setFirst(bars[0]?.time ?? null);
      if (last.current) props.current.onPrice(last.current.close);
      chart.current?.timeScale().scrollToRealTime();
    }).catch((e: Error) => alive && setError(e.message)).finally(() => alive && setLoading(false));
    return () => { alive = false; };
  }, [symbol, interval]);

  useEffect(() => subscribe((e) => {
    const c = e.candle as Candle | undefined;
    if (!c || c.symbol !== symbol || !series.current) return;
    const bar = mergeBar(last.current, c, intervalSeconds(interval));
    if (!bar) return;
    last.current = bar;
    series.current.update({ ...bar, time: chartTime(bar.time) as UTCTimestamp });
    props.current.onPrice(bar.close);
  }), [subscribe, symbol, interval]);

  // Hesap ya da filtre değişince çizgiler baştan çizilir. Sürükleme sürerken çizgiye dokunulmaz.
  useEffect(() => {
    const s = series.current;
    if (!s || drag.current) return;
    const card = css("--card");
    drawn.current.forEach((d) => s.removePriceLine(d.line));
    drawn.current = priceLines(symbol, positions, orders, alerts, hidden).map((spec) => {
      const layer = LAYERS.find((l) => l.key === spec.layer)!;
      return { spec, line: s.createPriceLine({ price: spec.price, color: dimColor(css(layer.color), card, LINE_DIM), lineWidth: 2,
                                               lineStyle: STYLES[layer.style], axisLabelVisible: true, title: spec.title }) };
    });
  }, [symbol, positions, orders, alerts, hidden]);

  useEffect(() => {
    const s = series.current;
    if (!s) return;
    if (ghostLine.current) s.removePriceLine(ghostLine.current);
    ghostLine.current = ghost == null ? undefined : s.createPriceLine({
      price: ghost, color: css("--link"), lineWidth: 1, lineStyle: LineStyle.LargeDashed, axisLabelVisible: true, title: "önerilen",
    });
  }, [ghost]);

  // İşaret çubuğun zamanına oturtulur, yoksa Lightweight Charts onu çizmez.
  useEffect(() => {
    const s = series.current;
    if (!s || first == null) return;
    const sec = intervalSeconds(interval);
    const tone = { up: css("--up"), down: css("--down") };
    s.setMarkers(hidden.has("marks") ? [] : markers(trades, symbol, first).map((m): SeriesMarker<Time> => ({
      time: chartTime(Math.floor(m.time / sec) * sec) as UTCTimestamp, position: m.position, shape: m.shape,
      color: tone[m.tone], text: m.text,
    })));
  }, [trades, symbol, interval, first, hidden]);

  const yOf = (e: MouseEvent) => e.clientY - box.current!.getBoundingClientRect().top;
  const near = (y: number) => {
    const s = series.current!;
    let best: { spec: PriceLine; line: IPriceLine } | null = null, dist = GRAB_PX;
    for (const d of drawn.current) {
      if (!DRAGGABLE.has(d.spec.layer)) continue;
      const cy = s.priceToCoordinate(d.spec.price);
      if (cy != null && Math.abs(cy - y) <= dist) { best = d; dist = Math.abs(cy - y); }
    }
    return best;
  };

  const down = (e: MouseEvent) => {
    if (e.button !== 0 || !series.current) return;
    const hit = near(yOf(e));
    if (!hit) return;
    drag.current = { ...hit, price: hit.spec.price };
    chart.current?.applyOptions({ handleScroll: false, handleScale: false });
  };
  const move = (e: MouseEvent) => {
    if (!series.current) return;
    const y = yOf(e);
    if (drag.current) {
      const p = series.current.coordinateToPrice(y);
      if (p != null) { drag.current.price = roundPrice(p); drag.current.line.applyOptions({ price: drag.current.price }); }
      return;
    }
    setCursor(near(y) ? "ns-resize" : "crosshair");
  };
  const up = () => {
    const d = drag.current;
    if (!d) return;
    drag.current = null;
    chart.current?.applyOptions({ handleScroll: true, handleScale: true });
    d.line.applyOptions({ price: d.spec.price });
    if (d.price !== d.spec.price) props.current.onDrag?.(d.spec, d.price);
  };
  const context = (e: MouseEvent) => {
    const p = series.current?.coordinateToPrice(yOf(e));
    if (p == null || !onContext) return;
    e.preventDefault();
    onContext(roundPrice(p), e.clientX, e.clientY);
  };

  const edit = onDrag ? { onMouseDown: down, onMouseMove: move, onMouseUp: up, onMouseLeave: up, style: { cursor } } : {};
  return (
    <div className="chart-wrap">
      {error && <div className="chart-error error">{error}</div>}
      {loading && !error && <div className="chart-loading">Mumlar yükleniyor…</div>}
      <div ref={box} className="chart" {...edit} onContextMenu={onContext ? context : undefined} role="img"
           aria-label={`${symbol} grafiği, pozisyon, emir ve alarm çizgileri ve işlem işaretleriyle`} />
    </div>
  );
}
