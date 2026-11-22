/**
 * file: Journal.tsx
 * importable: Journal
 *
 * Günlük: Opus'un kararları ve kararın dışında kapanan işlemler yeniden
 * eskiye tek akışta. Her karar kartında uyandırma nedeni, yapı ve rejim
 * rozeti, özet, araçların sayısı, eylemlerin sonucu, süre ve sonraki çağrı
 * vardır. Şablon 6'dan önceki kayıtlar rozet yerine eski görüş ve güven
 * etiketleriyle görünür. Ayrıntıda uyandırmalar, paritelerin yapısı ve
 * rejimi, analiz, not defteri ve kullanılan araçların listesi açılır. Opus düşünürken en üstte canlı
 * araç listesiyle bir kart durur.
 *
 * description: Kararların ve işlem olaylarının zaman akışı.
 */

import { useEffect, useState } from "react";
import { get } from "../api/client";
import type { Entry, LiveTool } from "../api/types";
import { dayClock, num, signed, when } from "../shared/format";
import { DIRECTIONS, REASONS, VIEWS } from "../shared/labels";
import { notebookParts } from "../shared/notebook";
import { RegimeBadge } from "../shared/regime/RegimeBadge";
import type { PairView, Structure, ToolCall, Trade } from "../shared/types";
import { timeline, toolCounts, toolLine, wakeText } from "./model";

const VIEW_TONE: Record<string, string> = { yukselis: "up", dusus: "down" };

function ToolList({ tools }: { tools: LiveTool[] }) {
  return (
    <ol className="tool-list">
      {tools.map((t, i) => {
        const { name, detail } = toolLine(t);
        return <li key={i} className={t.sub ? "sub" : ""}><b>{name}</b> <span className="dim">{detail}</span></li>;
      })}
    </ol>
  );
}

// Yeni kayıtta yapı rakamlarıyla ve rejimle, eski kayıtta görüş ve
// değerlendirmeyle yazılır.
function Pair({ symbol, x }: { symbol: string; x: PairView }) {
  if (!x.yapi) {
    return (
      <div className="pair-view"><b>{symbol}</b> <span className={VIEW_TONE[x.gorus ?? ""] ?? "muted"}>{VIEWS[x.gorus ?? ""] ?? x.gorus}</span>
        <div className="text">{x.degerlendirme}</div></div>
    );
  }
  const line = (name: string, l: Structure) =>
    `${name}: ${DIRECTIONS[l.yon] ?? l.yon}, tepeler ${l.tepeler.map((v) => num(v)).join(" → ")}, dipler ${l.dipler.map((v) => num(v)).join(" → ")}`;
  return (
    <div className="pair-view"><b>{symbol}</b>
      <div className="text">{line("Üst", x.yapi.ust)}{"\n"}{line("Alt", x.yapi.alt)}</div>
    </div>
  );
}

function Detail({ e }: { e: Entry }) {
  const [tools, setTools] = useState<ToolCall[] | null>(null);
  useEffect(() => { void get<ToolCall[]>(`/journal/${e.id}/tools`).then(setTools, () => setTools([])); }, [e.id]);
  const d = e.record.decision;
  const wakes = e.wakes.map(wakeText);
  return (
    <div className="detail">
      {wakes.length > 0 && (
        <section><h4>Neden çağrıldı</h4>
          {wakes.map((w, i) => <div key={i}><span className="soft">{w.label}</span>{w.detail && <span className="dim"> · {w.detail}</span>}</div>)}
        </section>
      )}
      {d?.pariteler && (
        <section><h4>Pariteler</h4>
          {Object.entries(d.pariteler).map(([s, x]) => <Pair key={s} symbol={s} x={x} />)}
        </section>
      )}
      {d?.analiz && <section><h4>Analiz</h4><div className="text">{d.analiz}</div></section>}
      {notebookParts(d?.not_defteri).length > 0 && (
        <section><h4>Not defterine yazdığı</h4>
          {notebookParts(d?.not_defteri).map((p) => <div key={p.title} className="nb-part"><span className="soft">{p.title}</span><div className="text">{p.text}</div></div>)}
        </section>
      )}
      <section><h4>Kullandığı araçlar{tools && tools.length > 0 && ` · ${tools.length}`}</h4>
        {!tools ? <div className="dim">Yükleniyor…</div> : !tools.length ? <div className="dim">Araç kullanmadı.</div>
          : <ToolList tools={tools} />}
      </section>
      {(e.record.model || d?.sonraki_cagri_dk != null) && (
        <div className="dim small">
          {e.record.model && `${e.record.model}${e.record.effort ? `, ${e.record.effort}` : ""}`}
          {d?.sonraki_cagri_dk != null && ` · istediği bekleme ${d.sonraki_cagri_dk} dk`}
        </div>
      )}
    </div>
  );
}

