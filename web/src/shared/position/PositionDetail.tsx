/**
 * file: PositionDetail.tsx
 * importable: PositionDetail
 *
 * Pozisyon satırına tıklanınca altında açılan detay kartı. Dört bölümü var:
 * pozisyonun kendisi, şimdiye kadar olan, TP'ler ve stoplar. Altındaki
 * senaryo satırı pozisyon TP'lerden ya da stoplardan çıkarsa bütün net'ini
 * söyler. Herkese açık sayfa ve panel aynı kartı gösterir, panelin düğmeleri
 * satırda kalır.
 *
 * description: Pozisyonun marjini, kapanan parçaları, TP ve SL tahminleri.
 */

import { money, num, qty, signed } from "../format";
import { orderName } from "../labels";
import type { Order, Position } from "../types";
import { detailOf, liqDistance, type Leg } from "./model";
import "./position.css";

const tone = (x: number | null) => (x == null || x === 0 ? "" : x > 0 ? "up" : "down");

function Item({ label, value, cls = "" }: { label: string; value: string; cls?: string }) {
  return <div className="pd-item"><span>{label}</span><b className={`mono ${cls}`}>{value}</b></div>;
}

// Önündeki adım pozisyonu çoktan kapatıyorsa bu adım bir şey kapatmaz, tutarı
// yerine bunu söyleyen kısa bir not yazılır.
function Legs({ legs, empty }: { legs: Leg[]; empty: string }) {
  if (!legs.length) return <div className="pd-none">{empty}</div>;
  return (
    <div className="pd-legs">
      {legs.map((l, i) => (
        <div key={l.order?.id ?? `liq-${i}`} className={`pd-leg${l.order ? "" : " liq"}`}>
          <span>{l.order ? `#${l.order.number} ${orderName(l.order)}` : "Likidasyon"}</span>
          <span className="mono">{num(l.level)}</span>
          <span className="mono dim">{l.qty ? qty(l.qty) : "—"}</span>
          {l.qty ? <b className={`mono ${tone(l.net)}`}>{signed(l.net)}</b> : <span className="pd-idle">önceki kapatır</span>}
        </div>
      ))}
    </div>
  );
}

interface Props { position: Position; orders: Order[]; price: number | undefined; leverage: number | undefined }

export function PositionDetail({ position: p, orders, price, leverage }: Props) {
  const d = detailOf(p, orders);
  const liq = liqDistance(p, price);
  const beforeStop = d.stops.some((l) => !l.order && l.qty);
  return (
    <div className="pd">
      <div className="pd-grid">
        <section>
          <h3>Pozisyon</h3>
          <Item label="Marjin" value={money(p.margin)} />
          <Item label="Büyüklük" value={money(p.notional)} />
          <Item label="Kaldıraç" value={leverage ? `${leverage}x` : "—"} />
          <Item label="Likidasyona uzaklık" value={liq == null ? "—" : `%${num(liq, 2)}`} />
        </section>
        <section>
          <h3>Şimdiye kadar</h3>
          <Item label={`Kapanan parçalar · ${p.realized_count}`} value={p.realized_count ? signed(p.realized) : "—"}
                cls={tone(p.realized_count ? p.realized : null)} />
          <Item label="Açılış komisyonu" value={signed(-p.fee)} cls={tone(-p.fee)} />
          <Item label="Fonlama" value={signed(-p.funding)} cls={tone(-p.funding)} />
          <Item label="Açık kâr" value={signed(p.open_pnl)} cls={tone(p.open_pnl)} />
        </section>
        <section>
          <h3>TP'ler</h3>
          <Legs legs={d.tps} empty="TP yok." />
          {d.bareTp > 0 && d.tps.length > 0 && <div className="pd-note">{qty(d.bareTp)} TP'siz kalır.</div>}
        </section>
        <section>
          <h3>SL ve iz</h3>
          <Legs legs={d.stops} empty="Stop yok, pozisyon likidasyona kadar korumasız." />
          {beforeStop && <div className="pd-note warn">Likidasyon stoptan önce gelir.</div>}
          {d.bareStop > 0 && d.stops.length > 0 && <div className="pd-note warn">{qty(d.bareStop)} stopsuz kalır.</div>}
        </section>
      </div>
      <div className="pd-scenario">
        <Item label="TP'lerden çıkarsa toplam" value={signed(d.ifTp)} cls={tone(d.ifTp)} />
        <Item label="Stoplardan çıkarsa toplam" value={signed(d.ifStop)} cls={tone(d.ifStop)} />
        <span className="pd-hint">Kapanan parçalar, komisyon, fonlama ve kayma dahil net.</span>
      </div>
    </div>
  );
}
