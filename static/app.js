'use strict';
// Tablero VES/USD. Todo el JS vive aquí: la CSP (script-src 'self') bloquea scripts y atributos on* inline.

const API = '/api/v1/rates';
const ALL_TIME_DAYS = 7300; // = max_range_days del servidor
const REFERENCE = 'bcv';
const USDT_SOURCES = ['binance_p2p', 'yadio']; // candidatas a "fuente principal" (paneles de variación y brecha)
const TZ = 'America/Caracas'; // UTC-4 sin horario de verano, igual que LOCAL_TZ del servidor
const DAY_MS = 86400000;

const state = { days: 30, sources: ['binance_p2p', 'bcv'], catalog: [], stats: [] };
const $ = (id) => document.getElementById(id);

// --- Formato: siempre es-VE (punto de miles, coma decimal)
const fmtPrice = new Intl.NumberFormat('es-VE', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtAxis = new Intl.NumberFormat('es-VE', { maximumFractionDigits: 2 });
const fmtPct = new Intl.NumberFormat('es-VE', { minimumFractionDigits: 1, maximumFractionDigits: 2, signDisplay: 'exceptZero' });
const fmtPctCell = new Intl.NumberFormat('es-VE', { minimumFractionDigits: 2, maximumFractionDigits: 2, signDisplay: 'exceptZero' });
const fmtPctAbs = new Intl.NumberFormat('es-VE', { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const fmtInt = new Intl.NumberFormat('es-VE');
const dayMonth = new Intl.DateTimeFormat('es-VE', { day: 'numeric', month: 'short', timeZone: 'UTC' });
const dayMonthYear = new Intl.DateTimeFormat('es-VE', { day: 'numeric', month: 'short', year: '2-digit', timeZone: 'UTC' });
const monthYear = new Intl.DateTimeFormat('es-VE', { month: 'short', year: 'numeric', timeZone: 'UTC' });
const dateTime = new Intl.DateTimeFormat('es-VE', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', timeZone: TZ });

// "2026-10-05" (o el prefijo de un ISO con hora local) como fecha de calendario, sin corrimiento de zona
const calendarDate = (iso) => new Date(`${iso.slice(0, 10)}T00:00:00Z`);

function fmtLabel(iso, interval, longSpan) {
  if (interval === 'raw' || interval === 'hour') return dateTime.format(new Date(iso));
  if (interval === 'month') return monthYear.format(calendarDate(iso));
  return (longSpan ? dayMonthYear : dayMonth).format(calendarDate(iso));
}

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

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'text') node.textContent = v;
    else if (k === 'class') node.className = v;
    else if (k === 'style') Object.assign(node.style, v);
    else node.setAttribute(k, v);
  }
  for (const c of [].concat(children)) if (c != null) node.append(c);
  return node;
}

const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const seriesColor = (source) => cssVar(`--s-${source}`) || cssVar('--s-other');
const sourceMeta = (name) => state.catalog.find((s) => s.name === name) || { name, label: name, unit: '' };
const shortLabel = (name) => sourceMeta(name).label.replace(/\s+(USDT|USD)\/VES$/, '');

// Valor a graficar de un punto: precio en raw, el campo elegido (open/close/avg...) en velas
const pointValue = (p, interval, valueKey) => (interval === 'raw' ? p.price : p[valueKey]);

// Fuente principal: la primera USDT marcada; si no hay ninguna, Binance P2P
const primarySource = () => state.sources.find((s) => USDT_SOURCES.includes(s)) || USDT_SOURCES[0];

// --- Parámetros compartidos por todas las consultas del rango
function rangeParams() {
  const p = new URLSearchParams();
  const start = $('start').value, end = $('end').value;
  if (start) p.set('start', start); else p.set('days', state.days > 0 ? state.days : ALL_TIME_DAYS);
  if (end) p.set('end', end);
  return p;
}

function params(extra = {}) {
  const p = rangeParams();
  p.set('interval', $('interval').value);
  p.set('fill', $('fill').value);
  p.set('sources', state.sources.join(','));
  if ($('interval').value === 'raw') p.set('max_points', '2000');
  for (const [k, v] of Object.entries(extra)) p.set(k, v);
  return p;
}

// `/daily-change` solo acepta `days` hacia atrás desde ahora: se traduce el rango y luego se recorta
function dailyChangeDays() {
  const start = $('start').value;
  if (!start) return state.days > 0 ? state.days : ALL_TIME_DAYS;
  const days = Math.ceil((Date.now() - calendarDate(start).getTime()) / DAY_MS) + 1;
  return Math.min(Math.max(days, 1), ALL_TIME_DAYS);
}

function withinRange(isoDate) {
  const start = $('start').value, end = $('end').value;
  const d = isoDate.slice(0, 10);
  return (!start || d >= start) && (!end || d <= end);
}

// --- Gráficas
function theme() {
  return {
    surface: cssVar('--surface'), ink: cssVar('--ink'), ink2: cssVar('--ink-2'), muted: cssVar('--axis-text'),
    grid: cssVar('--grid'), axis: cssVar('--axis'), border: cssVar('--border'),
  };
}

// Línea vertical que sigue al puntero y se ancla a la X más cercana
const crosshair = {
  id: 'crosshair',
  beforeDatasetsDraw(chart, _args, opts) {
    const active = chart.tooltip && chart.tooltip.getActiveElements();
    if (!active || !active.length) return;
    const x = Math.round(active[0].element.x) + 0.5;
    const { top, bottom } = chart.chartArea;
    const ctx = chart.ctx;
    ctx.save();
    ctx.strokeStyle = opts.color;
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, bottom); ctx.stroke();
    ctx.restore();
  },
};

