/**
 * file: docs.js
 *
 * Hakkında sayfasının bütün davranışı. Adresin son parçasından bölümü bulur,
 * pages/ altındaki HTML parçasını içeriğe koyar, parçadaki data-bind ve
 * data-render işaretli yerleri ../api/about verisiyle doldurur, solda bölüm
 * menüsünü, sağda bu sayfadaki başlıkları ve altta önceki ile sonraki bölüm
 * bağlantılarını kurar.
 *
 * description: Opus Trading Masası dokümantasyonunun bölüm yönlendirmesi ve canlı veri doldurucusu.
 */

// Yeni bir bölüm için pages/ altına bir HTML parçası eklenir ve buraya bir
// satır yazılır. Sıra menüdeki ve önceki sonraki bağlantılarındaki sıradır.
// Kimlik adresteki parçadır, genel bakışın kimliği boştur ve details/ adresidir.
const SECTIONS = [
  ["Başlarken", [
    ["", "Genel bakış", "info"],
    ["profiller", "Profiller", "sparkle"],
    ["yasal-uyari", "Yasal uyarı", "scale"],
  ]],
  ["Opus", [
    ["cagri-akisi", "Bir çağrının akışı", "refresh"],
    ["gordukleri", "Opus'un gördükleri", "page"],
    ["araclar", "Araçlar", "chart"],
    ["talimat", "Talimat", "flag"],
    ["karar-semasi", "Karar şeması", "check"],
    ["mudahaleler", "Yönetici müdahaleleri", "bell"],
  ]],
  ["Sistem", [
    ["mimari", "Mimari", "diamond"],
    ["hesap-kurallari", "Hesap kuralları", "shield"],
    ["sinirlar", "Sınırlar", "warning"],
  ]],
];

const FLAT = SECTIONS.flatMap(([, items]) => items);
const $ = (id) => document.getElementById(id);
const icon = (name) => `<svg class="ic" aria-hidden="true"><use href="../static/icons.svg#${name}"/></svg>`;
const href = (slug) => (slug ? `./${slug}` : "./");

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

const num = (x, d = 2) => new Intl.NumberFormat("tr-TR", { minimumFractionDigits: 0, maximumFractionDigits: d }).format(x);
const pct = (x) => `yüzde ${num(x * 100, 4)}`;
const signed = (x) => (x > 0 ? "+" : x < 0 ? "−" : "") + num(Math.abs(x), 2);
const MONTHS = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"];
const date = (t) => {
  const d = new Date(t * 1000);
  return `${d.getDate()} ${MONTHS[d.getMonth()]} ${d.getFullYear()}, ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
};
const duration = (h) => (h % 24 === 0 ? `${h / 24} gün` : `${h} saat`);

// Adresin details/ sonrasındaki parçası bölümün kimliğidir.
function currentSlug() {
  const m = location.pathname.match(/\/details\/([^/]*)\/?$/);
  return m ? decodeURIComponent(m[1]) : "";
}

// --- Menü, başlıklar ve sayfa altı ---

function drawNav(slug) {
  $("docsNav").innerHTML = SECTIONS.map(([group, items]) => `<h4>${group}</h4>` + items.map(([s, title, ic]) =>
    `<a href="${href(s)}" class="${s === slug ? "selected" : ""}">${icon(ic)}${title}</a>`).join("")).join("");
}

function drawPager(slug) {
  const i = FLAT.findIndex(([s]) => s === slug);
  const prev = FLAT[i - 1], next = FLAT[i + 1];
  $("pager").innerHTML =
    (prev ? `<a href="${href(prev[0])}"><small>Önceki</small><b>${prev[1]}</b></a>` : "<span style='flex:1'></span>") +
    (next ? `<a class="next" href="${href(next[0])}"><small>Sonraki</small><b>${next[1]}</b></a>` : "");
}

// Sağdaki liste içerikteki h2 başlıklarından kurulur. Kaydırınca ekranın
// üstüne en yakın başlık işaretlenir.
function drawToc() {
  const heads = [...$("content").querySelectorAll("h2")];
  heads.forEach((h) => {
    h.id = h.id || h.textContent.trim().toLocaleLowerCase("tr").replace(/[^a-z0-9çğıöşü]+/g, "-").replace(/^-|-$/g, "");
  });
  $("toc").innerHTML = heads.length
    ? `<h4>Bu sayfada</h4>` + heads.map((h) => `<a href="#${h.id}">${esc(h.textContent)}</a>`).join("")
    : "";
  const links = [...$("toc").querySelectorAll("a")];
  const mark = () => {
    let current = heads[0];
    for (const h of heads) if (h.getBoundingClientRect().top < 120) current = h;
    links.forEach((a) => a.classList.toggle("active", current && a.getAttribute("href") === `#${current.id}`));
  };
  window.addEventListener("scroll", mark, { passive: true });
  mark();
}

// --- Canlı veri ---