function Card({ e, now }: { e: Entry; now: number }) {
  const [open, setOpen] = useState(false);
  const d = e.record.decision;
  const wakes = [...new Set(e.wakes.map((w) => wakeText(w).label))];
  const counts = toolCounts(e.record.tools);
  return (
    <article className={`jcard${e.error ? " failed" : ""}`}>
      <div className="jtop">
        <span className="mono dim">{dayClock(e.time)}</span>
        {wakes.map((w) => <span key={w} className="tag">{w}</span>)}
        <RegimeBadge badge={e.record.badge} />
        {!e.record.badge && d?.gorus && <span className={`tag ${VIEW_TONE[d.gorus] ?? ""}`}>{VIEWS[d.gorus] ?? d.gorus}</span>}
        {!e.record.badge && d?.guven != null && (
          <span className="confidence" title="Kararına güveni">
            {d.guven}<i><b style={{ width: `${Math.max(0, Math.min(100, d.guven))}%` }} /></i>
          </span>
        )}
      </div>
      {e.error ? <p className="jsum down">{e.error}</p>
        : <p className="jsum">{e.record.summary || "Eylem yok, bekliyor."}</p>}
      {counts && <div className="dim small">{counts}</div>}
      {e.results.length > 0 && (
        <ul className="results">
          {e.results.map((r, i) => <li key={i} className={r.ok ? "ok" : "rejected"}>{r.message}</li>)}
        </ul>
      )}
      <div className="jbottom">
        {e.record.duration_s != null && <span>{num(e.record.duration_s, 0)} sn</span>}
        {e.next_wake && <span>sonraki {when(e.next_wake, now)}</span>}
        <button className="link push" aria-expanded={open} onClick={() => setOpen(!open)}>{open ? "Gizle" : "Ayrıntı"}</button>
      </div>
      {open && <Detail e={e} />}
    </article>
  );
}

function TradeRow({ t }: { t: Trade }) {
  return (
    <div className="jevent">
      <span className="mono dim">{dayClock(t.exit_time)}</span>
      <span>{t.symbol} {t.side} · {REASONS[t.reason] ?? t.reason} · {num(t.exit_price)}</span>
      <span className={`mono push ${t.net >= 0 ? "up" : "down"}`}>{signed(t.net)}</span>
    </div>
  );
}

interface Props {
  entries: Entry[];
  trades: Trade[];
  thinking: boolean;
  tools: LiveTool[];
  more: boolean;
  onMore: () => void;
  now: number;
}

export function Journal({ entries, trades, thinking, tools, more, onMore, now }: Props) {
  const rows = timeline(entries, trades);
  return (
    <div className="journal">
      {thinking && (
        <div className="thinking" role="status">
          <div className="row"><span className="pulse" />Opus düşünüyor, piyasayı inceliyor…</div>
          {tools.length > 0 && <ToolList tools={tools.slice(-8)} />}
        </div>
      )}
      {!rows.length && !thinking && <div className="empty">Henüz karar yok. Opus'un ilk kararı burada görünecek.</div>}
      {rows.map((r) => (r.kind === "entry" ? <Card key={`e${r.entry.id}`} e={r.entry} now={now} />
                                           : <TradeRow key={`t${r.trade.id}`} t={r.trade} />))}
      {more && <button className="btn small more" onClick={onMore}>Daha eski kararlar</button>}
    </div>
  );
}
