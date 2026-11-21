/**
 * file: App.tsx
 * importable: App
 *
 * Herkese açık sayfa. Üst satır, yayındaki profilin grafiği, sekmeleri ve
 * günlüğü. Veri yalnızca herkese açık API'den gelir. Canlı akıştaki olaylar
 * ilgili parçayı yeniden yükler: karar günlüğü, hesap olayı hesabı ve
 * işlemleri, araç çağrısı düşünme kartını. Program kapalıysa kapalı ekranı
 * gösterilir ve yarım dakikada bir yeniden denenir.
 *
 * description: Herkese açık sayfanın yerleşimi ve verisi.
 */

// NOTE: Profilin özeti, yani durum, sonraki çağrı ve düşünüyor bilgisi,
// akışta ayrı bir olay olarak gelmez. Yarım dakikada bir yeniden okunur,
// karar ve hesap olaylarında da hemen yenilenir.
//
// NOTE: Geniş ekranda günlük sağdaki sütundadır. 1180 pikselden dar ekranda
// sekmelerin ilki olur, böylece grafik ve tablolar daralmaz.
//
// NOTE: Tam ekran tarayıcının tam ekranı değildir. Grafik kartı sekmenin
// tamamını kaplar, arkadaki sayfa kaymaz, Esc kartı küçültür.
//
// NOTE: Gizli katmanlar ziyaretçinin tarayıcısında saklanır. Tarayıcı izin
// vermezse filtre yalnızca o ziyaret boyunca geçerli olur.

import { useCallback, useEffect, useRef, useState, useSyncExternalStore, type RefObject } from "react";
import { ApiError, get } from "./api/client";
import { useConnected, useStream } from "./api/stream";
import type { Account, Entry, LiveTool, Profile } from "./api/types";
import { Chart } from "./page/Chart";
import { Journal } from "./page/Journal";
import { Filter, Legend } from "./page/Layers";
import { readHidden, status, summary, type Layer } from "./page/model";
import { Tabs } from "./page/Tabs";
import { INTERVALS } from "./shared/chart";
import { money, num, pct, signed } from "./shared/format";
import logo from "./shared/logo.svg";
import type { EquityPoint, Trade } from "./shared/types";

const POLL_MS = 30_000;
const PAGE = 50;
const WIDE = "(min-width: 1180px)";
const DOCS = import.meta.env.BASE_URL + "details/";
const NAME = "Opus Trade";
const HIDDEN_KEY = "opus-chart-hidden";

function loadHidden(): Set<Layer> {
  try { return readHidden(localStorage.getItem(HIDDEN_KEY)); } catch { return new Set(); }
}

function saveHidden(h: Set<Layer>) {
  try { localStorage.setItem(HIDDEN_KEY, JSON.stringify([...h])); } catch { /* tarayıcı saklamaya izin vermiyor */ }
}

// Üst satırın yüksekliği CSS'teki --top-h'ye yazılır. Grafik kartı ve günlük
// sütunu ekranı bu yüksekliğin altından doldurur, satır kırılınca da doğru kalır.
function useTopHeight(): RefObject<HTMLElement> {
  const ref = useRef<HTMLElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(() => document.documentElement.style.setProperty("--top-h", `${el.offsetHeight}px`));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return ref;
}

function Expand({ full, onClick }: { full: boolean; onClick: () => void }) {
  const label = full ? "Küçült" : "Tam ekran";
  return (
    <button className="expand" aria-label={label} title={label} aria-pressed={full} onClick={onClick}>
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"
           strokeLinejoin="round" aria-hidden="true">
        <path d={full ? "M9 3v6H3M15 3v6h6M9 21v-6H3M15 21v-6h6" : "M3 9V3h6M21 9V3h-6M3 15v6h6M21 15v6h-6"} />
      </svg>
    </button>
  );
}

function useWide(): boolean {
  return useSyncExternalStore((cb) => {
    const m = window.matchMedia(WIDE);
    m.addEventListener("change", cb);
    return () => m.removeEventListener("change", cb);
  }, () => window.matchMedia(WIDE).matches);
}

function useNow(): number {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now() / 1000), POLL_MS);
    return () => clearInterval(id);
  }, []);
  return now;
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: number | null }) {
  const cls = tone == null || tone === 0 ? "" : tone > 0 ? " up" : " down";
  return <div className="stat"><span>{label}</span><b className={`mono${cls}`}>{value}</b></div>;
}

