const API = '/api/v1/rates';
const ALL_TIME_DAYS = 7300; // = max_range_days del servidor
const state = { days: 30, sources: ['binance_p2p', 'bcv'], catalog: [] };
const $ = (id) => document.getElementById(id);
async function getJson(url) {
  const res = await fetch(url);
  if (!res.ok) {
    let detail = `Error ${res.status}`;
    try {
      const body = await res.json();
      if (typeof body.detail === 'string') detail = body.detail;
      else if (body.detail) detail = JSON.stringify(body.detail);
    } catch (_) { /* cuerpo no JSON */ }
    throw new Error(detail);
  }
  return res.json();
}
// Valor a graficar de un punto: precio en raw, el campo elegido (open/close/avg...) en velas
const pointValue = (p, interval, valueKey) => (interval === 'raw' ? p.price : p[valueKey]);
const cssVar = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const fmt = new Intl.NumberFormat('es-VE', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtDate = (iso, interval) => interval === 'raw' || interval === 'hour'
  ? new Date(iso).toLocaleString('es-VE', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' })
  : iso.slice(0, 10);

let chart;
function ensureChart() {
  if (chart) return chart;
  Chart.defaults.font.family = 'system-ui, -apple-system, "Segoe UI", sans-serif';
  chart = new Chart($('chart'), {
    type: 'line',
    data: { labels: [], datasets: [] },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: 'index', intersect: false },
      elements: { line: { borderWidth: 2, tension: 0 }, point: { radius: 0, hitRadius: 8, hoverRadius: 4 } },
      plugins: {
        legend: { display: true, labels: { color: cssVar('--ink-2'), boxWidth: 10, boxHeight: 10, usePointStyle: true } },
        tooltip: { callbacks: { label: (c) => ` ${c.dataset.label}: ${c.parsed.y == null ? '—' : fmt.format(c.parsed.y)} Bs` } },
      },
      scales: {
        x: { ticks: { color: cssVar('--muted'), maxTicksLimit: 10, maxRotation: 0 }, grid: { display: false }, border: { color: cssVar('--axis') } },
        y: { ticks: { color: cssVar('--muted'), callback: (v) => fmt.format(v) }, grid: { color: cssVar('--grid') }, border: { display: false },
             title: { display: true, text: 'Bs por unidad', color: cssVar('--muted') } },
      },
    },
  });
  return chart;
}

function params(extra = {}) {
  const p = new URLSearchParams();
  const start = $('start').value, end = $('end').value;
  if (start) p.set('start', start); else p.set('days', state.days > 0 ? state.days : ALL_TIME_DAYS);
  if (end) p.set('end', end);
  p.set('interval', $('interval').value);
  p.set('fill', $('fill').value);
  p.set('sources', state.sources.join(','));
  if ($('interval').value === 'raw') p.set('max_points', '2000');
  for (const [k, v] of Object.entries(extra)) p.set(k, v);
  return p;
}

let lastData;
async function load() {
  if (!state.sources.length) return;
  $('status').textContent = 'Cargando…';
  try {
    lastData = await getJson(`${API}/history?${params()}`);
  } catch (e) { $('status').textContent = e.message; return; }
  render(lastData);
}

function render(data) {
  const c = ensureChart();
  const interval = data.interval, valueKey = $('value').value;
  const labelSet = new Set();
  data.series.forEach((s) => s.points.forEach((p) => labelSet.add(p.date)));
  const labels = [...labelSet].sort();
  c.data.labels = labels.map((l) => fmtDate(l, interval));
  c.data.datasets = data.series.map((s) => {
    const by = new Map(s.points.map((p) => [p.date, pointValue(p, interval, valueKey)]));
    const color = cssVar(`--s-${s.source}`) || '#888';
    return { label: `${s.label} (${s.unit})`, data: labels.map((l) => by.has(l) ? by.get(l) : null),
             borderColor: color, backgroundColor: color, spanGaps: true, pointStyle: 'circle' };
  });
  c.update();
  const n = data.series.reduce((a, s) => a + s.points.length, 0);
  $('status').innerHTML = `<span class="live"></span>${n} puntos · ${data.start.slice(0,10)} → ${data.end.slice(0,10)} · ${data.timezone}`;
  renderTable(data, labels, valueKey);
}