let aboutData = null;
const about = () => (aboutData ??= fetch("../api/about").then((r) => r.json()));

// data-bind="tool_step_limit" gibi yollar about verisinden okunur. data-format
// pct ise oran yüzdeye, int ise sayı Türkçe biçime çevrilir.
function fillBinds(data) {
  for (const el of document.querySelectorAll("[data-bind]")) {
    const v = el.dataset.bind.split(".").reduce((o, k) => o?.[k], data);
    if (v === undefined) continue;
    el.textContent = el.dataset.format === "pct" ? pct(v) : typeof v === "number" ? num(v, 4) : v;
  }
}

const RENDER = {
  profiles: renderProfiles,
  tools: (data) => data.tools.map(toolCard).join(""),
  "builtin-tools": (data) => `<div class="table-wrap"><table><thead><tr><th>Araç</th><th>Ne yapar</th></tr></thead><tbody>${
    data.builtin_tools.map((t) => `<tr><td><code>${t.name}</code></td><td>${esc(t.description)}</td></tr>`).join("")}</tbody></table></div>`,
  "system-prompt": (data) => profileTabs(data, (p) => `<pre>${esc(p.system_prompt)}</pre>`),
  schema: (data) => profileTabs(data, (p) => `<pre>${esc(JSON.stringify(p.schema, null, 2))}</pre>`),
  // Her eylem şemada ayrı bir seçenektir: tipi sabit değer, açıklaması ve
  // zorunlu alanları seçeneğin kendisinde durur.
  actions: (data) => `<div class="table-wrap"><table><thead><tr><th>Eylem</th><th>Ne yapar</th><th>Zorunlu alanlar</th></tr></thead><tbody>${
    data.profiles[0].schema.properties.eylemler.items.anyOf.map((a) => `<tr><td><code>${a.properties.tip.const}</code></td><td>${esc(a.description)}</td><td>${
      a.required.filter((k) => k !== "tip").map((k) => `<code>${k}</code>`).join(" ") || "Yok"}</td></tr>`).join("")}</tbody></table></div>`,
  catalog: (data) => `<div class="table-wrap"><table><thead><tr><th>Parite</th><th>En fazla kaldıraç</th><th>Bakım marjini</th><th>En küçük işlem</th><th>Miktar adımı</th><th>Kayma</th></tr></thead><tbody>${
    Object.entries(data.catalog).map(([s, c]) => `<tr><td><code>${s}</code></td><td>${c.max_leverage}x</td><td>${pct(c.maint_margin)}</td><td>${num(c.min_notional)} $</td><td>${c.qty_step}</td><td>${pct(c.slippage)}</td></tr>`).join("")}</tbody></table></div>`,
  markets: (data) => `<div class="table-wrap"><table><thead><tr><th>Piyasa</th><th>Saat dilimi</th><th>Yerel seans</th></tr></thead><tbody>${
    data.markets.map((m) => `<tr><td>${esc(m.name)}</td><td><code>${m.tz}</code></td><td>${m.sessions.map(([a, b]) => `${a}-${b}`).join(", ")}</td></tr>`).join("")}</tbody></table></div>`,
  "last-prompt": renderLastPrompt,
};

async function fillRenders() {
  const targets = [...document.querySelectorAll("[data-render]")];
  if (!targets.length && !document.querySelector("[data-bind]")) return;
  let data;
  try {
    data = await about();
  } catch (_) {
    targets.forEach((el) => { el.innerHTML = `<p class="loading-text">Veri şu an alınamıyor, program kapalı olabilir.</p>`; });
    return;
  }
  fillBinds(data);
  for (const el of targets) {
    try {
      el.innerHTML = await RENDER[el.dataset.render](data);
    } catch (_) {
      el.innerHTML = `<p class="loading-text">Bu bölüm şu an yüklenemedi.</p>`;
    }
  }
  wireTabs();
}

// Aynı içerik her profil için ayrı bir sekmede gösterilir.
function profileTabs(data, body) {
  const tabs = data.profiles.map((p, i) => `<button data-tab="${p.id}" class="${i ? "" : "selected"}">${esc(p.name)}</button>`).join("");
  const panes = data.profiles.map((p, i) => `<div data-pane="${p.id}" ${i ? "hidden" : ""}>${body(p)}</div>`).join("");
  return `<div class="tabs-inline">${tabs}</div>${panes}`;
}

function wireTabs() {
  for (const bar of document.querySelectorAll(".tabs-inline")) {
    bar.onclick = (e) => {
      const b = e.target.closest("[data-tab]");
      if (!b) return;
      bar.querySelectorAll("button").forEach((x) => x.classList.toggle("selected", x === b));
      bar.parentElement.querySelectorAll("[data-pane]").forEach((p) => { p.hidden = p.dataset.pane !== b.dataset.tab; });
    };
  }
}