function Closed() {
  return (
    <div className="closed" role="status">
      <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round"
           aria-hidden="true"><path d="M12 3v9M6.3 6.3a8 8 0 1 0 11.4 0" /></svg>
      <h2>Program şu an kapalı</h2>
      <p>Açık pozisyonlar ve emirler korunuyor. Program yeniden açıldığında kapalı kalınan süre işlenir ve Opus'a
        bildirilir. Sayfa kendini yarım dakikada bir yeniler.</p>
    </div>
  );
}

export function App() {
  const [profile, setProfile] = useState<Profile | null>(null);
  const [account, setAccount] = useState<Account | null>(null);
  const [trades, setTrades] = useState<Trade[]>([]);
  const [equity, setEquity] = useState<EquityPoint[]>([]);
  const [entries, setEntries] = useState<Entry[]>([]);
  const [more, setMore] = useState(false);
  const [down, setDown] = useState(false);
  const [missing, setMissing] = useState<string | null>(null);
  const [thinking, setThinking] = useState(false);
  const [tools, setTools] = useState<LiveTool[]>([]);
  const [symbol, setSymbol] = useState<string | null>(null);
  const [interval, setIv] = useState("15m");
  const [price, setPrice] = useState<number | null>(null);
  const [hidden, setHidden] = useState(loadHidden);
  const [full, setFull] = useState(false);
  const top = useTopHeight();
  const now = useNow();
  const wide = useWide();
  const connected = useConnected();

  // Program kapalıysa ya da yayında profil yoksa bunu söyleyen ekran gelir,
  // başka hata sayfanın o parçasını boş bırakır.
  const guard = useCallback(async <T,>(p: Promise<T>, set: (v: T) => void) => {
    try {
      set(await p);
      setDown(false);
    } catch (e) {
      if (e instanceof ApiError && e.down) setDown(true);
      else if (e instanceof ApiError && e.status === 404) setMissing(e.message);
    }
  }, []);

  const loadProfile = useCallback(() => guard(get<Profile>("/profile"), (p) => {
    setProfile(p);
    setMissing(null);
    setSymbol((s) => (s && p.symbols.includes(s) ? s : p.symbols[0] ?? null));
    if (!p.busy) setThinking(false);
  }), [guard]);
  const loadAccount = useCallback(() => guard(get<Account>("/account"), setAccount), [guard]);
  const loadTrades = useCallback(() => guard(get<Trade[]>("/trades"), (t) => setTrades([...t].reverse())), [guard]);
  const loadEquity = useCallback(() => guard(get<EquityPoint[]>("/equity"), setEquity), [guard]);
  const loadJournal = useCallback(() => guard(get<Entry[]>(`/journal?limit=${PAGE}`), (fresh) => {
    setEntries((old) => {
      const ids = new Set(fresh.map((e) => e.id));
      const kept = old.filter((e) => !ids.has(e.id) && e.time < (fresh.at(-1)?.time ?? Infinity));
      return [...fresh, ...kept];
    });
    setMore((m) => m || fresh.length === PAGE);
  }), [guard]);
  const loadOlder = () => {
    const oldest = entries.at(-1);
    if (!oldest) return;
    void guard(get<Entry[]>(`/journal?limit=${PAGE}&before=${oldest.id}`), (older) => {
      setEntries((old) => [...old, ...older.filter((e) => !old.some((x) => x.id === e.id))]);
      setMore(older.length === PAGE);
    });
  };

  const all = useCallback(() => {
    void loadProfile(); void loadAccount(); void loadTrades(); void loadEquity(); void loadJournal();
  }, [loadProfile, loadAccount, loadTrades, loadEquity, loadJournal]);
  useEffect(all, [all]);

  const downRef = useRef(down);
  downRef.current = down;
  useEffect(() => {
    const id = setInterval(() => (downRef.current ? all() : void loadProfile()), POLL_MS);
    return () => clearInterval(id);
  }, [all, loadProfile]);

  useStream((e) => {
    if ("journal" in e) {
      setThinking(false); setTools([]);
      void loadJournal(); void loadProfile(); void loadAccount();
    } else if ("tool" in e) {
      setThinking(true);
      setTools((t) => [...t, e.tool as LiveTool]);
    } else if ("account_event" in e) {
      void loadAccount(); void loadTrades(); void loadEquity();
    } else if ("fire" in e || "version" in e) {
      void loadProfile();
    }
  });

  const s = summary(profile, account, trades);
  const st = profile ? status(profile, thinking, tools.length, now) : null;
  const shown = price ?? (symbol ? account?.prices[symbol] : undefined) ?? null;

  useEffect(() => {
    if (!symbol || shown == null) return;
    document.title = `${symbol.replace(/USDT$/, "")} ${num(shown)} ${pct(s.ret)}`;
  }, [symbol, shown, s.ret]);

  const changeHidden = (h: Set<Layer>) => { setHidden(h); saveHidden(h); };

  useEffect(() => {
    if (!full) return;
    document.documentElement.classList.add("locked");
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setFull(false); };
    window.addEventListener("keydown", esc);
    return () => { document.documentElement.classList.remove("locked"); window.removeEventListener("keydown", esc); };
  }, [full]);

  const journal = (
    <Journal entries={entries} trades={trades} thinking={thinking || !!profile?.busy} tools={tools} more={more}
             onMore={loadOlder} now={now} />
  );

  return (
    <div className="site">
      <header className="top" ref={top}>
        <div className="who">
          <img src={logo} width="34" height="34" alt="" />
          <div>
            <h1>{NAME}</h1>
            {st && <div className="status">{st.busy && <i />}{st.text}{!connected && !down && " · canlı akış bağlanıyor"}</div>}
          </div>
        </div>
        <div className="stats">
          <Stat label="Fiyat" value={num(shown)} />
          <Stat label="Özkaynak" value={money(s.equity)} />
          <Stat label="Getiri" value={pct(s.ret)} tone={s.ret} />
          <Stat label="Açık K/Z" value={s.open == null ? "—" : signed(s.open)} tone={s.open} />
          <Stat label="İşlemler" value={s.trades ? `${s.trades} · ${s.wins} kazanç` : profile ? "0" : "—"} />
        </div>
        <nav className="links">
          <a href={`${DOCS}yasal-uyari`}>Yasal uyarı<small>Yatırım tavsiyesi değildir</small></a>
          <a href={DOCS}>Hakkında</a>
        </nav>
      </header>

      {down ? <Closed />
        : missing ? <div className="card empty notice">{missing} Yayına bir profil alındığında masası burada görünecek.</div>
        : (
          <main className={wide ? "desk wide" : "desk"}>
            <div className="main-col">
              <section className={full ? "card chart-card full" : "card chart-card"}>
                <div className="chart-head">
                  <div className="seg" role="tablist" aria-label="Parite">
                    {(profile?.symbols ?? []).map((x) => (
                      <button key={x} role="tab" aria-selected={x === symbol} onClick={() => { setSymbol(x); setPrice(null); }}>
                        {x.replace(/USDT$/, "")}</button>
                    ))}
                  </div>
                  <Filter hidden={hidden} onChange={changeHidden} />
                  <div className="seg push" role="tablist" aria-label="Aralık">
                    {INTERVALS.map(([k, label]) => (
                      <button key={k} role="tab" aria-selected={k === interval} onClick={() => setIv(k)}>{label}</button>
                    ))}
                  </div>
                </div>
                {symbol && (
                  <Chart symbol={symbol} interval={interval} positions={account?.state.positions ?? []}
                         orders={account?.state.orders ?? []} alerts={account?.alerts ?? []} trades={trades} hidden={hidden}
                         onPrice={setPrice} />
                )}
                <div className="legend">
                  <Legend hidden={hidden} />
                  <Expand full={full} onClick={() => setFull((f) => !f)} />
                </div>
              </section>
              {account && <Tabs account={account} trades={trades} equity={equity} start={profile?.start_balance ?? null}
                                journal={wide ? undefined : journal} />}
            </div>
            {wide && (
              <aside className="card side">
                <div className="side-head"><h2>Günlük</h2>
                  <span className="dim small">{entries.filter((e) => !e.error).length} karar</span></div>
                {journal}
              </aside>
            )}
          </main>
        )}
    </div>
  );
}