function tooltipBase(t) {
  return {
    backgroundColor: t.surface, borderColor: t.axis, borderWidth: 1, cornerRadius: 8, padding: 10,
    titleColor: t.ink2, titleFont: { weight: '400', size: 12 }, bodyColor: t.ink, bodyFont: { weight: '600', size: 13 },
    usePointStyle: true, boxWidth: 14, boxHeight: 14, boxPadding: 6, caretSize: 0,
    callbacks: {
      labelPointStyle: () => ({ pointStyle: 'line', rotation: 0 }),
      labelColor: (c) => ({ borderColor: c.dataset.borderColor, backgroundColor: c.dataset.borderColor, borderWidth: 2 }),
    },
  };
}

function axes(t, { yFormat, yTitle, maxTicks = 8, zeroLine = false }) {
  return {
    x: {
      ticks: { color: t.muted, maxTicksLimit: maxTicks, maxRotation: 0, autoSkipPadding: 12, font: { size: 11 } },
      grid: { display: false }, border: { color: t.axis },
    },
    y: {
      ticks: { color: t.muted, callback: yFormat, font: { size: 11 } },
      grid: { color: (c) => (zeroLine && c.tick && c.tick.value === 0 ? t.axis : t.grid), drawTicks: false },
      border: { display: false },
      title: yTitle ? { display: true, text: yTitle, color: t.muted, font: { size: 11 } } : { display: false },
    },
  };
}

const charts = {};
const narrow = () => window.matchMedia('(max-width: 520px)').matches;