async function renderProfiles(data) {
  const states = await Promise.all(data.profiles.map((p) =>
    fetch(`../api/${p.id}/state`).then((r) => r.json()).catch(() => null)));
  return `<div class="profile-cards">${data.profiles.map((p, i) => {
    const s = states[i];
    const ret = s ? s.return_pct : 0;
    return `<section class="profile-card">
      <header><img src="../static/vendor/icons/claude.svg" alt=""><h3>${esc(p.name)}</h3>
        <div class="figure"><b>$${num(s ? s.equity : p.balance, 2)}</b><span class="${ret >= 0 ? "positive" : "negative"}">yüzde ${signed(ret)}</span></div>
      </header>
      <dl>
        <dt>Başlangıç</dt><dd>$${num(p.balance)}</dd>
        <dt>Pariteler</dt><dd>${p.symbols.map((x) => x.replace("USDT", "")).join(", ")}</dd>
        <dt>Dönem</dt><dd>${duration(p.period_hours)}, ${date(p.start_time)} ile ${date(p.end_time)} arası</dd>
        <dt>Çağrı aralığı</dt><dd>${p.min_wait} ile ${p.max_wait} dakika, Opus kendisi seçer</dd>
        <dt>Strateji</dt><dd>${p.style ? "Talimatına bir çalışma tarzı eklenir" : "Verilmez, kararlarını kendisi verir"}</dd>
      </dl>
      ${p.style ? `<p class="style-text">${esc(p.style)}</p>` : ""}
    </section>`;
  }).join("")}</div>`;
}

// Opus'un gördüğü tanım olduğu gibi gösterilir: açıklamanın ilk paragrafı
// metin olarak, parametreler şemadan okunarak. Docstring'deki Args kısmı
// şemadaki açıklamaların aynısı olduğu için tekrar gösterilmez.
function toolCard(t) {
  const [summary] = t.description.split(/\n\s*Args:/);
  const props = t.parameters?.properties || {};
  const required = new Set(t.parameters?.required || []);
  const docArgs = Object.fromEntries((t.description.split(/Args:/)[1] || "").split("\n")
    .map((l) => l.trim().match(/^(\w+):\s*(.+)$/)).filter(Boolean).map((m) => [m[1], m[2]]));
  const params = Object.entries(props).map(([k, v]) =>
    `<code>${k}${required.has(k) ? "" : "?"}</code><span>${esc(docArgs[k] || v.description || v.type || "")}${v.default !== undefined ? ` Varsayılan ${esc(JSON.stringify(v.default))}.` : ""}</span>`).join("");
  return `<div class="tool"><h3><code>${t.name}</code></h3><p>${esc(summary.trim()).replace(/\n\s*\n/g, "</p><p>").replace(/\n\s*/g, " ")}</p>
    ${params ? `<div class="params">${params}</div>` : ""}</div>`;
}

// Asıl profilin en son başarılı çağrısının istemi olduğu gibi gösterilir.
async function renderLastPrompt(data) {
  const pid = data.profiles[0].id;
  const list = await (await fetch(`../api/${pid}/journal`)).json();
  const last = list.find((j) => !j.error && j.decision);
  if (!last) return `<p class="loading-text">Henüz tamamlanmış bir çağrı yok.</p>`;
  const full = await (await fetch(`../api/${pid}/journal/${last.id}`)).json();
  return `<details><summary>${esc(data.profiles[0].name)} · ${date(last.time)} çağrısının istemi <span class="muted">${num(full.prompt.length, 0)} karakter</span></summary><pre>${esc(full.prompt)}</pre></details>`;
}

// --- Sayfa ---

async function show() {
  const slug = currentSlug();
  const known = FLAT.find(([s]) => s === slug);
  drawNav(slug);
  drawPager(slug);
  document.body.classList.remove("nav-open");
  if (!known) {
    $("content").innerHTML = `<p class="eyebrow">Bulunamadı</p><h1>Böyle bir bölüm yok</h1><p class="lead">Soldaki menüden bir bölüm seçebilirsin.</p>`;
    $("toc").innerHTML = "";
    return;
  }
  document.title = `${known[1]} · Hakkında · Opus Trading Masası`;
  try {
    const r = await fetch(`../static/docs/pages/${slug || "genel-bakis"}.html`);
    $("content").innerHTML = await r.text();
  } catch (_) {
    $("content").innerHTML = `<p class="lead">Bu bölüm şu an yüklenemedi.</p>`;
  }
  // Parçalardaki ikonlar icon adıyla işaretlenir, burada çizilir.
  $("content").querySelectorAll("[data-icon]").forEach((el) => { el.outerHTML = icon(el.dataset.icon); });
  drawToc();
  if (location.hash) document.getElementById(location.hash.slice(1))?.scrollIntoView();
  fillRenders();
}

$("menuButton").addEventListener("click", () => {
  const open = document.body.classList.toggle("nav-open");
  $("menuButton").setAttribute("aria-expanded", String(open));
});

show();