function renderTable(data, labels, valueKey) {
  const wrap = $('table-wrap');
  if (wrap.hidden) return;
  const byDate = data.series.map((s) => new Map(s.points.map((p) => [p.date, pointValue(p, data.interval, valueKey)])));
  const rows = labels.map((l) => `<tr><td>${fmtDate(l, data.interval)}</td>${byDate.map((m) => {
    const v = m.get(l);
    return `<td>${v == null ? '—' : fmt.format(v)}</td>`; }).join('')}</tr>`);
  wrap.innerHTML = `<table><thead><tr><th>Fecha</th>${data.series.map((s) => `<th>${s.label}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table>`;
}

async function loadCatalog() {
  state.catalog = await getJson(`${API}/sources`);
  $('sources').innerHTML = state.catalog.map((s) => `<label><input type="checkbox" value="${s.name}" ${state.sources.includes(s.name) ? 'checked' : ''}>
     <span class="swatch" style="background:${cssVar(`--s-${s.name}`)}"></span>${s.label}</label>`).join('');
  $('sources').querySelectorAll('input').forEach((cb) => cb.addEventListener('change', () => {
    state.sources = [...$('sources').querySelectorAll('input:checked')].map((i) => i.value); load();
  }));
}

async function loadLatest() {
  let latest;
  try { latest = await getJson(`${API}/latest`); } catch { return; }
  $('tiles').innerHTML = latest.map((p) => {
    const meta = state.catalog.find((s) => s.name === p.source) || { label: p.source, unit: '' };
    const extra = p.extra && p.extra.buy ? `compra ${fmt.format(p.extra.buy)} · venta ${fmt.format(p.extra.sell)}` : '';
    return `<div class="tile"><div class="k"><span class="swatch" style="background:${cssVar(`--s-${p.source}`)}"></span>${meta.label}</div>
      <div class="v">${fmt.format(p.price)} Bs</div><div class="d">${new Date(p.date).toLocaleString('es-VE')}${extra ? ' · ' + extra : ''}</div></div>`;
  }).join('');
}

// --- En vivo: SSE actualiza las tarjetas y refresca la serie cuando llega un tick nuevo
function connectLive() {
  const es = new EventSource(`${API}/stream?every=15`);
  es.addEventListener('rates', () => { loadLatest(); load(); });
  es.onerror = () => { es.close(); setTimeout(connectLive, 10000); };
}

// --- UI
$('presets').addEventListener('click', (e) => {
  const b = e.target.closest('button'); if (!b) return;
  $('presets').querySelectorAll('button').forEach((x) => x.setAttribute('aria-pressed', x === b));
  state.days = +b.dataset.days; $('start').value = ''; $('end').value = ''; load();
});
['start', 'end', 'interval', 'fill'].forEach((id) => $(id).addEventListener('change', () => {
  if (id === 'start' || id === 'end') $('presets').querySelectorAll('button').forEach((x) => x.setAttribute('aria-pressed', 'false'));
  load();
}));
$('value').addEventListener('change', () => lastData && render(lastData));
$('csv-long').onclick = () => location.href = `${API}/history.csv?${params({ layout: 'long', sep: ';' })}`;
$('csv-wide').onclick = () => location.href = `${API}/history.csv?${params({ layout: 'wide', sep: ';', value: $('value').value })}`;
$('toggle-table').onclick = () => {
  const w = $('table-wrap'); w.hidden = !w.hidden; $('toggle-table').setAttribute('aria-pressed', !w.hidden);
  $('toggle-table').textContent = w.hidden ? 'Ver tabla' : 'Ocultar tabla'; if (lastData) render(lastData);
};

(async () => { await loadCatalog(); await Promise.all([loadLatest(), load()]); connectLive(); })();