function buildCharts() {
  Object.values(charts).forEach((c) => c.destroy());
  Chart.defaults.font.family = 'system-ui, -apple-system, "Segoe UI", sans-serif';
  const t = theme();
  const maxTicks = narrow() ? 4 : 8;

  charts.main = new Chart($('chart'), {
    type: 'line',
    data: { labels: [], datasets: [] },
    plugins: [crosshair],
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: 'index', intersect: false },
      elements: {
        line: { borderWidth: 2, tension: 0, borderJoinStyle: 'round', borderCapStyle: 'round' },
        point: { radius: 0, hitRadius: 12, hoverRadius: 5, hoverBorderWidth: 2 },
      },
      plugins: {
        crosshair: { color: t.axis },
        legend: {
          display: false, align: 'start',
          labels: { color: t.ink2, usePointStyle: true, pointStyle: 'line', pointStyleWidth: 18, boxHeight: 2, padding: 14,
                    font: { size: 12 } },
        },
        tooltip: {
          ...tooltipBase(t),
          itemSort: (a, b) => (b.parsed.y ?? -Infinity) - (a.parsed.y ?? -Infinity),
          callbacks: {
            ...tooltipBase(t).callbacks,
            label: (c) => `${c.parsed.y == null ? '—' : fmtPrice.format(c.parsed.y)} Bs   ${shortLabel(c.dataset.source)}`,
          },
        },
      },
      scales: axes(t, { yFormat: (v) => fmtAxis.format(v), maxTicks }),
    },
  });

  charts.daily = new Chart($('chart-daily'), {
    type: 'bar',
    data: { labels: [], datasets: [] },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      // En barras el blanco es la barra: sin cruz, la más cercana en X recibe el tooltip
      interaction: { mode: 'nearest', axis: 'x', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          ...tooltipBase(t),
          callbacks: {
            ...tooltipBase(t).callbacks,
            label: (c) => `${fmtPct.format(c.parsed.y)} %   ${shortLabel(c.dataset.source)}`,
          },
        },
      },
      scales: axes(t, { yFormat: (v) => `${fmtAxis.format(v)} %`, maxTicks, zeroLine: true }),
    },
  });

  charts.spread = new Chart($('chart-spread'), {
    type: 'line',
    data: { labels: [], datasets: [] },
    plugins: [crosshair],
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: 'index', intersect: false },
      elements: {
        line: { borderWidth: 2, tension: 0, borderJoinStyle: 'round' },
        point: { radius: 0, hitRadius: 12, hoverRadius: 5, hoverBorderWidth: 2 },
      },
      plugins: {
        crosshair: { color: t.axis },
        legend: { display: false },
        tooltip: {
          ...tooltipBase(t),
          callbacks: {
            ...tooltipBase(t).callbacks,
            label: (c) => `${fmtPct.format(c.parsed.y)} %   ${shortLabel(c.dataset.source)} contra BCV`,
          },
        },
      },
      scales: axes(t, { yFormat: (v) => `${fmtAxis.format(v)} %`, maxTicks, zeroLine: true }),
    },
  });
}

// --- Estado vacío explícito por panel
function setEmpty(id, missing) {
  const node = $(id);
  if (!missing.length) { node.hidden = true; node.textContent = ''; return; }
  node.hidden = false;
  node.textContent = `${missing.join(', ')}: Sin datos en este rango — corre el backfill`;
}

function setLoading(on) {
  ['panel-main', 'panel-daily', 'panel-spread'].forEach((id) => $(id).classList.toggle('is-loading', on));
}

// --- Serie principal + paneles secundarios
let lastData, lastDaily = [], lastSpread = [];
let loadSeq = 0;

async function load() {
  if (!state.sources.length) {
    setEmpty('main-empty', ['Ninguna fuente marcada']);
    return;
  }
  const seq = ++loadSeq;
  setLoading(true);
  $('status').textContent = 'Cargando…';
  const primary = primarySource();
  const spreadParams = rangeParams();
  spreadParams.set('interval', 'day');
  spreadParams.set('fill', 'forward');
  spreadParams.set('sources', `${primary},${REFERENCE}`);
  const [main, daily, spread] = await Promise.allSettled([
    getJson(`${API}/history?${params()}`),
    getJson(`${API}/daily-change?source=${primary}&days=${dailyChangeDays()}`),
    getJson(`${API}/history?${spreadParams}`),
  ]);
  if (seq !== loadSeq) return; // llegó una consulta más nueva mientras tanto
  setLoading(false);
  if (main.status === 'rejected') { setStatus(main.reason.message); return; }
  lastData = main.value;
  lastDaily = daily.status === 'fulfilled' ? daily.value.filter((d) => withinRange(d.date)) : [];
  lastSpread = spread.status === 'fulfilled' ? spreadSeries(spread.value, primary) : [];
  render();
}

