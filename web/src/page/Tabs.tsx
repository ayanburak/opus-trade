/**
 * file: Tabs.tsx
 * importable: Tabs
 *
 * Grafiğin altındaki sekmeler: açık pozisyonlar, bekleyen emirler, Opus'un
 * alarmları, kapanmış işlemler ve özkaynak eğrisi. Dar ekranda günlük de
 * buraya ilk sekme olarak gelir, journal verilirse gösterilir ve ilk açılan odur.
 *
 * description: Hesabın ve kayıtların sekmeli tabloları.
 */

import { createChart, LineStyle, type UTCTimestamp } from "lightweight-charts";
import { useEffect, useRef, useState, type ReactNode } from "react";
import type { Account } from "../api/types";
import { chartTime } from "../shared/chart";
import { dayClock, duration, money, num, signed } from "../shared/format";
import { REASONS } from "../shared/labels";
import type { EquityPoint, TpLevel, Trade } from "../shared/types";

type Tab = "journal" | "positions" | "orders" | "alerts" | "trades" | "equity";

const levels = (ls: TpLevel[]) =>
  ls.length ? ls.map((l) => (l.pct >= 100 ? num(l.price) : `${num(l.price)} %${num(l.pct, 0)}`)).join(" · ") : "—";

const Side = ({ side }: { side: string }) => <span className={`strong ${side === "long" ? "up" : "down"}`}>{side}</span>;

