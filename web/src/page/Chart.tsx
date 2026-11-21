/**
 * file: Chart.tsx
 * importable: Chart
 *
 * Sayfanın fiyat grafiği, Lightweight Charts ile, salt okunur. Mumlar
 * /candles'tan yüklenir, canlı akıştaki candle olaylarıyla son çubuk
 * güncellenir. Açık pozisyonun girişi, TP kademeleri, SL ve likidasyonu,
 * bekleyen emirler ve Opus'un alarmları fiyat çizgisidir. Kapanmış işlemlerin
 * girişi ve çıkışı mumların üstünde işarettir.
 *
 * description: Fiyat grafiği, işlem çizgileri ve işlem işaretleri.
 */

import { createChart, CrosshairMode, LineStyle, type IChartApi, type IPriceLine, type ISeriesApi,
         type SeriesMarker, type Time, type UTCTimestamp } from "lightweight-charts";
import { useEffect, useRef, useState } from "react";
import { get } from "../api/client";
import { useStream } from "../api/stream";
import { chartTime, digits, intervalSeconds, mergeBar, type Bar } from "../shared/chart";
import { num } from "../shared/format";
import type { Alert, Candle, Order, Position, Trade } from "../shared/types";
import { LAYERS, LINE_DIM, dimColor, markers, priceLines, type Layer, type LayerSpec } from "./model";

interface Props {
  symbol: string;
  interval: string;
  positions: Position[];
  orders: Order[];
  alerts: Alert[];
  trades: Trade[];
  hidden: ReadonlySet<Layer>;
  onPrice: (price: number) => void;
}

function css(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

const STYLES: Record<LayerSpec["style"], LineStyle> = {
  solid: LineStyle.Solid, dotted: LineStyle.Dotted, dashed: LineStyle.Dashed, sparse: LineStyle.SparseDotted, marks: LineStyle.Solid,
};

export function Chart({ symbol, interval, positions, orders, alerts, trades, hidden, onPrice }: Props) {
  const box = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi>();
  const series = useRef<ISeriesApi<"Candlestick">>();
  const last = useRef<Bar | null>(null);
  const drawn = useRef<IPriceLine[]>([]);
  const [first, setFirst] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const price = useRef(onPrice);
  price.current = onPrice;

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
    return () => { c.remove(); chart.current = undefined; drawn.current = []; };
  }, []);

  useEffect(() => {
    let alive = true;
    setError(null);
    setLoading(true);
    get<Candle[]>(`/candles?symbol=${symbol}&interval=${interval}&limit=500`).then((rows) => {
      if (!alive || !series.current) return;
      const bars = rows.map((c) => ({ time: c.time, open: c.open, high: c.high, low: c.low, close: c.close }));
      const p = bars.at(-1)?.close ?? 1;
      series.current.applyOptions({ priceFormat: { type: "price", precision: digits(p), minMove: 10 ** -digits(p) } });
      series.current.setData(bars.map((b) => ({ ...b, time: chartTime(b.time) as UTCTimestamp })));
      last.current = bars.at(-1) ?? null;
      setFirst(bars[0]?.time ?? null);
      if (last.current) price.current(last.current.close);
      chart.current?.timeScale().scrollToRealTime();
    }).catch((e: Error) => alive && setError(e.message)).finally(() => alive && setLoading(false));
    return () => { alive = false; };
  }, [symbol, interval]);

  useStream((e) => {
    const c = e.candle as Candle | undefined;
    if (!c || c.symbol !== symbol || !series.current) return;
    const bar = mergeBar(last.current, c, intervalSeconds(interval));
    if (!bar) return;
    last.current = bar;
    series.current.update({ ...bar, time: chartTime(bar.time) as UTCTimestamp });
    price.current(bar.close);
  });

  useEffect(() => {
    const s = series.current;
    if (!s) return;
    drawn.current.forEach((l) => s.removePriceLine(l));
    drawn.current = priceLines(symbol, positions, orders, alerts, hidden).map((line) => {
      const spec = LAYERS.find((l) => l.key === line.layer)!;
      return s.createPriceLine({ price: line.price, color: dimColor(css(spec.color), css("--card"), LINE_DIM), lineWidth: 2, lineStyle: STYLES[spec.style],
                                 axisLabelVisible: true, title: line.title });
    });
  }, [symbol, positions, orders, alerts, hidden]);

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

  return (
    <div className="chart-wrap">
      {error && <div className="chart-error error">{error}</div>}
      {loading && !error && <div className="chart-loading">Mumlar yükleniyor…</div>}
      <div ref={box} className="chart" role="img"
           aria-label={`${symbol} grafiği, pozisyon, emir ve alarm çizgileri ve işlem işaretleriyle`} />
    </div>
  );
}