function spreadSeries(data, primary) {
  const closes = (name) => {
    const s = data.series.find((x) => x.source === name);
    return new Map((s ? s.points : []).map((p) => [p.date.slice(0, 10), p.close]));
  };
  const usdt = closes(primary), ref = closes(REFERENCE);
  const out = [];
  for (const [date, u] of usdt) {
    const r = ref.get(date);
    if (u != null && r != null && r > 0) out.push({ date, pct: (u / r - 1) * 100 });
  }
  return out.sort((a, b) => a.date.localeCompare(b.date));
}

function render() {
  if (!lastData) return;
  const data = lastData;
  const interval = data.interval, valueKey = $('value').value;
  const labelSet = new Set();
  data.series.forEach((s) => s.points.forEach((p) => labelSet.add(p.date)));
  const labels = [...labelSet].sort();
  const longSpan = labels.length > 1 && (Date.parse(labels.at(-1)) - Date.parse(labels[0])) > 300 * DAY_MS;
  const surface = cssVar('--surface');

  const c = charts.main;
  c.data.labels = labels.map((l) => fmtLabel(l, interval, longSpan));
  c.data.datasets = data.series.filter((s) => s.points.length).map((s) => {
    const by = new Map(s.points.map((p) => [p.date, pointValue(p, interval, valueKey)]));
    const color = seriesColor(s.source);
    return {
      label: `${s.label} (${s.unit})`, source: s.source, data: labels.map((l) => (by.has(l) ? by.get(l) : null)),
      borderColor: color, backgroundColor: color, pointHoverBackgroundColor: color, pointHoverBorderColor: surface,
      spanGaps: true,
    };
  });
  c.options.plugins.legend.display = c.data.datasets.length >= 2; // leyenda siempre con 2+ series
  const valueName = interval === 'raw' ? 'cada tick' : $('value').selectedOptions[0].textContent.toLowerCase();
  const intervalName = $('interval').selectedOptions[0].textContent.toLowerCase();
  $('main-sub').textContent = interval === 'raw'
    ? 'Bolívares por unidad, cada tick registrado'
    : `Bolívares por unidad, ${valueName} por ${intervalName}`;
  c.update();
  setEmpty('main-empty', data.series.filter((s) => !s.points.length).map((s) => s.label));
  $('panel-main').classList.toggle('is-empty', !c.data.datasets.length);

  renderDaily(longSpan);
  renderSpread(longSpan);

  const n = data.series.reduce((a, s) => a + s.points.length, 0);
  setStatus(`${fmtInt.format(n)} puntos del ${data.start.slice(0, 10)} al ${data.end.slice(0, 10)} (${data.timezone})`, true);
  renderTables(labels);
}

function renderDaily(longSpan) {
  const primary = primarySource();
  const color = seriesColor(primary);
  $('daily-sub').textContent = `${shortLabel(primary)}, cierre contra cierre del día anterior, en %`;
  const c = charts.daily;
  c.data.labels = lastDaily.map((d) => fmtLabel(d.date, 'day', longSpan));
  const thin = lastDaily.length > 90; // con muchas barras no cabe esquina redondeada ni hueco
  c.data.datasets = [{
    label: shortLabel(primary), source: primary, data: lastDaily.map((d) => d.pct),
    backgroundColor: color, borderColor: color, hoverBackgroundColor: `${color}b3`,
    borderRadius: thin ? 0 : 4, borderSkipped: 'start', maxBarThickness: 24,
    categoryPercentage: 1, barPercentage: thin ? 1 : 0.8, borderWidth: 0,
  }];
  c.update();
  setEmpty('daily-empty', lastDaily.length ? [] : [shortLabel(primary)]);
  $('panel-daily').classList.toggle('is-empty', !lastDaily.length);
}