function Positions({ account }: { account: Account }) {
  const ps = account.state.positions;
  if (!ps.length) return <div className="empty">Açık pozisyon yok.</div>;
  return (
    <table className="grid" style={{ minWidth: 820 }}>
      <thead><tr><th>#</th><th>Parite</th><th className="num">Giriş</th><th className="num">Güncel</th><th>TP</th><th>SL</th>
        <th className="num">Likidasyon</th><th className="num">Açık K/Z</th></tr></thead>
      <tbody>
        {ps.map((p) => (
          <tr key={p.id}>
            <td className="mono">{p.number}</td>
            <td><Side side={p.side} /> {p.symbol} · {num(p.leverage, 0)}x · {num(p.qty, 4)}</td>
            <td className="num">{num(p.entry_price)}</td>
            <td className="num">{num(account.prices[p.symbol])}</td>
            <td className="mono small">{levels(p.tp_levels)}</td>
            <td className="mono small">{p.sl == null ? "—" : num(p.sl)}{p.trail_pct ? ` · iz %${num(p.trail_pct, 1)}` : ""}</td>
            <td className="num dim">{num(p.liq_price)}</td>
            <td className={`num ${p.open_pnl == null ? "" : p.open_pnl >= 0 ? "up" : "down"}`}>{signed(p.open_pnl)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Orders({ account }: { account: Account }) {
  const os = account.state.orders;
  if (!os.length) return <div className="empty">Bekleyen emir yok.</div>;
  return (
    <table className="grid" style={{ minWidth: 760 }}>
      <thead><tr><th>#</th><th>Parite</th><th className="num">Tetik</th><th className="num">Marjin</th><th>TP</th><th>SL</th>
        <th>Süre</th></tr></thead>
      <tbody>
        {os.map((o) => (
          <tr key={o.id}>
            <td className="mono">{o.number}</td>
            <td><Side side={o.side} /> {o.symbol} · {num(o.leverage, 0)}x</td>
            <td className="num">{num(o.trigger)}</td>
            <td className="num">{money(o.margin)}</td>
            <td className="mono small">{levels(o.tp_levels)}</td>
            <td className="mono small">{o.sl == null ? "—" : num(o.sl)}</td>
            <td className="small">{o.expires_at ? `${dayClock(o.expires_at)}'e kadar` : "süresiz"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Alerts({ account }: { account: Account }) {
  const as = account.alerts;
  if (!as.length) return <div className="empty">Kurulu alarm yok.</div>;
  return (
    <table className="grid" style={{ minWidth: 560 }}>
      <thead><tr><th>#</th><th>Parite</th><th className="num">Fiyat</th><th>Yön</th><th>Kuruldu</th><th>Not</th></tr></thead>
      <tbody>
        {as.map((a) => (
          <tr key={a.number}>
            <td className="mono">{a.number}</td>
            <td>{a.symbol}</td>
            <td className="num">{num(a.price)}</td>
            <td className="small">{a.below ? "düşüşte" : "yükselişte"}</td>
            <td className="small dim">{dayClock(a.created_at)}</td>
            <td className="small soft">{a.note || "—"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Trades({ trades }: { trades: Trade[] }) {
  if (!trades.length) return <div className="empty">Kapanmış işlem yok.</div>;
  return (
    <table className="grid" style={{ minWidth: 800 }}>
      <thead><tr><th>#</th><th>Parite</th><th className="num">Giriş</th><th className="num">Çıkış</th><th>Neden</th><th>Süre</th>
        <th>Kapanış</th><th className="num">Net</th></tr></thead>
      <tbody>
        {trades.map((t) => (
          <tr key={t.id}>
            <td className="mono">{t.number}</td>
            <td><Side side={t.side} /> {t.symbol} · {num(t.leverage, 0)}x</td>
            <td className="num">{num(t.entry_price)}</td>
            <td className="num">{num(t.exit_price)}</td>
            <td className="small">{REASONS[t.reason] ?? t.reason}</td>
            <td className="small">{duration(t.exit_time - t.entry_time)}</td>
            <td className="small dim">{dayClock(t.exit_time)}</td>
            <td className={`num ${t.net >= 0 ? "up" : "down"}`}>{signed(t.net)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// Tek seri olduğu için açıklama kutusu yoktur, sekmenin adı seriyi söyler.
// Kesikli çizgi başlangıç bakiyesidir.
function Equity({ points, start }: { points: EquityPoint[]; start: number | null }) {
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!box.current || points.length < 2) return;
    const s = getComputedStyle(document.documentElement);
    const v = (n: string) => s.getPropertyValue(n).trim();
    const c = createChart(box.current, {
      autoSize: true,
      layout: { background: { color: v("--card") }, textColor: v("--axis"), fontFamily: "JetBrains Mono, monospace", fontSize: 11 },
      grid: { vertLines: { visible: false }, horzLines: { color: v("--line-soft") } },
      rightPriceScale: { borderColor: v("--line") }, timeScale: { borderColor: v("--line"), timeVisible: true },
      localization: { locale: "tr-TR", priceFormatter: (p: number) => num(p, 2) },
    });
    const line = c.addLineSeries({ color: v("--up"), lineWidth: 2 });
    line.setData(points.map((p) => ({ time: chartTime(p.time) as UTCTimestamp, value: p.equity })));
    line.createPriceLine({ price: start ?? points[0].equity, color: v("--dim"), lineStyle: LineStyle.Dashed, lineWidth: 1,
                           axisLabelVisible: false, title: "başlangıç" });
    c.timeScale().fitContent();
    return () => c.remove();
  }, [points, start]);
  if (points.length < 2) return <div className="empty">Eğri için henüz yeterli kayıt yok.</div>;
  return <div ref={box} className="equity-chart" role="img" aria-label="Özkaynak eğrisi, kesikli çizgi başlangıç bakiyesi" />;
}

interface Props {
  account: Account;
  trades: Trade[];
  equity: EquityPoint[];
  start: number | null;
  journal?: ReactNode;
}

export function Tabs({ account, trades, equity, start, journal }: Props) {
  const [picked, setTab] = useState<Tab>(journal ? "journal" : "positions");
  const tab = picked === "journal" && !journal ? "positions" : picked;
  const tabs: [Tab, string, number | null][] = [
    ...(journal ? [["journal", "Günlük", null] as [Tab, string, null]] : []),
    ["positions", "Pozisyonlar", account.state.positions.length], ["orders", "Bekleyen emirler", account.state.orders.length],
    ["alerts", "Alarmlar", account.alerts.length], ["trades", "Geçmiş işlemler", trades.length], ["equity", "Özkaynak eğrisi", null],
  ];
  return (
    <section className="card tabs-card">
      <div className="tabbar" role="tablist" aria-label="Hesap">
        {tabs.map(([k, label, n]) => (
          <button key={k} role="tab" aria-selected={tab === k} onClick={() => setTab(k)}>
            {label}{n != null && <b className="count">{n}</b>}
          </button>
        ))}
      </div>
      <div className={tab === "journal" ? "" : "scroll-x"}>
        {tab === "journal" && journal}
        {tab === "positions" && <Positions account={account} />}
        {tab === "orders" && <Orders account={account} />}
        {tab === "alerts" && <Alerts account={account} />}
        {tab === "trades" && <Trades trades={trades} />}
        {tab === "equity" && <Equity points={equity} start={start} />}
      </div>
    </section>
  );
}