function renderSpread(longSpan) {
  const primary = primarySource();
  const color = seriesColor(primary);
  $('spread-sub').textContent = `Cuánto más cuesta el USDT en ${shortLabel(primary)} que el dólar oficial, en %`;
  const c = charts.spread;
  c.data.labels = lastSpread.map((d) => fmtLabel(d.date, 'day', longSpan));
  c.data.datasets = [{
    label: `${shortLabel(primary)} contra BCV`, source: primary, data: lastSpread.map((d) => d.pct),
    borderColor: color, backgroundColor: color, fill: false, // sin área: el eje no parte de 0
    pointHoverBackgroundColor: color, pointHoverBorderColor: cssVar('--surface'),
  }];
  c.update();
  setEmpty('spread-empty', lastSpread.length ? [] : [`${shortLabel(primary)} o BCV`]);
  $('panel-spread').classList.toggle('is-empty', !lastSpread.length);
}

function setStatus(text, live = false) {
  const s = $('status');
  s.replaceChildren(...(live ? [el('span', { class: 'live', 'aria-hidden': 'true' })] : []), text);
}

// --- Tablas: el equivalente accesible de las tres gráficas (etiquetas vía textContent)
function renderTables(labels) {
  const wrap = $('table-wrap');
  if (wrap.hidden || !lastData) return;
  const data = lastData, valueKey = $('value').value;
  const byDate = data.series.map((s) => new Map(s.points.map((p) => [p.date, pointValue(p, data.interval, valueKey)])));
  const head = el('tr', {}, [el('th', { scope: 'col', text: 'Fecha' }),
    ...data.series.map((s) => el('th', { scope: 'col', text: `${shortLabel(s.source)} (Bs)` }))]);
  const rows = labels.slice().reverse().map((l) => el('tr', {}, [
    el('td', { text: fmtLabel(l, data.interval, true) }),
    ...byDate.map((m) => el('td', { text: m.get(l) == null ? '—' : fmtPrice.format(m.get(l)) })),
  ]));
  const main = el('table', {}, [el('caption', { text: 'Tipo de cambio' }), el('thead', {}, head), el('tbody', {}, rows)]);

  const spreadBy = new Map(lastSpread.map((d) => [d.date, d.pct]));
  const dailyBy = new Map(lastDaily.map((d) => [d.date, d.pct]));
  const days = [...new Set([...dailyBy.keys(), ...spreadBy.keys()])].sort().reverse();
  const pct = (v) => (v == null ? '—' : `${fmtPctCell.format(v)} %`);
  const side = el('table', {}, [
    el('caption', { text: `Variación diaria y brecha de ${shortLabel(primarySource())}` }),
    el('thead', {}, el('tr', {}, [el('th', { scope: 'col', text: 'Día' }), el('th', { scope: 'col', text: 'Variación' }),
      el('th', { scope: 'col', text: 'Brecha' })])),
    el('tbody', {}, days.map((d) => el('tr', {}, [el('td', { text: fmtLabel(d, 'day', true) }), el('td', { text: pct(dailyBy.get(d)) }),
      el('td', { text: pct(spreadBy.get(d)) })]))),
  ]);
  wrap.replaceChildren(el('div', { class: 'table-scroll' }, main), el('div', { class: 'table-scroll' }, side));
}

// --- Fuentes
async function loadCatalog() {
  state.catalog = await getJson(`${API}/sources`);
  const box = $('sources');
  box.replaceChildren(...state.catalog.map((s) => {
    const cb = el('input', { type: 'checkbox', value: s.name });
    cb.checked = state.sources.includes(s.name);
    cb.addEventListener('change', () => {
      state.sources = [...box.querySelectorAll('input:checked')].map((i) => i.value);
      load();
    });
    return el('label', { title: s.label }, [cb, el('span', { class: 'key-line', style: { background: seriesColor(s.name) } }),
      shortLabel(s.name)]);
  }));
}

// --- Tiles: último precio, variación 24h y sparkline de 7 días
function sparkline(values, color) {
  const W = 88, H = 36, pad = 4;
  const svgNS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(svgNS, 'svg');
  svg.setAttribute('class', 'tile-spark');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.setAttribute('preserveAspectRatio', 'none');
  svg.setAttribute('aria-hidden', 'true');
  const vals = values.filter((v) => v != null);
  if (vals.length < 2) return svg;
  const min = Math.min(...vals), max = Math.max(...vals), span = max - min || 1;
  const pts = vals.map((v, i) => [pad + (i / (vals.length - 1)) * (W - 2 * pad), H - pad - ((v - min) / span) * (H - 2 * pad)]);
  const line = document.createElementNS(svgNS, 'polyline');
  line.setAttribute('points', pts.map((p) => p.map((n) => n.toFixed(1)).join(',')).join(' '));
  line.setAttribute('vector-effect', 'non-scaling-stroke');
  const [x, y] = pts.at(-1);
  const dot = document.createElementNS(svgNS, 'circle');
  Object.entries({ cx: x, cy: y, r: 3.5, fill: color, stroke: cssVar('--surface'), 'stroke-width': 2 })
    .forEach(([k, v]) => dot.setAttribute(k, v));
  svg.append(line, dot);
  return svg;
}

function deltaNode(change) {
  if (!change) return el('div', { class: 'tile-delta flat' }, [el('span', { class: 'vs', text: 'Sin dato de hace 24 h' })]);
  const dir = change.pct > 0.005 ? 'up' : change.pct < -0.005 ? 'down' : 'flat';
  const arrow = { up: '▲', down: '▼', flat: '■' }[dir];
  const word = { up: 'sube', down: 'baja', flat: 'igual' }[dir];
  return el('div', { class: `tile-delta ${dir}` }, [
    el('span', { 'aria-hidden': 'true', text: `${arrow} ` }),
    el('span', { text: `${word} ${fmtPctAbs.format(Math.abs(change.pct))} %` }),
    el('span', { class: 'vs', text: ' en 24 h' }),
  ]);
}

async function loadTiles() {
  const names = state.catalog.map((s) => s.name);
  const [statsRes, sparkRes] = await Promise.allSettled([
    getJson(`${API}/stats?sources=${names.join(',')}&reference=${REFERENCE}`),
    getJson(`${API}/history?days=7&interval=hour&fill=forward&sources=${names.join(',')}`),
  ]);
  if (statsRes.status === 'fulfilled') state.stats = statsRes.value;
  const spark = new Map();
  if (sparkRes.status === 'fulfilled') {
    for (const s of sparkRes.value.series) {
      const closes = s.points.map((p) => p.close);
      const step = Math.max(1, Math.floor(closes.length / 28)); // ~28 puntos: legible en 88 px
      spark.set(s.source, closes.filter((_, i) => i % step === 0 || i === closes.length - 1));
    }
  }
  $('tiles').replaceChildren(...state.catalog.map((meta) => {
    const st = state.stats.find((x) => x.source === meta.name);
    const label = el('div', { class: 'tile-label' }, [
      el('span', { class: 'swatch', style: { background: seriesColor(meta.name) } }), el('span', { text: meta.label })]);
    if (!st) {
      return el('article', { class: 'tile tile-empty' }, [label,
        el('div', { class: 'tile-value', text: 'Sin datos todavía — corre el backfill' })]);
    }
    return el('article', { class: 'tile' }, [
      label,
      el('div', { class: 'tile-value' }, [fmtPrice.format(st.latest), el('small', { text: 'Bs' })]),
      deltaNode(st.change && st.change['24h']),
      sparkline(spark.get(meta.name) || [], seriesColor(meta.name)),
      el('div', { class: 'tile-date', text: `Actualizado ${dateTime.format(new Date(st.latest_date))}` }),
    ]);
  }));
  renderHero();
}

function renderHero() {
  const usdt = USDT_SOURCES.map((n) => state.stats.find((s) => s.source === n)).find((s) => s && s.spread_vs_reference_pct != null);
  if (!usdt) {
    $('hero-label').textContent = 'Brecha USDT contra BCV';
    $('hero-value').textContent = '—';
    $('hero-note').textContent = 'Hace falta un precio USDT y uno del BCV';
    return;
  }
  const ref = state.stats.find((s) => s.source === REFERENCE);
  $('hero-label').textContent = `Brecha de ${shortLabel(usdt.source)} contra BCV`;
  $('hero-value').textContent = `${fmtPctAbs.format(usdt.spread_vs_reference_pct)} %`;
  $('hero-note').textContent = ref
    ? `${fmtPrice.format(usdt.latest)} Bs contra ${fmtPrice.format(ref.latest)} Bs oficiales`
    : '';
}

// --- Exportaciones. XLSX y PDF llegan en la fase D: se activan solos cuando el servidor los sirve.
const xlsxUrl = () => `${API}/history.xlsx?${params({ layout: 'wide', value: $('value').value })}`;
function pdfUrl() {
  const ref = $('end').value || new Date().toLocaleDateString('en-CA', { timeZone: TZ });
  return `${API}/report.pdf?month=${ref.slice(0, 7)}`;
}

async function probePending(button, url) {
  try {
    const res = await fetch(url, { method: 'HEAD' });
    // 405: la ruta existe pero solo admite GET (FastAPI no añade HEAD por defecto)
    if (res.ok || res.status === 405) {
      button.removeAttribute('aria-disabled');
      button.removeAttribute('title');
      button.dataset.pending = 'false';
    }
  } catch (_) { /* sin red: queda deshabilitado */ }
}

function onExport(button, urlFn) {
  button.addEventListener('click', () => {
    if (button.getAttribute('aria-disabled') === 'true') return;
    location.href = urlFn();
  });
}

// --- En vivo: SSE refresca tiles y gráficas cuando llega un tick nuevo
function connectLive() {
  const es = new EventSource(`${API}/stream?every=15`);
  es.addEventListener('rates', () => { loadTiles(); load(); });
  es.addEventListener('error', () => { es.close(); setTimeout(connectLive, 10000); });
}

// --- Controles
function wireControls() {
  $('presets').addEventListener('click', (e) => {
    const b = e.target.closest('button'); if (!b) return;
    $('presets').querySelectorAll('button').forEach((x) => x.setAttribute('aria-pressed', x === b));
    state.days = +b.dataset.days; $('start').value = ''; $('end').value = ''; load();
  });
  ['start', 'end', 'interval', 'fill'].forEach((id) => $(id).addEventListener('change', () => {
    if (id === 'start' || id === 'end') $('presets').querySelectorAll('button').forEach((x) => x.setAttribute('aria-pressed', 'false'));
    load();
  }));
  $('value').addEventListener('change', render);
  onExport($('csv-long'), () => `${API}/history.csv?${params({ layout: 'long', sep: ';' })}`);
  onExport($('csv-wide'), () => `${API}/history.csv?${params({ layout: 'wide', sep: ';', value: $('value').value })}`);
  onExport($('xlsx'), xlsxUrl);
  onExport($('pdf'), pdfUrl);
  $('toggle-table').addEventListener('click', () => {
    const w = $('table-wrap'); w.hidden = !w.hidden;
    $('toggle-table').setAttribute('aria-pressed', !w.hidden);
    $('toggle-table').textContent = w.hidden ? 'Ver tabla' : 'Ocultar tabla';
    render();
  });
  // Cambio de tema o de ancho: los colores y ticks del canvas se leen al construir
  const rebuild = () => { buildCharts(); render(); loadTiles(); };
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', rebuild);
  window.matchMedia('(max-width: 520px)').addEventListener('change', rebuild);
}

async function init() {
  wireControls();
  buildCharts();
  try { await loadCatalog(); } catch (e) { setStatus(e.message); return; }
  probePending($('xlsx'), xlsxUrl());
  probePending($('pdf'), pdfUrl());
  await Promise.all([loadTiles(), load()]);
  connectLive();
}

document.addEventListener('DOMContentLoaded', init);
