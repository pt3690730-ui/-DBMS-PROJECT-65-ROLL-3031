/* =============================================================================
   Galaxy Cinemas - single page app (vanilla JavaScript, no frameworks, no CDN)
   -----------------------------------------------------------------------------
   * hash routing:  #/now  #/bookings  #/food  #/solution  #/database  #/source
                    #/shows #/counter   #/customers #/dashboard #/scheduling
                    #/reports #/master  #/seats/<screening_id>
   * every value coming from the API is escaped before it reaches the DOM
   * nothing but the Google Fonts stylesheet is loaded from outside this app
   ============================================================================= */
'use strict';

/* ------------------------------------------------------------- helpers ---- */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function esc(value) {
  if (value === null || value === undefined) return '';
  return String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
function escAttr(value) { return esc(value); }

function money(value) {
  const n = Number(value || 0);
  return 'INR ' + n.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function shortMoney(value) {
  const n = Number(value || 0);
  if (Math.abs(n) >= 10000000) return 'INR ' + (n / 10000000).toFixed(2) + ' Cr';
  if (Math.abs(n) >= 100000) return 'INR ' + (n / 100000).toFixed(2) + ' L';
  if (Math.abs(n) >= 1000) return 'INR ' + (n / 1000).toFixed(1) + 'k';
  return 'INR ' + n.toFixed(0);
}
function fmtDT(value) {
  if (!value) return '—';
  return String(value).replace('T', ' ').slice(0, 16);
}
function fmtTime(value) { const t = fmtDT(value); return t.length > 11 ? t.slice(11, 16) : '—'; }
function fmtDate(value) { const t = fmtDT(value); return t.slice(0, 10); }
function fmtDay(value) {
  const d = new Date(fmtDT(value) + ':00');
  if (isNaN(d)) return fmtDate(value);
  return d.toLocaleDateString('en-GB', { weekday: 'short', day: '2-digit', month: 'short' });
}
function todayISO(offset = 0) {
  /* local date, not UTC: a viva at 9 pm in IST must not show "yesterday" */
  const d = new Date();
  d.setDate(d.getDate() + offset);
  const pad = (value) => String(value).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}
function num(value) { const n = Number(value); return isNaN(n) ? 0 : n; }
function clamp(value, min, max) { return Math.max(min, Math.min(max, value)); }

function toast(message, kind = 'info') {
  const root = $('#toastRoot');
  const node = document.createElement('div');
  node.className = 'toast ' + (kind === 'error' ? 'error' : kind === 'ok' ? 'ok' : '');
  const title = kind === 'error' ? 'Rejected by the database' : kind === 'ok' ? 'Done' : 'Galaxy Cinemas';
  node.innerHTML = `<b>${esc(title)}</b><div>${esc(message)}</div>`;
  root.appendChild(node);
  setTimeout(() => { node.style.opacity = '0'; node.style.transition = 'opacity .4s'; }, 6200);
  setTimeout(() => node.remove(), 6800);
}

function loading(rows = 3) {
  return `<div class="card"><div class="loading"><span class="spinner"></span> Loading…</div>
    <div class="skeleton h28" style="margin-top:14px"></div>
    ${'<div class="skeleton"></div>'.repeat(rows)}</div>`;
}
function empty(title, text) {
  return `<div class="empty"><div class="big">${esc(title)}</div><p class="small">${esc(text)}</p></div>`;
}

/* ---------------------------------------------------------------- api ----- */
const api = {
  async request(method, path, body) {
    const options = { method, headers: {} };
    if (body !== undefined) { options.headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(body); }
    let response;
    try {
      response = await fetch(path, options);
    } catch (error) {
      throw new Error('Cannot reach the Flask backend. Is python app.py running?');
    }
    let payload = null;
    const text = await response.text();
    try { payload = text ? JSON.parse(text) : null; } catch (error) { payload = { error: text.slice(0, 300) }; }
    if (!response.ok) {
      const message = (payload && payload.error) || ('HTTP ' + response.status);
      const failure = new Error(message);
      failure.status = response.status;
      failure.payload = payload;
      throw failure;
    }
    return payload;
  },
  get(path) { return this.request('GET', path); },
  post(path, body) { return this.request('POST', path, body); },
  put(path, body) { return this.request('PUT', path, body); },
  del(path) { return this.request('DELETE', path); },
};

/* -------------------------------------------------------------- state ----- */
const state = {
  meta: null,
  role: 'CUSTOMER',
  customerId: 1,
  route: '#/now',
  log: { open: false, paused: false, since: 0, entries: [], groups: [] },
  db: { table: 'booking', tab: 'structure', limit: 25, offset: 0, sort: '', dir: 'asc', q: '' },
  source: { path: 'supabase/schema.sql', find: '', present: false, tab: 'code' },
  reports: { key: null },
};
const CUSTOMER = () => (state.meta ? state.meta.customers.find((c) => c.customer_id === state.customerId) : null);
const ROLE_TABS = {
  CUSTOMER: [['now', 'Now Showing'], ['bookings', 'My Bookings'], ['food', 'Food & Drinks'],
    ['solution', 'Solution'], ['database', 'Database'], ['source', 'Source Code']],
  STAFF: [['shows', 'Shows'], ['bookings', 'Bookings'], ['counter', 'Concession Counter'],
    ['customers', 'Customers'], ['solution', 'Solution'], ['database', 'Database'], ['source', 'Source Code']],
  ADMIN: [['dashboard', 'Dashboard'], ['scheduling', 'Scheduling'], ['bookings', 'Bookings'],
    ['reports', 'Reports'], ['master', 'Master Data'], ['solution', 'Solution'],
    ['database', 'Database'], ['source', 'Source Code']],
};

/* ------------------------------------------------------------ highlighting */
const HL = {
  sql: ['select', 'from', 'where', 'group', 'by', 'order', 'having', 'limit', 'offset', 'join',
    'left', 'right', 'inner', 'outer', 'full', 'on', 'as', 'and', 'or', 'not', 'null', 'is',
    'in', 'any', 'all', 'exists', 'case', 'when', 'then', 'else', 'end', 'union', 'insert',
    'into', 'values', 'update', 'set', 'delete', 'create', 'table', 'view', 'index', 'trigger',
    'function', 'returns', 'language', 'begin', 'declare', 'if', 'then', 'elsif', 'loop',
    'for', 'each', 'row', 'statement', 'execute', 'constraint', 'primary', 'key', 'foreign',
    'references', 'unique', 'check', 'default', 'cascade', 'restrict', 'using', 'gist',
    'exclude', 'with', 'distinct', 'coalesce', 'count', 'sum', 'avg', 'min', 'max', 'round',
    'filter', 'over', 'partition', 'raise', 'exception', 'notice', 'return', 'security',
    'enable', 'alter', 'policy', 'grant', 'revoke', 'extension', 'interval', 'asc', 'desc',
    'now', 'true', 'false', 'conflict', 'do', 'nothing', 'returning', 'generated', 'always',
    'identity', 'before', 'after', 'instead', 'of', 'get', 'diagnostics'],
  python: ['def', 'class', 'return', 'import', 'from', 'as', 'if', 'elif', 'else', 'for',
    'while', 'in', 'not', 'and', 'or', 'is', 'None', 'True', 'False', 'try', 'except',
    'finally', 'with', 'yield', 'lambda', 'pass', 'raise', 'global', 'nonlocal', 'assert',
    'del', 'async', 'await', 'break', 'continue', 'self'],
  javascript: ['const', 'let', 'var', 'function', 'return', 'if', 'else', 'for', 'while',
    'of', 'in', 'new', 'class', 'extends', 'import', 'from', 'export', 'default', 'try',
    'catch', 'finally', 'throw', 'await', 'async', 'typeof', 'instanceof', 'null',
    'undefined', 'true', 'false', 'this', 'switch', 'case', 'break', 'continue', 'delete',
    'do', 'static', 'get', 'set'],
};

function highlight(raw, language) {
  const stash = [];
  const keep = (cls, text) => '\u0001' + (stash.push(`<span class="${cls}">${text}</span>`) - 1) + '\u0001';
  let text = String(raw).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const mask = (regex, cls) => { text = text.replace(regex, (match) => keep(cls, match)); };

  const lang = language === 'javascript' ? 'js' : language === 'python' ? 'py' : language;
  if (lang === 'sql') { mask(/--[^\n]*/g, 'tok-com'); mask(/'(?:[^']|'')*'/g, 'tok-str'); }
  if (lang === 'py') { mask(/#[^\n]*/g, 'tok-com'); mask(/'''[\s\S]*?'''|"""[\s\S]*?"""|'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"/g, 'tok-str'); }
  if (lang === 'js') { mask(/\/\*[\s\S]*?\*\/|\/\/[^\n]*/g, 'tok-com'); mask(/`(?:[^`\\]|\\.)*`|'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"/g, 'tok-str'); }
  if (lang === 'css') { mask(/\/\*[\s\S]*?\*\//g, 'tok-com'); mask(/"[^"]*"|'[^']*'/g, 'tok-str'); mask(/#[0-9a-fA-F]{3,8}\b/g, 'tok-num'); }
  if (lang === 'html') {
    mask(/&lt;!--[\s\S]*?--&gt;/g, 'tok-com');
    mask(/&lt;\/?[a-zA-Z][\w:-]*/g, 'tok-tag');
    mask(/\/?&gt;/g, 'tok-tag');
    mask(/[a-zA-Z-]+(?==)/g, 'tok-attr');
    mask(/"[^"]*"/g, 'tok-str');
    mask(/&lt;style[\s\S]*?&lt;\/style&gt;/g, 'tok-com');
  }
  if (lang === 'sql' || lang === 'py' || lang === 'js' || lang === 'css') {
    mask(/\b\d+(?:\.\d+)?\b/g, 'tok-num');
    const words = HL[language] || HL[lang] || [];
    if (words.length) {
      mask(new RegExp('\\b(?:' + words.map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|') + ')\\b', 'g'), 'tok-key');
    }
    mask(/\b[A-Za-z_][\w]*(?=\s*\()/g, 'tok-fn');
  }
  return text.replace(/\u0001(\d+)\u0001/g, (_, index) => stash[Number(index)]);
}

function codeBlock(code, language, extraClass = '') {
  const lines = String(code).replace(/\s+$/, '').split('\n');
  const body = lines.map((line, index) =>
    `<tr><td class="ln">${index + 1}</td><td>${highlight(line, language) || '&nbsp;'}</td></tr>`).join('');
  return `<div class="code-shell ${extraClass}"><div class="code-scroll">
    <table class="code-table"><tbody>${body}</tbody></table></div></div>`;
}

/* -------------------------------------------------------------- charts ---- */
function barChart(rows, options = {}) {
  const valueKey = options.value || 'net_revenue';
  const labelKey = options.label || 'day';
  const data = rows.slice().reverse();
  if (!data.length) return empty('No data yet', 'Nothing to chart for this range.');
  const width = 900, height = 250, pad = { l: 62, r: 14, t: 18, b: 34 };
  const values = data.map((row) => num(row[valueKey]));
  const max = Math.max(1, ...values.map((v) => Math.abs(v)));
  const innerW = width - pad.l - pad.r, innerH = height - pad.t - pad.b;
  const step = innerW / data.length;
  const barW = Math.max(4, step * 0.62);
  let bars = '', labels = '', grid = '';
  for (let g = 0; g <= 4; g += 1) {
    const y = pad.t + (innerH / 4) * g;
    const value = max - (max / 4) * g;
    grid += `<line class="grid-line" x1="${pad.l}" y1="${y.toFixed(1)}" x2="${width - pad.r}" y2="${y.toFixed(1)}"/>`;
    grid += `<text x="6" y="${(y + 4).toFixed(1)}">${esc(shortMoney(options.signed === false ? value : value).replace('INR ', ''))}</text>`;
  }
  data.forEach((row, index) => {
    const value = num(row[valueKey]);
    const h = (Math.abs(value) / max) * innerH;
    const x = pad.l + index * step + (step - barW) / 2;
    const y = value >= 0 ? pad.t + innerH - h : pad.t + innerH;
    bars += `<rect class="bar-rect${index % 2 ? ' alt' : ''}" x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barW.toFixed(1)}" height="${Math.max(1, h).toFixed(1)}" rx="3"><title>${esc(fmtDate(row[labelKey]))}: ${esc(money(value))}</title></rect>`;
    if (index % Math.ceil(data.length / 9 || 1) === 0) {
      labels += `<text x="${(x + barW / 2).toFixed(1)}" y="${height - 12}" text-anchor="middle">${esc(fmtDate(row[labelKey]).slice(5))}</text>`;
    }
  });
  return `<svg class="chart" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="${esc(options.title || 'chart')}">
    <defs>
      <linearGradient id="barGrad" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#6FD3F2"/><stop offset="100%" stop-color="#1C4770"/></linearGradient>
      <linearGradient id="barGrad2" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#3D8BFF"/><stop offset="100%" stop-color="#123B66"/></linearGradient>
    </defs>${grid}${bars}${labels}</svg>`;
}

function hBarChart(rows, labelKey, valueKey, options = {}) {
  const data = rows.slice(0, options.limit || 8);
  if (!data.length) return empty('No data', 'Nothing to chart.');
  const max = Math.max(1, ...data.map((row) => num(row[valueKey])));
  return '<div class="stack">' + data.map((row) => {
    const pct = (num(row[valueKey]) / max) * 100;
    return `<div><div class="row between small"><span>${esc(row[labelKey])}</span>
      <span class="mono muted">${esc(options.money === false ? num(row[valueKey]).toLocaleString('en-IN') : money(row[valueKey]))}</span></div>
      <div class="bar"><i style="width:${pct.toFixed(1)}%"></i></div></div>`;
  }).join('') + '</div>';
}

/* ------------------------------------------------------------- modals ----- */
function closeModal() { $('#modalRoot').innerHTML = ''; }
function openModal(html, size = '') {
  $('#modalRoot').innerHTML = `<div class="modal-back" data-close="1">
    <div class="modal ${size}" role="dialog" aria-modal="true"><div class="inner">${html}</div></div></div>`;
  const back = $('.modal-back');
  back.addEventListener('mousedown', (event) => { if (event.target === back) closeModal(); });
  document.addEventListener('keydown', escClose);
  const focusable = $('#modalRoot button, #modalRoot input, #modalRoot select, #modalRoot textarea');
  if (focusable) focusable.focus();
}
function escClose(event) { if (event.key === 'Escape') { closeModal(); document.removeEventListener('keydown', escClose); } }
function modalShell(title, eyebrow, body, foot) {
  return `<header><div><div class="eyebrow" style="margin:0">${esc(eyebrow)}</div>
    <h3>${esc(title)}</h3></div><span class="grow"></span>
    <button class="btn small ghost" data-act="close-modal" type="button">Close</button></header>
    <div class="body">${body}</div>${foot ? `<div class="foot">${foot}</div>` : ''}`;
}

/* ------------------------------------------------------------ sql log ----- */
async function pollLog() {
  if (!state.log.open || state.log.paused) return;
  try {
    const payload = await api.get('/api/log?since=' + state.log.since);
    if (payload.entries.length) {
      state.log.entries = state.log.entries.concat(payload.entries).slice(-300);
      state.log.since = payload.last_id;
      renderLog();
    }
    $('#logStats').textContent = `${payload.stats.count} of ${payload.stats.capacity} statements kept · ${payload.stats.errors} error(s)`;
    $('#logCount').textContent = String(payload.stats.last_id);
  } catch (error) { /* the drawer stays quiet if the backend is restarting */ }
}
function renderLog() {
  const body = $('#logBody');
  const groups = [];
  state.log.entries.forEach((entry) => {
    const route = entry.route || 'background';
    let group = groups[groups.length - 1];
    if (!group || group.route !== route) { group = { route, entries: [], ts: entry.ts }; groups.push(group); }
    group.entries.push(entry);
  });
  if (!groups.length) {
    body.innerHTML = empty('No statements yet', 'Book a seat and every INSERT, COMMIT and ROLLBACK appears here.');
    return;
  }
  body.innerHTML = groups.slice().reverse().map((group) => {
    const statements = group.entries.map((entry) => {
      const kind = (entry.kind || 'sql').toLowerCase();
      const classes = 'log-entry ' + kind + (entry.failed ? ' error' : '');
      const params = entry.params ? `<details><summary>parameters</summary>${esc(entry.params)}</details>` : '';
      const error = entry.error ? `<div class="err">${esc(entry.error)}</div>` : '';
      const trimmed = entry.statement && entry.statement.length > 260
        ? entry.statement.slice(0, 260) + ' …' : entry.statement;
      return `<div class="${classes}">
        <span class="time">${esc(entry.ts)}${entry.ms !== null ? ' · ' + entry.ms + ' ms' : ''}</span>
        <span class="kind">${esc((entry.kind || 'SQL').toUpperCase())}</span>
        <span class="sql">${esc(trimmed)}</span>${params}${error}</div>`;
    }).join('');
    const transactions = group.entries.some((e) => (e.kind || '').toUpperCase() === 'BEGIN');
    return `<div class="log-group"><div class="route"><span>${esc(group.route)}</span>
      <span>${esc(group.ts)}${transactions ? ' · transaction' : ''}</span></div>${statements}</div>`;
  }).join('');
}
function toggleLog(open) {
  state.log.open = open === undefined ? !state.log.open : open;
  $('#logDrawer').classList.toggle('open', state.log.open);
  $('#logDrawer').setAttribute('aria-hidden', state.log.open ? 'false' : 'true');
  if (state.log.open) pollLog();
}
setInterval(pollLog, 1500);

/* -------------------------------------------------------------- router ---- */
function parseRoute() {
  const raw = (location.hash || '#/now').replace(/^#\/?/, '');
  const [head, ...rest] = raw.split('/');
  return { name: head || 'now', args: rest };
}
async function render() {
  closeModal();          // navigating away always dismisses whatever modal is open
  const { name, args } = parseRoute();
  state.route = '#/' + name + (args.length ? '/' + args.join('/') : '');
  const allowed = ROLE_TABS[state.role].map(([key]) => key);
  const aliases = { seats: 'now', movie: 'now' };
  const target = aliases[name] || name;
  if (!allowed.includes(target) && !['seats'].includes(name)) {
    toast(`The ${state.role} role has no '${esc(name)}' tab — switched to the first tab.`, 'error');
    location.hash = '#/' + allowed[0];
    return;
  }
  $$('#tabs button').forEach((button) => {
    const key = button.dataset.tab;
    const active = key === target || (name === 'seats' && key === 'now');
    button.classList.toggle('active', active);
    button.setAttribute('aria-current', active ? 'page' : 'false');
  });
  // Swap in a fresh #view node before every page render: page-level click
  // listeners then belong to exactly one render and can never double-fire.
  const view = $('#view');
  const fresh = view.cloneNode(false);
  view.replaceWith(fresh);
  const pages = {
    now: renderNowShowing, shows: renderNowShowing, seats: () => renderSeats(args[0]),
    bookings: renderBookings, food: renderFood, counter: renderCounter,
    customers: renderCustomers, dashboard: renderDashboard, scheduling: renderScheduling,
    reports: renderReports, master: renderMaster, solution: renderSolution,
    database: renderDatabase, source: renderSource,
  };
  fresh.innerHTML = loading();
  try {
    await (pages[name] || renderNowShowing)(args);
  } catch (error) {
    fresh.innerHTML = `<div class="card accent bad"><div class="eyebrow">Could not load this page</div>
      <h2>${esc(error.message)}</h2><p class="muted small">Check that the Flask backend is running and that
      setup_db.py has been applied to the database.</p>
      <button class="btn" data-act="reload" type="button">Try again</button></div>`;
  }
}

/* -------------------------------------------------------- now showing ----- */
async function renderNowShowing() {
  const meta = state.meta;
  const filters = state.nowFilters || (state.nowFilters = {
    date: todayISO(), cinema_id: '', movie_id: '', upcoming: '1',
  });
  const query = new URLSearchParams();
  if (filters.date) query.set('date', filters.date);
  if (filters.cinema_id) query.set('cinema_id', filters.cinema_id);
  if (filters.movie_id) query.set('movie_id', filters.movie_id);
  if (filters.upcoming) query.set('upcoming', '1');
  query.set('limit', '200');
  const payload = await api.get('/api/screenings?' + query.toString());

  const byMovie = new Map();
  payload.screenings.forEach((show) => {
    if (!byMovie.has(show.movie_id)) byMovie.set(show.movie_id, { show, list: [] });
    byMovie.get(show.movie_id).list.push(show);
  });
  const staff = state.role === 'STAFF';

  const cards = Array.from(byMovie.values()).map(({ show, list }) => {
    const pills = list.map((item) => {
      const free = item.seats_left <= 0 || !item.bookable;
      const label = free ? 'Housefull' : item.seats_left + ' left';
      return `<button class="pill${free ? ' full' : ''}" ${free ? 'disabled' : ''}
        data-act="goto-seats" data-id="${item.screening_id}"
        title="${escAttr(item.cinema_name)} · ${escAttr(item.screen_name)} (${escAttr(item.screen_type)})">
        ${esc(fmtTime(item.start_time))}<small>${esc(label)} · ${esc(num(item.occupancy_pct).toFixed(0))}%</small></button>`;
    }).join('');
    return `<article class="card accent movie-card">
      <div class="poster"><span class="chip pale lang">${esc(show.language)}</span>
        <span class="title">${esc(show.title)}</span></div>
      <div class="row" style="gap:6px">
        <span class="chip">${esc(show.certificate)}</span>
        <span class="chip blue">${esc(show.genre)}</span>
        <span class="chip">${esc(show.duration_min)} min</span>
      </div>
      <div class="small muted">${esc(list.length)} show(s) on ${esc(fmtDay(filters.date || list[0].start_time))}
        · from ${esc(money(Math.min(...list.map((i) => num(i.base_price)))))}</div>
      <div class="pills">${pills}</div></article>`;
  }).join('');

  $('#view').innerHTML = `
    <section class="card accent" style="margin-bottom:18px">
      <div class="eyebrow">${staff ? 'Box office · shows on sale' : 'Now showing'}</div>
      <div class="row between">
        <div><h1>${esc(meta.cinemas.length)} cinemas · ${esc(meta.screens.length)} screens ·
          ${esc(meta.movies.length)} movies</h1>
          <p class="muted small" style="margin:0">${esc(payload.count)} shows match your filters.
          Seats are held by the partial unique index <span class="mono">ux_active_seat</span>, so a seat can
          never be sold twice for the same show.</p></div>
        <div class="row">
          <span class="badge"><b>${esc(fmtDay(filters.date || todayISO()))}</b></span>
          <span class="badge">${esc(num(payload.screenings.reduce((sum, s) => sum + num(s.seats_left), 0)).toLocaleString('en-IN'))} seats free</span>
        </div>
      </div>
      <hr>
      <div class="grid four">
        <label class="field"><span>Date</span><input type="date" id="fDate" value="${escAttr(filters.date)}"></label>
        <label class="field"><span>Cinema</span><select id="fCinema">
          <option value="">All cinemas</option>
          ${meta.cinemas.map((c) => `<option value="${c.cinema_id}" ${String(filters.cinema_id) === String(c.cinema_id) ? 'selected' : ''}>${esc(c.cinema_name)}</option>`).join('')}
        </select></label>
        <label class="field"><span>Movie</span><select id="fMovie">
          <option value="">All movies</option>
          ${meta.movies.map((m) => `<option value="${m.movie_id}" ${String(filters.movie_id) === String(m.movie_id) ? 'selected' : ''}>${esc(m.title)} (${esc(m.language)})</option>`).join('')}
        </select></label>
        <label class="field"><span>Quick range</span><select id="fWhen">
          <option value="today">Today</option><option value="tomorrow">Tomorrow</option>
          <option value="week">Next 7 days</option><option value="any">Any date</option>
        </select></label>
      </div>
    </section>
    ${byMovie.size ? `<div class="grid three">${cards}</div>`
      : empty('No shows for these filters', 'Try clearing the movie filter or picking another date.')}
  `;

  const apply = () => {
    state.nowFilters = {
      date: $('#fDate').value, cinema_id: $('#fCinema').value,
      movie_id: $('#fMovie').value, upcoming: filters.upcoming,
    };
    render();
  };
  $('#fDate').addEventListener('change', apply);
  $('#fCinema').addEventListener('change', apply);
  $('#fMovie').addEventListener('change', apply);
  $('#fWhen').addEventListener('change', (event) => {
    const value = event.target.value;
    state.nowFilters = {
      date: value === 'today' ? todayISO() : value === 'tomorrow' ? todayISO(1) : '',
      cinema_id: '', movie_id: '', upcoming: value === 'any' ? '' : '1',
    };
    render();
  });
}

/* ----------------------------------------------------------- seat map ----- */
async function renderSeats(screeningId) {
  if (!screeningId) { location.hash = '#/now'; return; }
  const payload = await api.get(`/api/screenings/${screeningId}/seats`);
  const show = payload.screening;
  const selected = new Map();
  const maxSeats = payload.max_seats_per_booking;
  const methods = state.meta.payment_methods;

  const rowsHTML = payload.rows.map((row) => `
    <div class="seat-row"><span class="row-label">${esc(row.row_label)}</span>
      ${row.seats.map((seat) => `<button class="seat ${seat.seat_type.toLowerCase()}${seat.taken ? ' taken' : ''}"
        data-seat="${seat.seat_id}" data-price="${seat.price}" data-label="${escAttr(seat.label)}"
        data-type="${esc(seat.seat_type)}" ${seat.taken ? 'disabled' : ''}
        aria-label="Seat ${escAttr(seat.label)}, ${esc(seat.seat_type)}, ${esc(seat.taken ? 'already booked' : money(seat.price))}"
        title="${escAttr(seat.label)} · ${esc(seat.seat_type)} · ${seat.taken ? 'already booked' : esc(money(seat.price))}">${esc(seat.seat_no)}</button>`).join('')}
      <span class="row-label">${esc(row.row_label)}</span></div>`).join('');

  $('#view').innerHTML = `
    <div class="grid split">
      <section class="card accent">
        <div class="row between">
          <div><div class="eyebrow">Seat map</div><h2 style="margin:0">${esc(show.title)}</h2>
            <p class="muted small" style="margin:0">${esc(show.cinema_name)} · ${esc(show.screen_name)}
              (${esc(show.screen_type)}) · ${esc(fmtDT(show.start_time))} · ${esc(show.certificate)} ·
              ${esc(show.duration_min)} min</p></div>
          <div class="right"><span class="chip ${show.bookable ? 'good' : 'bad'}">${esc(show.status)}</span>
            <div class="mono small muted">show #${esc(show.screening_id)}</div></div>
        </div>
        <hr>
        <div class="seatmap-scroll">
          <div class="screen-bar">S C R E E N</div>
          <div class="seat-rows">${rowsHTML}</div>
        </div>
        <div class="legend">
          <span><i style="background:var(--seat)"></i>STANDARD × 1.0</span>
          <span><i style="background:#123A63"></i>PREMIUM × 1.3</span>
          <span><i style="background:#1C2E58"></i>RECLINER × 1.6</span>
          <span><i style="background:#0A1526"></i>already booked (${esc(payload.taken_seats)})</span>
          <span><i style="background:var(--cyan)"></i>your selection</span>
        </div>
        <p class="small muted" style="margin-top:14px">${esc(payload.seats_left)} of ${esc(payload.total_seats)}
          seats are still free. Maximum ${esc(maxSeats)} seats per booking. Ticket price =
          base price ${esc(money(show.base_price))} × seat multiplier.</p>
      </section>

      <aside class="card accent blue summary">
        <div class="eyebrow" style="margin:0">Your booking</div>
        <h3 style="margin:0">Summary</h3>
        <div id="selList" class="small muted">No seats selected yet.</div>
        <hr>
        <div class="line"><span>Seats</span><b id="selCount">0</b></div>
        <div class="line"><span>Base fare</span><b id="selBase">${esc(money(0))}</b></div>
        <div class="line"><span>Booking fee</span><b>INR 0.00</b></div>
        <div class="line"><span class="muted">Total payable</span><span class="total" id="selTotal">${esc(money(0))}</span></div>
        <label class="field"><span>Customer</span>
          <select id="bookCustomer">
            ${state.meta.customers.map((c) => `<option value="${c.customer_id}" ${c.customer_id === state.customerId ? 'selected' : ''}>${esc(c.full_name)}</option>`).join('')}
          </select></label>
        <label class="field"><span>Payment method</span>
          <select id="bookMethod">${methods.map((m) => `<option value="${m}">${esc(m)}</option>`).join('')}</select></label>
        ${state.role === 'STAFF' ? `<label class="field"><span>Sold at counter by</span>
          <select id="bookStaff"><option value="">Online (no staff)</option>
          ${state.meta.staff.map((s) => `<option value="${s.staff_id}">${esc(s.full_name)} — ${esc(s.role)}</option>`).join('')}
          </select></label>` : ''}
        <button class="btn primary" id="payBtn" type="button" disabled>Pay &amp; confirm</button>
        <button class="btn ghost small" data-act="back" type="button">← Back to shows</button>
        <p class="small muted" style="margin:0">One transaction: booking → booked_seat → ticket → payment.
          If any statement fails, everything is rolled back.</p>
      </aside>
    </div>`;

  const refreshSummary = () => {
    const list = Array.from(selected.values());
    $('#selCount').textContent = String(list.length);
    const total = list.reduce((sum, seat) => sum + num(seat.price), 0);
    $('#selBase').textContent = money(total);
    $('#selTotal').textContent = money(total);
    $('#selList').innerHTML = list.length
      ? list.map((seat) => `<span class="fk-chip">${esc(seat.label)} · ${esc(money(seat.price))}</span>`).join(' ')
      : 'No seats selected yet.';
    $('#payBtn').disabled = list.length === 0 || !show.bookable;
  };
  const syncSeats = () => {
    $$('.seat', $('#view')).forEach((button) => {
      button.classList.toggle('selected', selected.has(Number(button.dataset.seat)));
    });
  };
  $('#view').addEventListener('click', (event) => {
    const button = event.target.closest('.seat');
    if (!button || button.classList.contains('taken')) return;
    const id = Number(button.dataset.seat);
    if (selected.has(id)) selected.delete(id);
    else if (selected.size >= maxSeats) { toast(`At most ${maxSeats} seats can be booked in one transaction.`, 'error'); return; }
    else selected.set(id, { seat_id: id, label: button.dataset.label, price: Number(button.dataset.price), seat_type: button.dataset.type });
    syncSeats(); refreshSummary();
  });
  $('#payBtn').addEventListener('click', async () => {
    $('#payBtn').disabled = true;
    try {
      const body = {
        customer_id: Number($('#bookCustomer').value),
        screening_id: Number(show.screening_id),
        seat_ids: Array.from(selected.keys()),
        method: $('#bookMethod').value,
      };
      if ($('#bookStaff') && $('#bookStaff').value) body.staff_id = Number($('#bookStaff').value);
      const result = await api.post('/api/bookings', body);
      await render();            // refresh the seat map first: render() closes modals
      toggleLog(true);
      showTicketModal(result.booking);
      toast(result.message, 'ok');
    } catch (error) {
      toast(error.message, 'error');
      render();
    }
  });
  refreshSummary();
}

function showTicketModal(booking) {
  const seats = booking.seats || [];
  const tickets = seats.map((seat) => `
    <div class="ticket">
      <div class="row between">
        <div><div class="eyebrow" style="margin:0">Galaxy Cinemas · e-ticket</div>
          <h3 style="margin:0">${esc(booking.movie_title)}</h3>
          <div class="small muted">${esc(booking.cinema_name)} · ${esc(booking.screen_name)}
            (${esc(booking.screen_type)}) · ${esc(fmtDT(booking.start_time))}</div></div>
        <div class="qr" title="ticket reference block"></div>
      </div>
      <div class="tear"></div>
      <div class="row between">
        <div><div class="small muted">Seat</div><b style="font-size:17px">${esc(seat.seat_label)}</b>
          <div class="small muted">${esc(seat.seat_type)} · ${esc(money(seat.price))}</div></div>
        <div><div class="small muted">Ticket</div><span class="ticket-code">${esc(seat.ticket_code || '—')}</span>
          <div class="small muted">status ${esc(seat.ticket_status || 'ISSUED')}</div></div>
        <div class="right"><div class="small muted">Booking</div><b>#${esc(booking.booking_id)}</b>
          <div class="small muted">${esc(booking.customer_name)}</div></div>
      </div>
    </div>`).join('');
  const payment = (booking.payments || [])[0] || {};
  openModal(modalShell('Your tickets', 'Booked in one transaction', `
    <div class="row between" style="margin-bottom:12px">
      <span class="chip good">CONFIRMED</span>
      <span class="mono small">${esc(seats.length)} ticket(s) · ${esc(money(booking.paid))} · ${esc(payment.method || '')}</span>
    </div>
    ${tickets}
    <details class="disclose" style="margin-top:14px"><summary>What the database just did</summary>
      <div class="small muted">INSERT booking → INSERT booked_seat ×${esc(seats.length)} →
      INSERT ticket ×${esc(seats.length)} → INSERT payment → COMMIT.
      payment reference <span class="mono">${esc(payment.transaction_ref || '')}</span>.</div>
    </details>`, `<button class="btn ghost" data-act="close-modal" type="button">Close</button>`));
}

/* ---------------------------------------------------------- bookings ------ */
async function renderBookings() {
  const params = new URLSearchParams();
  const staffView = state.role !== 'CUSTOMER';
  if (!staffView) params.set('customer_id', String(state.customerId));
  const filters = state.bookingFilters || (state.bookingFilters = { status: '', q: '', date: '' });
  if (filters.status) params.set('status', filters.status);
  if (filters.q) params.set('q', filters.q);
  if (filters.date) params.set('date', filters.date);
  params.set('limit', '80');
  const payload = await api.get('/api/bookings?' + params.toString());

  const rows = payload.bookings.map((booking) => `
    <tr>
      <td class="mono">#${esc(booking.booking_id)}</td>
      <td>${esc(booking.customer_name)}<div class="small muted">${esc(booking.email || '')}</div></td>
      <td>${esc(booking.movie_title)}<div class="small muted">${esc(booking.cinema_name)} · ${esc(booking.screen_name)}</div></td>
      <td class="mono">${esc(fmtDT(booking.start_time))}</td>
      <td>${esc(booking.seat_labels || '—')}<div class="small muted">${esc(booking.seats)} seat(s)</div></td>
      <td class="num">${esc(money(booking.total))}</td>
      <td>${num(booking.refunded) > 0 ? `<span class="chip bad">− ${esc(money(booking.refunded))}</span>` : '<span class="muted small">—</span>'}</td>
      <td><span class="chip ${booking.status === 'CONFIRMED' ? 'good' : 'bad'}">${esc(booking.status)}</span>
        ${booking.orders ? `<span class="chip blue" style="margin-left:4px">🍿 ${esc(booking.orders)}</span>` : ''}</td>
      <td class="right nowrap">
        <button class="btn small ghost" data-act="view-booking" data-id="${booking.booking_id}" type="button">Tickets</button>
        ${booking.cancellable ? `<button class="btn small danger" data-act="cancel-booking" data-id="${booking.booking_id}" type="button">Cancel</button>` : ''}
      </td></tr>`).join('');

  $('#view').innerHTML = `
    <section class="card accent">
      <div class="eyebrow">${staffView ? 'All bookings' : 'My bookings'}</div>
      <div class="row between">
        <div><h1>${esc(payload.count)} booking(s)</h1>
          <p class="muted small" style="margin:0">${staffView
            ? 'Every booking in the system, newest first. Cancel is offered only while the show has not started.'
            : `Bookings of ${esc((CUSTOMER() || {}).full_name || 'this customer')} only. A cancelled booking keeps its history and shows the refund it received.`}</p></div>
      </div>
      <hr>
      <div class="grid four">
        <label class="field"><span>Search</span><input id="bq" value="${escAttr(filters.q)}" placeholder="name, e-mail, movie or #id"></label>
        <label class="field"><span>Status</span><select id="bstatus">
          <option value="">All</option>
          <option value="CONFIRMED" ${filters.status === 'CONFIRMED' ? 'selected' : ''}>Confirmed</option>
          <option value="CANCELLED" ${filters.status === 'CANCELLED' ? 'selected' : ''}>Cancelled</option></select></label>
        <label class="field"><span>Show date</span><input type="date" id="bdate" value="${escAttr(filters.date)}"></label>
        <label class="field"><span>&nbsp;</span><button class="btn primary" data-act="apply-booking-filters" type="button">Apply filters</button></label>
      </div>
      ${payload.count ? `<div class="table-wrap"><table class="data"><thead><tr>
        <th>Booking</th><th>Customer</th><th>Movie</th><th>Show</th><th>Seats</th>
        <th>Paid</th><th>Refund</th><th>Status</th><th></th></tr></thead>
        <tbody>${rows}</tbody></table></div>`
        : empty('No bookings match', 'Change the filters or book a seat from Now Showing.')}
    </section>`;

  const apply = () => {
    state.bookingFilters = { q: $('#bq').value.trim(), status: $('#bstatus').value, date: $('#bdate').value };
    render();
  };
  document.addEventListener('cinema:apply-booking-filters', apply);
  $('#view').addEventListener('click', (event) => {
    if (event.target.closest('button[data-act="apply-booking-filters"]')) apply();
  });
  $('#bq').addEventListener('keydown', (event) => { if (event.key === 'Enter') apply(); });
  $('#bstatus').addEventListener('change', apply);
  $('#bdate').addEventListener('change', apply);
}

async function openBookingModal(bookingId) {
  const payload = await api.get('/api/bookings/' + bookingId);
  const booking = payload.booking;
  const seats = (booking.seats || []).map((seat) => `<tr>
      <td>${esc(seat.seat_label)}</td><td>${esc(seat.seat_type)}</td>
      <td class="mono">${esc(seat.ticket_code || '—')}</td>
      <td class="num">${esc(money(seat.price))}</td>
      <td><span class="chip ${seat.ticket_status === 'ISSUED' ? 'good' : seat.ticket_status === 'VOID' ? 'bad' : ''}">${esc(seat.ticket_status || seat.seat_status)}</span></td></tr>`).join('');
  const payments = (booking.payments || []).map((payment) => `<tr>
      <td class="mono">${esc(payment.transaction_ref)}</td><td>${esc(payment.method)}</td>
      <td class="num">${esc(money(payment.amount))}</td>
      <td><span class="chip ${payment.status === 'REFUNDED' ? 'bad' : payment.status === 'SUCCESS' ? 'good' : ''}">${esc(payment.status)}</span></td>
      <td class="mono">${esc(fmtDT(payment.paid_at))}</td></tr>`).join('');
  const cancellation = booking.cancellation;
  openModal(modalShell(`Booking #${booking.booking_id}`, booking.status, `
    <div class="grid two">
      <div><dl class="kv">
        <dt>Customer</dt><dd>${esc(booking.customer_name)} · ${esc(booking.customer_email)}</dd>
        <dt>Movie</dt><dd>${esc(booking.movie_title)} (${esc(booking.language)}, ${esc(booking.certificate)})</dd>
        <dt>Show</dt><dd>${esc(fmtDT(booking.start_time))} · ends ${esc(fmtTime(booking.end_time))}</dd>
        <dt>Cinema</dt><dd>${esc(booking.cinema_name)}, ${esc(booking.city)}</dd>
        <dt>Screen</dt><dd>${esc(booking.screen_name)} (${esc(booking.screen_type)})</dd>
        <dt>Booked at</dt><dd>${esc(fmtDT(booking.booking_time))}</dd>
        <dt>Channel</dt><dd>${booking.staff_id ? 'Box office · ' + esc(booking.staff_name || '') : 'Online (self service)'}</dd>
        <dt>Paid</dt><dd>${esc(money(booking.paid))}</dd>
        ${cancellation ? `<dt>Cancelled</dt><dd>${esc(fmtDT(cancellation.cancelled_at))} by ${esc(cancellation.cancelled_by)}
          · refund ${esc(money(cancellation.refund_amount))} (${esc(cancellation.refund_percent)}%)<br>
          <span class="muted">${esc(cancellation.reason || '')}</span></dd>` : ''}
      </dl></div>
      <div><div class="eyebrow">Tickets</div>
        <div class="table-wrap"><table class="data"><thead><tr><th>Seat</th><th>Type</th>
          <th>Code</th><th>Price</th><th>Status</th></tr></thead><tbody>${seats}</tbody></table></div>
        <div class="eyebrow" style="margin-top:14px">Payments</div>
        <div class="table-wrap"><table class="data"><thead><tr><th>Reference</th><th>Method</th>
          <th>Amount</th><th>Status</th><th>Paid at</th></tr></thead><tbody>${payments}</tbody></table></div>
      </div>
    </div>`,
  `<button class="btn ghost" data-act="close-modal" type="button">Close</button>
   ${booking.status === 'CONFIRMED' ? `<button class="btn danger" data-act="cancel-booking" data-id="${booking.booking_id}" type="button">Cancel this booking</button>` : ''}`));
}

async function openCancelModal(bookingId) {
  const preview = await api.get(`/api/bookings/${bookingId}/refund-preview`);
  const tiers = preview.policies.map((policy) => `<tr>
      <td>${policy.min_hours_before === 0 ? 'Less than 2 hours before the show'
        : `At least ${policy.min_hours_before} hours before the show`}</td>
      <td class="num">${esc(policy.refund_percent)}%</td>
      <td>${preview.percent === policy.refund_percent ? '<span class="chip cyan">applied now</span>' : ''}</td></tr>`).join('');
  openModal(modalShell(`Cancel booking #${bookingId}`, 'Refund preview from the database', `
    <div class="grid two">
      <div>
        <div class="row between"><span>Show starts</span><b>${esc(fmtDT(preview.start_time))}</b></div>
        <div class="row between"><span>Hours still to go</span><b>${esc(preview.hours_to_show)} h</b></div>
        <div class="row between"><span>Amount paid</span><b>${esc(money(preview.paid))}</b></div>
        <div class="row between"><span>Refund tier applied</span><b>${esc(preview.percent)}%</b></div>
        <hr>
        <div class="row between"><span class="display" style="font-size:15px">Refund</span>
          <span class="total">${esc(money(preview.refund_amount))}</span></div>
        <p class="small muted">${esc(preview.explain)}</p>
        ${preview.can_cancel ? '' : '<p class="bad small">This booking cannot be cancelled (already cancelled or the show has started).</p>'}
      </div>
      <div><div class="eyebrow">cancellation_policy</div>
        <div class="table-wrap"><table class="data"><thead><tr><th>Rule</th><th>Refund</th><th></th></tr></thead>
          <tbody>${tiers}</tbody></table></div>
        <label class="field" style="margin-top:14px"><span>Reason</span>
          <input id="cancelReason" placeholder="Plan changed" value="Plan changed"></label>
        <label class="field"><span>Cancelled by</span><select id="cancelBy">
          <option value="CUSTOMER">Customer</option><option value="STAFF">Staff</option>
          <option value="SYSTEM">System</option></select></label>
      </div>
    </div>`,
  `<button class="btn ghost" data-act="close-modal" type="button">Keep the booking</button>
   <button class="btn danger" data-act="confirm-cancel" data-id="${bookingId}" type="button"
     ${preview.can_cancel ? '' : 'disabled'}>Confirm cancellation</button>`));
}

async function confirmCancel(bookingId) {
  try {
    const result = await api.post(`/api/bookings/${bookingId}/cancel`, {
      reason: ($('#cancelReason') || {}).value || 'Cancelled', cancelled_by: ($('#cancelBy') || {}).value || 'CUSTOMER',
    });
    closeModal();
    toast(result.message, 'ok');
    toggleLog(true);
    render();
  } catch (error) { toast(error.message, 'error'); }
}

/* ------------------------------------------------------ food & drinks ----- */
async function renderFood() {
  const [items, orders] = await Promise.all([api.get('/api/items'), api.get('/api/orders?limit=8')]);
  const cart = state.cart || (state.cart = new Map());
  const bookings = await api.get(`/api/bookings?customer_id=${state.customerId}&status=CONFIRMED&limit=20`);

  const cards = items.items.map((item) => {
    const inStock = num(item.stock_qty) > 0;
    return `<article class="card accent pale">
      <div class="row between"><span class="chip">${esc(item.category)}</span>
        <span class="chip ${num(item.stock_qty) <= 10 ? 'bad' : 'good'}">stock ${esc(item.stock_qty)}</span></div>
      <h4 style="margin:10px 0 4px">${esc(item.item_name)}</h4>
      <div class="display" style="font-size:20px">${esc(money(item.unit_price))}</div>
      <div class="small muted">sold ${esc(item.sold)} so far</div>
      <div class="row" style="margin-top:8px">
        <button class="btn small ghost" data-act="cart-minus" data-id="${item.item_id}" type="button" ${inStock ? '' : 'disabled'}>−</button>
        <span class="mono" id="qty-${item.item_id}">${esc(cart.get(item.item_id) || 0)}</span>
        <button class="btn small" data-act="cart-plus" data-id="${item.item_id}" type="button" ${inStock ? '' : 'disabled'}>+</button>
        <button class="btn small primary" data-act="cart-add" data-id="${item.item_id}" type="button" ${inStock ? '' : 'disabled'}>Add</button>
      </div></article>`;
  }).join('');

  $('#view').innerHTML = `
    <div class="grid split">
      <section>
        <div class="card accent">
          <div class="eyebrow">Concession stand</div>
          <h1>Food &amp; Drinks</h1>
          <p class="muted small">Stock is a real database column: placing an order runs trigger
            <span class="mono">trg_order_item_stock</span>, which copies the price from the item master and
            decreases <span class="mono">stock_qty</span>. The CHECK <span class="mono">stock_qty &gt;= 0</span>
            makes over-selling impossible.</p>
        </div>
        <div class="grid three" style="margin-top:16px">${cards}</div>
      </section>
      <aside class="card accent blue summary">
        <div class="eyebrow" style="margin:0">Cart</div>
        <h3 style="margin:0">Your order</h3>
        <div id="cartBody" class="small muted">Cart is empty.</div>
        <hr>
        <div class="line"><span>Total</span><span class="total" id="cartTotal">${esc(money(0))}</span></div>
        <label class="field"><span>Attach to a booking (optional)</span>
          <select id="cartBooking"><option value="">Counter sale — no booking</option>
            ${bookings.bookings.map((b) => `<option value="${b.booking_id}">#${b.booking_id} · ${esc(b.movie_title)} · ${esc(fmtTime(b.start_time))}</option>`).join('')}
          </select></label>
        <label class="field"><span>Payment method</span>
          <select id="cartMethod">${state.meta.payment_methods.map((m) => `<option value="${m}">${esc(m)}</option>`).join('')}</select></label>
        <button class="btn primary" data-act="place-order" type="button">Pay for the order</button>
        <button class="btn ghost small" data-act="cart-clear" type="button">Empty cart</button>
      </aside>
    </div>
    <section class="card accent good" style="margin-top:18px">
      <div class="eyebrow">Recent orders</div>
      <div class="table-wrap"><table class="data"><thead><tr><th>Order</th><th>When</th><th>Booking</th>
        <th>Items</th><th>Total</th><th>Status</th><th></th></tr></thead><tbody>
        ${orders.orders.map((order) => `<tr>
          <td class="mono">#${esc(order.order_id)}</td><td class="mono">${esc(fmtDT(order.order_time))}</td>
          <td>${order.booking_id ? '#' + esc(order.booking_id) + '<div class="small muted">' + esc(order.customer_name || '') + '</div>' : '<span class="muted small">counter sale</span>'}</td>
          <td class="small">${esc(order.summary || '—')}</td>
          <td class="num">${esc(money(order.total))}</td>
          <td><span class="chip good">${esc(order.status)}</span></td>
          <td><button class="btn small ghost" data-act="view-order" data-id="${order.order_id}" type="button">Lines</button></td>
        </tr>`).join('') || '<tr><td colspan="7" class="muted">No orders yet.</td></tr>'}
      </tbody></table></div>
    </section>`;

  const refreshCart = () => {
    const lines = Array.from(cart.entries()).map(([id, qty]) => {
      const item = items.items.find((i) => i.item_id === id);
      return { item, qty };
    }).filter((line) => line.item && line.qty > 0);
    const total = lines.reduce((sum, line) => sum + num(line.item.unit_price) * line.qty, 0);
    $('#cartTotal').textContent = money(total);
    $('#cartBody').innerHTML = lines.length ? lines.map((line) =>
      `<div class="row between"><span>${esc(line.item.item_name)} × ${line.qty}</span>
       <span class="mono">${esc(money(num(line.item.unit_price) * line.qty))}</span></div>`).join('')
      : 'Cart is empty.';
    $$('[id^="qty-"]').forEach((node) => {
      const id = Number(node.id.replace('qty-', ''));
      node.textContent = String(cart.get(id) || 0);
    });
  };
  refreshCart();

  $('#view').addEventListener('click', async (event) => {
    const button = event.target.closest('button[data-act]');
    if (!button) return;
    const id = Number(button.dataset.id);
    const item = items.items.find((i) => i.item_id === id);
    if (button.dataset.act === 'cart-plus') { cart.set(id, (cart.get(id) || 0) + 1); refreshCart(); }
    if (button.dataset.act === 'cart-minus') {
      const next = (cart.get(id) || 0) - 1;
      if (next <= 0) cart.delete(id); else cart.set(id, next);
      refreshCart();
    }
    if (button.dataset.act === 'cart-add') { cart.set(id, (cart.get(id) || 0) + 1); refreshCart(); toast(`${esc(item.item_name)} added to the cart.`); }
    if (button.dataset.act === 'cart-clear') { cart.clear(); refreshCart(); }
    if (button.dataset.act === 'view-order') { await openOrderModal(id); }
    if (button.dataset.act === 'place-order') {
      if (!cart.size) { toast('Add at least one item to the cart first.', 'error'); return; }
      try {
        const result = await api.post('/api/orders', {
          items: Array.from(cart.entries()).map(([item_id, quantity]) => ({ item_id, quantity })),
          method: $('#cartMethod').value,
          booking_id: $('#cartBooking').value || null,
          staff_id: state.role === 'STAFF' ? (state.meta.staff[0] || {}).staff_id : null,
        });
        cart.clear();
        toast(result.message, 'ok');
        toggleLog(true);
        render();
      } catch (error) { toast(error.message, 'error'); }
    }
  });
}

async function openOrderModal(orderId) {
  const payload = await api.get('/api/orders?limit=200');
  const detail = payload.orders.find((order) => order.order_id === orderId) || {};
  openModal(modalShell(`Food order #${orderId}`, 'concession_order + order_item', `
    <dl class="kv">
      <dt>Placed</dt><dd>${esc(fmtDT(detail.order_time))}</dd>
      <dt>Booking</dt><dd>${detail.booking_id ? '#' + esc(detail.booking_id) : 'counter sale'}</dd>
      <dt>Staff</dt><dd>${esc(detail.staff_name || 'online')}</dd>
      <dt>Items</dt><dd>${esc(detail.summary || '—')}</dd>
      <dt>Total</dt><dd>${esc(money(detail.total))}</dd>
      <dt>Status</dt><dd>${esc(detail.status)}</dd>
    </dl>`,
  `<button class="btn ghost" data-act="close-modal" type="button">Close</button>`));
}

/* ---------------------------------------------------------- counter ------- */
/* The counter is the same screen as Food & Drinks: the only difference is that a
   staff member can attach the sale to a booking or leave it as a walk-up sale. */
async function renderCounter() {
  await renderFood();
  const heading = $('.card.accent .eyebrow');
  if (heading) heading.textContent = 'Concession counter · staff';
}

/* --------------------------------------------------------- customers ------ */
async function renderCustomers() {
  const payload = await api.get('/api/customers?limit=300');
  const rows = payload.customers.map((customer) => `<tr>
      <td class="mono">${esc(customer.customer_id)}</td>
      <td>${esc(customer.full_name)}${customer.customer_id === 1 ? ' <span class="chip cyan">shared</span>' : ''}</td>
      <td class="mono small">${esc(customer.email)}</td>
      <td class="mono small">${esc(customer.phone)}</td>
      <td class="num">${esc(customer.bookings)}</td>
      <td class="num">${esc(customer.cancelled)}</td>
      <td class="num">${esc(money(customer.spend))}</td>
      <td class="mono small">${esc(fmtDate(customer.created_at))}</td>
      <td class="right nowrap">
        <button class="btn small ghost" data-act="edit-customer" data-id="${customer.customer_id}" type="button">Edit</button>
        <button class="btn small danger" data-act="delete-customer" data-id="${customer.customer_id}" type="button">Delete</button>
      </td></tr>`).join('');
  $('#view').innerHTML = `
    <section class="card accent">
      <div class="eyebrow">Customers</div>
      <div class="row between"><div><h1>${esc(payload.count)} registered patrons</h1>
        <p class="muted small" style="margin:0">customer_id = 1 is the shared Walk-in Guest used by counter sales and
          cannot be deleted. Every other customer disappears only when no booking references them
          (ON DELETE RESTRICT).</p></div>
        <button class="btn primary" data-act="new-customer" type="button">Register customer</button></div>
      <hr>
      <label class="field" style="max-width:320px"><span>Search</span>
        <input id="custSearch" placeholder="name, e-mail or phone" value="${escAttr(state.customerQuery || '')}"></label>
      <div class="table-wrap"><table class="data"><thead><tr><th>#</th><th>Name</th><th>E-mail</th>
        <th>Phone</th><th>Bookings</th><th>Cancelled</th><th>Spend</th><th>Since</th><th></th></tr></thead>
        <tbody>${rows}</tbody></table></div>
    </section>`;
  const search = $('#custSearch');
  search.addEventListener('input', () => {
    const needle = search.value.trim().toLowerCase();
    state.customerQuery = search.value;
    $$('table.data tbody tr').forEach((row) => {
      row.classList.toggle('hidden', needle && !row.textContent.toLowerCase().includes(needle));
    });
  });
}

/* --------------------------------------------------------- dashboard ------ */
async function renderDashboard() {
  const payload = await api.get('/api/dashboard');
  const kpis = payload.kpis;
  const kpi = (label, value, foot, accent) => `<article class="card kpi accent ${accent || ''}">
    <div class="label">${esc(label)}</div><div class="value">${value}</div>
    <div class="foot">${esc(foot || '')}</div></article>`;
  $('#view').innerHTML = `
    <section class="card accent">
      <div class="eyebrow">Administrator</div>
      <div class="row between"><div><h1>Cinema dashboard</h1>
        <p class="muted small" style="margin:0">Net revenue = tickets − refunds + concessions, computed live from
          <span class="mono">v_daily_revenue</span>. Database holds
          <b>${esc(payload.rows.toLocaleString('en-IN'))} rows</b> in ${esc(payload.tables)} tables.</p></div>
        <span class="badge"><span class="dot"></span>clock ${esc(fmtDT(payload.cinema_now))}</span></div>
    </section>
    <div class="grid kpis" style="margin-top:18px">
      ${kpi('Net revenue · 30 days', esc(shortMoney(kpis.net_revenue)), money(kpis.net_revenue), 'good')}
      ${kpi('Ticket revenue', esc(shortMoney(kpis.ticket_revenue)), money(kpis.ticket_revenue))}
      ${kpi('Concessions', esc(shortMoney(kpis.concession_revenue)), money(kpis.concession_revenue), 'pale')}
      ${kpi('Refunds', esc(shortMoney(kpis.refunds)), money(kpis.refunds), 'bad')}
      ${kpi('Average occupancy', esc(num(kpis.avg_occupancy_pct).toFixed(1)) + '%', `${esc(kpis.shows_last_30_days)} shows · ${esc(num(kpis.avg_occupancy_upcoming).toFixed(1))}% upcoming`)}
      ${kpi('Active bookings', esc(num(kpis.active_bookings).toLocaleString('en-IN')), `${esc(kpis.cancelled_bookings)} cancelled`)}
      ${kpi('Upcoming shows', esc(kpis.upcoming_shows), `${esc(kpis.total_shows)} shows ever scheduled`, 'blue')}
      ${kpi('Customers', esc(kpis.customers), `${esc(kpis.staff)} staff`)}
      ${kpi('Tickets sold', esc(num(kpis.tickets).toLocaleString('en-IN')), 'ISSUED + USED')}
      ${kpi('Seats', esc(kpis.seats), `${esc(kpis.screens)} screens · ${esc(kpis.movies)} movies`)}
      ${kpi('Food orders', esc(num(kpis.food_orders).toLocaleString('en-IN')), `${esc(num(kpis.items_sold).toLocaleString('en-IN'))} items sold`, 'pale')}
      ${kpi('Low stock items', esc(kpis.low_stock), 'stock_qty ≤ 20', kpis.low_stock ? 'bad' : 'good')}
    </div>

    <section class="card accent blue" style="margin-top:18px">
      <div class="row between"><div><div class="eyebrow">14-day trend</div>
        <h2 style="margin:0">Revenue per day</h2></div>
        <span class="chip cyan">v_daily_revenue</span></div>
      ${barChart(payload.trend, { value: 'net_revenue', label: 'day', title: 'net revenue per day' })}
      <div class="row small muted"><span>◼ net revenue (tickets − refunds + concessions)</span></div>
    </section>

    <div class="grid two" style="margin-top:18px">
      <section class="card accent">
        <div class="eyebrow">Next shows</div>
        <div class="table-wrap"><table class="data"><thead><tr><th>When</th><th>Movie</th>
          <th>Screen</th><th>Free</th><th>Occ.</th></tr></thead><tbody>
          ${payload.next_shows.map((show) => `<tr><td class="mono">${esc(fmtDT(show.start_time))}</td>
            <td>${esc(show.movie)}</td><td>${esc(show.screen_name)}<div class="small muted">${esc(show.screen_type)}</div></td>
            <td class="num">${esc(show.seats_left)}</td><td class="num">${esc(num(show.occupancy_pct).toFixed(1))}%</td></tr>`).join('')}
        </tbody></table></div>
      </section>
      <section class="card accent pale">
        <div class="eyebrow">Top movies</div>
        ${hBarChart(payload.top_movies, 'title', 'ticket_revenue')}
        <div class="eyebrow" style="margin-top:16px">Payment mix</div>
        ${hBarChart(payload.payment_mix, 'method', 'amount')}
      </section>
    </div>

    <section class="card accent" style="margin-top:18px">
      <div class="eyebrow">Occupancy by format</div>
      ${hBarChart(payload.formats, 'screen_type', 'occupancy_pct', { money: false, limit: 6 })}
    </section>`;
}

/* -------------------------------------------------------- scheduling ------ */
async function renderScheduling() {
  const date = state.schedDate || todayISO();
  state.schedDate = date;
  const payload = await api.get('/api/screenings?date=' + date + '&limit=200');
  const meta = state.meta;
  const rows = payload.screenings.map((show) => `<tr>
      <td class="mono">#${esc(show.screening_id)}</td>
      <td class="mono">${esc(fmtTime(show.start_time))} → ${esc(fmtTime(show.end_time))}</td>
      <td>${esc(show.title)}</td>
      <td>${esc(show.cinema_name)}<div class="small muted">${esc(show.screen_name)} · ${esc(show.screen_type)}</div></td>
      <td class="num">${esc(money(show.base_price))}</td>
      <td class="num">${esc(show.booked_seats)}/${esc(show.total_seats)}</td>
      <td><span class="chip ${show.status === 'SCHEDULED' ? 'good' : show.status === 'CANCELLED' ? 'bad' : ''}">${esc(show.status)}</span></td>
      <td class="right nowrap">
        <button class="btn small ghost" data-act="reschedule" data-id="${show.screening_id}" type="button">Move</button>
        <button class="btn small danger" data-act="cancel-show" data-id="${show.screening_id}" type="button">Cancel</button>
        <button class="btn small ghost" data-act="delete-show" data-id="${show.screening_id}" type="button">Delete</button>
      </td></tr>`).join('');
  $('#view').innerHTML = `
    <section class="card accent">
      <div class="eyebrow">Administrator · scheduling</div>
      <h1>Schedule a show</h1>
      <p class="muted small">end_time is computed as start + movie duration + 15 minute cleaning gap by
        <span class="mono">fn_schedule_screening()</span>. Trying to squeeze a second show into the same slot is
        refused by the EXCLUDE constraint <span class="mono">ex_screening_no_overlap</span>.</p>
      <div class="grid four">
        <label class="field"><span>Movie</span><select id="sMovie">
          ${meta.movies.map((m) => `<option value="${m.movie_id}">${esc(m.title)} · ${esc(m.duration_min)} min</option>`).join('')}</select></label>
        <label class="field"><span>Screen</span><select id="sScreen">
          ${meta.screens.map((s) => `<option value="${s.screen_id}">${esc(s.cinema_name)} · ${esc(s.screen_name)} (${esc(s.screen_type)})</option>`).join('')}</select></label>
        <label class="field"><span>Start (YYYY-MM-DD HH:MM)</span>
          <input id="sStart" value="${escAttr(date + ' 21:15')}"></label>
        <label class="field"><span>Base price (INR)</span><input id="sPrice" type="number" min="1" step="10" value="350"></label>
      </div>
      <div class="row">
        <button class="btn primary" data-act="schedule" type="button">Schedule show</button>
        <button class="btn ghost" data-act="overlap-demo" type="button">Try a deliberate overlap</button>
        <label class="row small" style="gap:6px"><span class="muted">Date shown</span>
          <input type="date" id="sDate" value="${escAttr(date)}" style="width:auto"></label>
      </div>
    </section>
    <section class="card accent blue" style="margin-top:18px">
      <div class="row between"><div><div class="eyebrow">${esc(payload.count)} shows</div>
        <h2 style="margin:0">${esc(fmtDay(date))} timetable</h2></div>
        <span class="chip cyan">EXCLUDE USING gist (screen_id WITH =, tsrange &&)</span></div>
      <div class="table-wrap"><table class="data"><thead><tr><th>#</th><th>Time</th><th>Movie</th>
        <th>Screen</th><th>Base</th><th>Sold</th><th>Status</th><th></th></tr></thead>
        <tbody>${rows || '<tr><td colspan="8" class="muted">Nothing scheduled for this date.</td></tr>'}</tbody></table></div>
    </section>`;

  $('#sDate').addEventListener('change', (event) => { state.schedDate = event.target.value; render(); });
  $('#view').addEventListener('click', async (event) => {
    const button = event.target.closest('button[data-act]');
    if (!button) return;
    const action = button.dataset.act;
    const id = Number(button.dataset.id);
    try {
      if (action === 'schedule' || action === 'overlap-demo') {
        let start = $('#sStart').value.trim();
        if (action === 'overlap-demo') {
          const show = payload.screenings[0];
          if (show) {
            $('#sScreen').value = String(show.screen_id);
            start = fmtDT(show.start_time);
            toast('Sending a show that overlaps an existing one — the database must refuse it.');
          } else { toast('Schedule one show first, then try the overlap demo.', 'error'); return; }
        }
        const result = await api.post('/api/screenings', {
          movie_id: Number($('#sMovie').value), screen_id: Number($('#sScreen').value),
          start_time: start, base_price: Number($('#sPrice').value),
        });
        toast(result.message, 'ok');
        state.schedDate = fmtDate(result.screening.start_time);
        render();
      }
      if (action === 'cancel-show') {
        const result = await api.post(`/api/screenings/${id}/cancel`, {});
        toast(result.message, 'ok'); render();
      }
      if (action === 'delete-show') {
        const result = await api.del(`/api/screenings/${id}`);
        toast(result.message, 'ok'); render();
      }
      if (action === 'reschedule') {
        const show = payload.screenings.find((s) => s.screening_id === id);
        openModal(modalShell(`Move show #${id}`, 'screening', `
          <label class="field"><span>New start (YYYY-MM-DD HH:MM)</span>
            <input id="rStart" value="${escAttr(fmtDT(show.start_time))}"></label>
          <label class="field"><span>Base price</span>
            <input id="rPrice" type="number" value="${escAttr(show.base_price)}"></label>
          <p class="small muted">A show with confirmed bookings cannot be moved, and the EXCLUDE constraint
            still applies at the new time.</p>`,
        `<button class="btn ghost" data-act="close-modal" type="button">Close</button>
         <button class="btn primary" data-act="do-reschedule" data-id="${id}" type="button">Move show</button>`));
      }
    } catch (error) { toast(error.message, 'error'); }
  });
}

/* ----------------------------------------------------------- reports ------ */
async function renderReports() {
  const [catalogue, schema] = await Promise.all([api.get('/api/queries'), api.get('/api/schema')]);
  const key = state.reports.key || 'daily_revenue';
  state.reports.key = key;
  let result = null;
  try { result = await api.get('/api/reports/' + key); } catch (error) { toast(error.message, 'error'); }

  const buttons = catalogue.queries.map((query) => `<button class="pill" data-act="run-report"
      data-key="${escAttr(query.key)}" style="${query.key === key ? 'border-color:var(--cyan);color:var(--pale)' : ''}">
      ${esc(query.title)}<small>${esc(query.kind)}</small></button>`).join('');

  const table = result ? `<div class="table-wrap" style="max-height:420px"><table class="data">
      <thead><tr>${result.columns.map((column) => `<th>${esc(column)}</th>`).join('')}</tr></thead>
      <tbody>${result.rows.map((row) => `<tr>${row.map((cell) => `<td>${cell === null
        ? '<span class="null">NULL</span>' : esc(typeof cell === 'number' ? cell.toLocaleString('en-IN') : cell)}</td>`).join('')}</tr>`).join('')}
      </tbody></table></div>` : '';
  const numeric = result && result.columns.length > 1
    ? result.columns.find((column) => !['day', 'title', 'name', 'method', 'movie', 'customer', 'screen_type', 'seat_type'].includes(column))
    : null;
  const chartable = result && numeric && result.rows.some((row) => typeof row[result.columns.indexOf(numeric)] === 'number');

  $('#view').innerHTML = `
    <section class="card accent">
      <div class="eyebrow">Administrator · reports</div>
      <h1>Named SQL queries</h1>
      <p class="muted small">${esc(catalogue.count)} queries are published by the backend. The browser can only ask
        for one of them by name — <span class="mono">GET /api/reports/&lt;key&gt;</span> — arbitrary SQL is impossible
        here (that is what the read-only console on the Database page is for).</p>
      <div class="pills">${buttons}</div>
    </section>
    ${result ? `<section class="card accent blue" style="margin-top:18px">
      <div class="row between"><div><div class="eyebrow">${esc(result.kind)}</div>
        <h2 style="margin:0">${esc(result.title)}</h2>
        <p class="muted small" style="margin:0">${esc(result.description)}</p></div>
        <span class="chip good">${esc(result.count)} row(s) · ${esc(result.ms)} ms</span></div>
      ${chartable ? barChart(result.rows, { value: numeric, label: result.columns[0], title: result.title }) : ''}
      ${table}
      <details class="disclose" style="margin-top:14px"><summary>Show the SQL</summary>
        ${codeBlock(result.sql, 'sql')}</details>
    </section>` : ''}
    <section class="card accent" style="margin-top:18px">
      <div class="eyebrow">Testing summary</div>
      <div class="grid four">
        <div class="kpi"><div class="label">Tables</div><div class="value">${esc(schema.summary.tables)}</div></div>
        <div class="kpi"><div class="label">Triggers</div><div class="value">${esc(schema.summary.triggers)}</div></div>
        <div class="kpi"><div class="label">Views</div><div class="value">${esc(schema.summary.views)}</div></div>
        <div class="kpi"><div class="label">Indexes</div><div class="value">${esc(schema.summary.indexes)}</div></div>
      </div>
      <p class="small muted" style="margin-top:12px">Run the full unittest suite from the Source Code page →
        Tests tab, or with <span class="mono">python tests/test_cinema.py</span>.</p>
    </section>`;

  $('#view').addEventListener('click', (event) => {
    const button = event.target.closest('button[data-act="run-report"]');
    if (!button) return;
    state.reports.key = button.dataset.key;
    render();
  });
}

/* -------------------------------------------------------- master data ----- */
async function renderMaster() {
  const [movies, customers, items] = await Promise.all([
    api.get('/api/movies'), api.get('/api/customers'), api.get('/api/items')]);
  $('#view').innerHTML = `
    <section class="card accent">
      <div class="eyebrow">Administrator · master data</div>
      <h1>Master data</h1>
      <p class="muted small">Full CRUD through the API. Deleting a row that other tables still reference is refused
        by the database with a readable message (foreign keys are ON DELETE RESTRICT).</p>
    </section>

    <section class="card accent blue" style="margin-top:18px">
      <div class="row between"><div><div class="eyebrow">movie</div><h2 style="margin:0">Movies</h2></div>
        <button class="btn primary small" data-act="new-movie" type="button">Add movie</button></div>
      <div class="table-wrap"><table class="data"><thead><tr><th>#</th><th>Title</th><th>Genre</th>
        <th>Language</th><th>Min</th><th>Cert</th><th>Released</th><th>Shows</th><th></th></tr></thead><tbody>
        ${movies.movies.map((movie) => `<tr>
          <td class="mono">${esc(movie.movie_id)}</td><td>${esc(movie.title)}</td><td>${esc(movie.genre)}</td>
          <td>${esc(movie.language)}</td><td class="num">${esc(movie.duration_min)}</td>
          <td><span class="chip">${esc(movie.certificate)}</span></td><td class="mono">${esc(fmtDate(movie.release_date))}</td>
          <td class="num">${esc(movie.screenings)}</td>
          <td class="right nowrap"><button class="btn small ghost" data-act="edit-movie" data-id="${movie.movie_id}" type="button">Edit</button>
            <button class="btn small danger" data-act="delete-movie" data-id="${movie.movie_id}" type="button">Delete</button></td>
        </tr>`).join('')}</tbody></table></div>
    </section>

    <section class="card accent pale" style="margin-top:18px">
      <div class="row between"><div><div class="eyebrow">concession_item</div><h2 style="margin:0">Menu &amp; stock</h2></div>
        <button class="btn primary small" data-act="new-item" type="button">Add item</button></div>
      <div class="table-wrap"><table class="data"><thead><tr><th>#</th><th>Item</th><th>Category</th>
        <th>Price</th><th>Stock</th><th>Sold</th><th></th></tr></thead><tbody>
        ${items.items.map((item) => `<tr><td class="mono">${esc(item.item_id)}</td><td>${esc(item.item_name)}</td>
          <td><span class="chip">${esc(item.category)}</span></td><td class="num">${esc(money(item.unit_price))}</td>
          <td class="num ${num(item.stock_qty) <= 10 ? 'bad' : ''}">${esc(item.stock_qty)}</td>
          <td class="num">${esc(item.sold)}</td>
          <td class="right nowrap"><button class="btn small ghost" data-act="edit-item" data-id="${item.item_id}" type="button">Edit</button>
            <button class="btn small danger" data-act="delete-item" data-id="${item.item_id}" type="button">Delete</button></td>
        </tr>`).join('')}</tbody></table></div>
    </section>

    <section class="card accent good" style="margin-top:18px">
      <div class="row between"><div><div class="eyebrow">customer</div><h2 style="margin:0">Customers</h2></div>
        <button class="btn primary small" data-act="new-customer" type="button">Register customer</button></div>
      <div class="table-wrap"><table class="data"><thead><tr><th>#</th><th>Name</th><th>E-mail</th>
        <th>Phone</th><th>Bookings</th><th>Spend</th><th></th></tr></thead><tbody>
        ${customers.customers.map((customer) => `<tr><td class="mono">${esc(customer.customer_id)}</td>
          <td>${esc(customer.full_name)}</td><td class="mono small">${esc(customer.email)}</td>
          <td class="mono small">${esc(customer.phone)}</td><td class="num">${esc(customer.bookings)}</td>
          <td class="num">${esc(money(customer.spend))}</td>
          <td class="right nowrap"><button class="btn small ghost" data-act="edit-customer" data-id="${customer.customer_id}" type="button">Edit</button>
            <button class="btn small danger" data-act="delete-customer" data-id="${customer.customer_id}" type="button">Delete</button></td>
        </tr>`).join('')}</tbody></table></div>
    </section>`;

  const form = (fields, values, act, id) => `
    ${fields.map(([key, label, type, extra]) => type === 'select'
      ? `<label class="field"><span>${esc(label)}</span><select id="f-${esc(key)}">
          ${extra.map((option) => `<option value="${escAttr(option)}" ${String(values[key]) === String(option) ? 'selected' : ''}>${esc(option)}</option>`).join('')}
        </select></label>`
      : `<label class="field"><span>${esc(label)}</span>
          <input id="f-${esc(key)}" type="${esc(type)}" value="${escAttr(values[key] === undefined ? '' : values[key])}"></label>`).join('')}
    <div class="foot"><button class="btn ghost" data-act="close-modal" type="button">Cancel</button>
      <button class="btn primary" data-act="${esc(act)}" data-id="${esc(id || '')}" type="button">Save</button></div>`;

  $('#view').addEventListener('click', async (event) => {
    const button = event.target.closest('button[data-act]');
    if (!button) return;
    const action = button.dataset.act;
    const id = Number(button.dataset.id);
    try {
      if (action === 'new-movie' || action === 'edit-movie') {
        const movie = action === 'edit-movie' ? movies.movies.find((m) => m.movie_id === id) : {};
        openModal(modalShell(action === 'edit-movie' ? `Edit movie #${id}` : 'Add a movie', 'movie',
          form([['title', 'Title', 'text'], ['genre', 'Genre', 'text'], ['language', 'Language', 'text'],
            ['duration_min', 'Duration (minutes)', 'number'], ['certificate', 'Certificate', 'select', ['U', 'UA', 'A', 'S']],
            ['release_date', 'Release date', 'date']], movie || {}, 'save-movie', id)));
      }
      if (action === 'delete-movie') {
        const result = await api.del('/api/movies/' + id); toast(result.message, 'ok'); render();
      }
      if (action === 'new-item' || action === 'edit-item') {
        const item = action === 'edit-item' ? items.items.find((i) => i.item_id === id) : {};
        openModal(modalShell(action === 'edit-item' ? `Edit item #${id}` : 'Add a menu item', 'concession_item',
          form([['item_name', 'Item name', 'text'], ['category', 'Category', 'select', ['POPCORN', 'BEVERAGE', 'SNACK', 'COMBO']],
            ['unit_price', 'Unit price (INR)', 'number'], ['stock_qty', 'Stock quantity', 'number']],
          item || {}, 'save-item', id)));
      }
      if (action === 'delete-item') {
        const result = await api.del('/api/items/' + id); toast(result.message, 'ok'); render();
      }
      if (action === 'new-customer' || action === 'edit-customer') {
        const customer = action === 'edit-customer'
          ? customers.customers.find((c) => c.customer_id === id) : {};
        openModal(modalShell(action === 'edit-customer' ? `Edit customer #${id}` : 'Register a customer', 'customer',
          form([['full_name', 'Full name', 'text'], ['email', 'E-mail', 'email'], ['phone', 'Phone (10-15 digits)', 'text']],
            customer || {}, 'save-customer', id)));
      }
      if (action === 'delete-customer') {
        const result = await api.del('/api/customers/' + id); toast(result.message, 'ok'); render();
      }
    } catch (error) { toast(error.message, 'error'); }
  });
}

/* ---------------------------------------------------------- database ------ */
async function renderDatabase() {
  const overview = await api.get('/api/db/tables');
  const name = db_tables_has(state.db.table) ? state.db.table : 'booking';
  state.db.table = name;
  const table = await api.get(`/api/db/tables/${name}?limit=${state.db.limit}&offset=${state.db.offset}` +
    `&q=${encodeURIComponent(state.db.q)}&sort=${encodeURIComponent(state.db.sort || '')}&dir=${state.db.dir}`);
  const objects = await api.get('/api/db/objects');

  const list = overview.tables.map((item) => `<button class="tree-row ${item.name === name ? 'active' : ''}"
      data-act="pick-table" data-name="${escAttr(item.name)}"
      style="display:flex;width:100%;gap:8px;align-items:center;text-align:left;background:transparent;border:1px solid ${item.name === name ? 'var(--cyan)' : 'transparent'};border-radius:9px;color:var(--text);padding:7px 9px;cursor:pointer;font-family:'JetBrains Mono',monospace;font-size:12.5px">
      <span>${esc(item.name)}</span><span class="grow"></span>
      <span class="chip">${esc(item.row_count.toLocaleString('en-IN'))}</span></button>`).join('');

  const columnRows = table.columns.map((column) => `<tr>
      <td class="mono">${esc(column.name)}</td>
      <td class="mono small">${esc(column.data_type)}</td>
      <td>${column.is_pk ? '<span class="chip good">PK</span>' : ''}
        ${column.is_fk ? `<span class="chip cyan">FK → ${esc(column.references)}</span>` : ''}
        ${column.is_unique && !column.is_pk ? '<span class="chip bad">U</span>' : ''}
        ${column.not_null ? '<span class="chip">NOT NULL</span>' : '<span class="chip warn">NULL ok</span>'}</td>
      <td class="mono small">${esc(column.default_value || '—')}</td>
      <td class="small muted">${esc(column.check || column.comment || '')}</td></tr>`).join('');

  const fk = table.fk_columns || {};
  const dataRows = table.rows.map((row) => `<tr>${table.columns.map((column, index) => {
    const value = row[index];
    if (value === null || value === undefined) return '<td><span class="null">NULL</span></td>';
    if (fk[column.name]) {
      return `<td><span class="fk-chip" title="${escAttr(fk[column.name].table + '.' + fk[column.name].column)}">${esc(value)}</span></td>`;
    }
    const text = typeof value === 'number' ? value.toLocaleString('en-IN') : String(value);
    const cut = text.length > 60 ? text.slice(0, 60) + '…' : text;
    return `<td class="${column.data_type.includes('int') || column.data_type.includes('numeric') ? 'num' : ''}" title="${escAttr(text)}">${esc(cut)}</td>`;
  }).join('')}</tr>`).join('');

  const dataHeader = table.columns.map((column) => `<th data-act="sort-col" data-col="${escAttr(column.name)}"
      title="Sort by ${escAttr(column.name)}">${esc(column.name)}${state.db.sort === column.name
        ? (state.db.dir === 'asc' ? ' ▲' : ' ▼') : ''}${column.is_pk ? ' · PK' : ''}</th>`).join('');

  const pages = Math.ceil(table.row_count / state.db.limit) || 1;
  const page = Math.floor(state.db.offset / state.db.limit) + 1;

  const objectCards = [
    ['Views', objects.views, (view) => `${esc(view.name)} <span class="chip">${esc(view.row_count)} rows</span>`,
      (view) => view.sql],
    ['Triggers', objects.triggers, (trigger) => `${esc(trigger.name)} <span class="chip blue">${esc(trigger.timing)} ${esc(trigger.event)} ON ${esc(trigger.table_name)}</span>`,
      (trigger) => trigger.function_sql],
    ['Functions', objects.functions, (object) => `${esc(object.name)}(${esc(object.arguments)}) → ${esc(object.returns)}`,
      (object) => object.sql],
    ['Indexes', objects.indexes, (index) => `${esc(index.name)} <span class="chip">${esc(index.table_name)}</span>`,
      (index) => `${esc(index.definition)};`],
  ].map(([title, rows, label, sql]) => `
    <details class="disclose"><summary>${esc(title)} · ${rows.length}</summary>
      ${rows.map((row) => `<details class="disclose" style="background:transparent">
        <summary style="text-transform:none;letter-spacing:0;font-family:'JetBrains Mono',monospace;font-size:12.5px">${label(row)}</summary>
        ${codeBlock(sql(row) || '-- not available', 'sql')}</details>`).join('')}
    </details>`).join('');

  $('#view').innerHTML = `
    <section class="card accent">
      <div class="row between">
        <div><div class="eyebrow">Database</div><h1>Every table in Supabase</h1>
          <p class="muted small" style="margin:0">Row Level Security is ON for all 17 tables with zero policies, so
            neither the anon key nor a browser can read a single row. This page goes through the Flask API.</p></div>
        <div class="row">
          <span class="badge"><span class="dot"></span><b>${esc(overview.count)}</b> tables</span>
          <span class="badge"><b>${esc(overview.total_rows.toLocaleString('en-IN'))}</b> rows</span>
          <button class="btn small primary" data-act="show-er" type="button">ER diagram</button>
        </div>
      </div>
    </section>

    <div class="grid split-wide" style="margin-top:18px">
      <aside class="card flat">
        <div class="eyebrow">All 17 tables</div>
        <input id="tableSearch" placeholder="filter tables…" value="${escAttr('')}" style="margin-bottom:10px">
        <div class="stack">${list}</div>
        <hr>
        <div class="eyebrow">Write access</div>
        <p class="small muted">Read-only here by design. Every write goes through a validated endpoint so the
          database rules (triggers, constraints) always run.</p>
      </aside>

      <section class="card accent blue">
        <div class="row between">
          <div><div class="eyebrow">${esc(table.label)}</div><h2 style="margin:0" class="mono">${esc(table.table)}</h2>
            <div class="small muted">${esc(table.comment || '')}</div></div>
          <div class="row">
            <button class="btn small ${state.db.tab === 'structure' ? 'primary' : 'ghost'}" data-act="db-tab" data-tab="structure" type="button">Structure</button>
            <button class="btn small ${state.db.tab === 'data' ? 'primary' : 'ghost'}" data-act="db-tab" data-tab="data" type="button">Data</button>
          </div>
        </div>
        <hr>
        ${state.db.tab === 'structure' ? `
          <div class="table-wrap"><table class="data"><thead><tr><th>Column</th><th>Type</th><th>Keys</th>
            <th>Default</th><th>Rule / note</th></tr></thead><tbody>${columnRows}</tbody></table></div>
          <div class="eyebrow" style="margin-top:16px">CREATE TABLE (live from pg_catalog)</div>
          ${codeBlock(table.ddl, 'sql')}`
        : `
          <div class="row between">
            <input id="rowSearch" placeholder="search inside ${escAttr(table.table)}…" value="${escAttr(state.db.q)}" style="max-width:340px">
            <div class="row small muted"><span>${esc(table.row_count.toLocaleString('en-IN'))} row(s) match of
              ${esc(table.total_count.toLocaleString('en-IN'))}</span>
              <span class="mono">limit ${esc(state.db.limit)} max 100</span></div>
          </div>
          <div class="table-wrap" style="margin-top:10px;max-height:60vh">
            <table class="data"><thead><tr>${dataHeader}</tr></thead><tbody>${dataRows ||
              '<tr><td class="muted">No rows match the search.</td></tr>'}</tbody></table></div>
          <div class="row between" style="margin-top:12px">
            <div class="row">
              <button class="btn small ghost" data-act="page" data-dir="-1" type="button" ${page <= 1 ? 'disabled' : ''}>← Prev</button>
              <span class="mono small">page ${esc(page)} / ${esc(pages)}</span>
              <button class="btn small ghost" data-act="page" data-dir="1" type="button" ${page >= pages ? 'disabled' : ''}>Next →</button>
            </div>
            <div class="row small muted"><span>Click a column header to sort · FK values are chips</span></div>
          </div>`}
      </section>
    </div>

    <section class="card accent pale" style="margin-top:18px">
      <div class="eyebrow">Views, Triggers, Functions, Indexes</div>
      <h2>Everything the database runs by itself</h2>
      ${objectCards}
    </section>

    <section class="card accent good" style="margin-top:18px">
      <div class="eyebrow">Read-only SQL console</div>
      <div class="row between"><h2 style="margin:0">Try a query</h2>
        <span class="chip good">one SELECT/WITH · 3 s timeout · max 200 rows</span></div>
      <p class="small muted">INSERT, UPDATE, DELETE, DROP, ALTER, GRANT and COPY are refused, multiple statements are
        refused, and the whole statement runs inside a READ ONLY transaction that is never written to the SQL log.</p>
      <div class="pills" style="margin-bottom:10px">
        <button class="pill" data-act="example" data-sql="SELECT m.title, count(*) AS shows FROM screening s JOIN movie m USING (movie_id) GROUP BY m.title ORDER BY shows DESC" type="button">group by</button>
        <button class="pill" data-act="example" data-sql="SELECT b.booking_id, c.full_name, m.title, sum(t.price) AS paid FROM booking b JOIN customer c USING (customer_id) JOIN screening s USING (screening_id) JOIN movie m USING (movie_id) JOIN booked_seat bs ON bs.booking_id = b.booking_id JOIN ticket t ON t.booked_seat_id = bs.booked_seat_id GROUP BY b.booking_id, c.full_name, m.title ORDER BY paid DESC LIMIT 20" type="button">4-table join</button>
        <button class="pill" data-act="example" data-sql="SELECT * FROM v_show_occupancy ORDER BY occupancy_pct DESC LIMIT 15" type="button">a view</button>
        <button class="pill" data-act="example" data-sql="SELECT seat_type, count(*) FROM seat GROUP BY seat_type" type="button">select</button>
        <button class="pill" data-act="example" data-sql="SELECT * FROM booking LIMIT 5" type="button">with semicolon</button>
      </div>
      <textarea id="sqlBox" spellcheck="false">SELECT m.title, count(*) AS shows FROM screening s JOIN movie m USING (movie_id) GROUP BY m.title ORDER BY shows DESC LIMIT 10</textarea>
      <div class="row" style="margin-top:10px">
        <button class="btn primary" data-act="run-sql" type="button">Run query</button>
        <span id="sqlMeta" class="small muted"></span>
      </div>
      <div id="sqlResult" style="margin-top:12px"></div>
    </section>`;

  const search = $('#tableSearch');
  search.addEventListener('input', () => {
    const needle = search.value.trim().toLowerCase();
    $$('[data-act="pick-table"]').forEach((button) => {
      button.classList.toggle('hidden', needle && !button.dataset.name.includes(needle));
    });
  });
  if ($('#rowSearch')) {
    let timer = null;
    $('#rowSearch').addEventListener('input', (event) => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        state.db.q = event.target.value.trim();
        state.db.offset = 0;
        render();
      }, 420);
    });
  }
  const sqlBox = $('#sqlBox');
  const store = state.db.sqlText;
  if (store) sqlBox.value = store;

  $('#view').addEventListener('click', async (event) => {
    const button = event.target.closest('button[data-act]');
    if (!button) return;
    const action = button.dataset.act;
    try {
      if (action === 'pick-table') {
        state.db.table = button.dataset.name; state.db.offset = 0; state.db.sort = ''; state.db.q = '';
        render();
      }
      if (action === 'db-tab') { state.db.tab = button.dataset.tab; render(); }
      if (action === 'sort-col') {
        const column = button.dataset.col;
        if (state.db.sort === column) state.db.dir = state.db.dir === 'asc' ? 'desc' : 'asc';
        else { state.db.sort = column; state.db.dir = 'asc'; }
        render();
      }
      if (action === 'page') {
        state.db.offset = Math.max(0, state.db.offset + Number(button.dataset.dir) * state.db.limit);
        render();
      }
      if (action === 'example') { sqlBox.value = button.dataset.sql; sqlBox.focus(); }
      if (action === 'show-er') {
        openModal(modalShell('Entity Relationship diagram', '17 entities · 20 relationships', `
          <p class="small muted">Generated by hand from <span class="mono">make_er.py</span> — no diagram library.
            Crow's feet show cardinality, PK/FK/U markers show keys.</p>
          <div style="overflow:auto;border:1px solid var(--line);border-radius:12px;background:#050A14;padding:8px">
            <img src="static/er.svg" alt="Entity relationship diagram of the 17 Galaxy Cinemas tables"
              style="min-width:1100px;width:100%;height:auto;display:block">
          </div>`, `<button class="btn ghost" data-act="close-modal" type="button">Close</button>`), 'full');
      }
      if (action === 'run-sql') {
        state.db.sqlText = sqlBox.value;
        const meta = $('#sqlMeta');
        meta.textContent = 'running…';
        try {
          const result = await api.post('/api/db/query', { sql: sqlBox.value });
          meta.innerHTML = `<span class="chip good">${esc(result.count)} row(s)</span>
            <span class="chip cyan">${esc(result.ms)} ms</span> <span class="muted">${esc(result.note)}</span>`;
          $('#sqlResult').innerHTML = result.columns.length ? `<div class="table-wrap" style="max-height:340px">
            <table class="data"><thead><tr>${result.columns.map((column) => `<th>${esc(column)}</th>`).join('')}</tr></thead>
            <tbody>${result.rows.map((row) => `<tr>${row.map((cell) => `<td>${cell === null
              ? '<span class="null">NULL</span>' : esc(String(cell))}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`
            : '<p class="small muted">The statement returned no result set.</p>';
        } catch (error) {
          meta.innerHTML = '<span class="chip bad">refused</span>';
          $('#sqlResult').innerHTML = `<div class="card accent bad" style="margin:0">
            <b class="bad">The console refused that statement</b><p class="small" style="margin:6px 0 0">${esc(error.message)}</p></div>`;
        }
      }
    } catch (error) { toast(error.message, 'error'); }
  });
}
function db_tables_has(name) {
  return ['cinema', 'screen', 'seat', 'movie', 'screening', 'customer', 'staff', 'concession_item',
    'cancellation_policy', 'booking', 'booked_seat', 'ticket', 'concession_order', 'order_item',
    'payment', 'cancellation', 'refund'].includes(name);
}

/* -------------------------------------------------------- source code ----- */
async function renderSource() {
  const catalogue = await api.get('/api/source');
  const path = state.source.path;
  const file = await api.get('/api/source/' + path);
  const groups = catalogue.groups.map((group) => `
    <div class="group">${esc(group.group)}</div>
    ${group.files.map((entry) => `<button data-act="open-file" data-path="${escAttr(entry.path)}"
      class="${entry.path === path ? 'active' : ''}">
      ${esc(entry.path.split('/').pop())}
      <span class="lines">${esc(entry.lines)} ln · ${esc(entry.language)}</span></button>`).join('')}`).join('');

  const arch = [
    ['Browser', 'static/index.html · static/app.js', 'static/app.js'],
    ['Flask API', 'app.py', 'app.py'],
    ['SQL queries', 'db.py (psycopg2, %s placeholders)', 'db.py'],
    ['Supabase PostgreSQL', 'supabase/schema.sql (17 tables, 5 triggers)', 'supabase/schema.sql'],
  ].map(([title, sub, target], index) => `${index ? '<span class="arrow">→</span>' : ''}
    <button data-act="open-file" data-path="${escAttr(target)}" type="button">
      <div class="display" style="font-size:14px">${esc(title)}</div><div class="small muted">${esc(sub)}</div></button>`).join('');

  $('#view').innerHTML = `
    <section class="card accent">
      <div class="eyebrow">Source code</div>
      <div class="row between">
        <div><h1>${esc(catalogue.count)} published files · ${esc(catalogue.total_lines.toLocaleString('en-IN'))} lines</h1>
          <p class="muted small" style="margin:0">Only these files are readable through the API. A whitelist means
            <span class="mono">.env</span>, logs, <span class="mono">.git</span> and path traversal
            (<span class="mono">../</span>) return 404 — secrets can never leak through the website.</p></div>
        <div class="row">
          <a class="btn small ghost" href="/api/source.zip">Download .zip</a>
          <button class="btn small ${state.source.present ? 'primary' : 'ghost'}" data-act="toggle-present" type="button">Presentation mode</button>
        </div>
      </div>
      <div class="arch" style="margin-top:16px">${arch}</div>
    </section>

    <div class="row" style="margin-top:16px">
      <button class="btn small ${state.source.tab === 'code' ? 'primary' : 'ghost'}" data-act="source-tab" data-tab="code" type="button">Code</button>
      <button class="btn small ${state.source.tab === 'tests' ? 'primary' : 'ghost'}" data-act="source-tab" data-tab="tests" type="button">Tests</button>
    </div>

    ${state.source.tab === 'tests' ? `
      <section class="card accent good" style="margin-top:14px">
        <div class="row between">
          <div><div class="eyebrow">tests/test_cinema.py</div><h2 style="margin:0">Automated test suite</h2>
            <p class="muted small" style="margin:0">Runs with <span class="mono">unittest</span> against a throw-away
              test schema, never against this demo data. 46 cases: constraints, triggers, rollbacks, refund tiers,
              the API surface and both integrity audits.</p></div>
          <button class="btn primary" data-act="run-tests" type="button">Run tests</button>
        </div>
        <div id="testResult" style="margin-top:14px">${state.tests ? renderTestResult(state.tests) : '<div class="loading"><span class="spinner"></span> not run in this session yet — press Run tests</div>'}</div>
      </section>` : `
    <div class="grid split-wide" style="margin-top:14px">
      <aside class="card flat"><ul class="tree">${groups}</ul></aside>
      <section class="card accent blue">
        <div class="row between">
          <div><div class="eyebrow">${esc(file.path)}</div>
            <h3 style="margin:0" class="mono">${esc(file.path.split('/').pop())} · ${esc(file.lines)} lines ·
              ${esc((file.size / 1024).toFixed(1))} KB</h3>
            <div class="small muted">${esc(file.description)}</div></div>
          <div class="row">
            <button class="btn small ghost" data-act="copy-file" type="button">Copy</button>
            ${file.functions && file.functions.length ? `<select id="jumpFn" style="width:auto">
              <option value="">Jump to function…</option>
              ${file.functions.map((fn) => `<option value="${fn.line}">${esc(fn.name)} (line ${fn.line})</option>`).join('')}
            </select>` : ''}
          </div>
        </div>
        <div class="code-toolbar" style="margin-top:12px;border:1px solid var(--line);border-radius:12px 12px 0 0">
          <label class="row small" style="gap:6px;margin:0"><span class="muted">Find in file</span>
            <input id="findBox" placeholder="text to highlight" value="${escAttr(state.source.find)}" style="width:190px"></label>
          <span id="findInfo" class="small muted"></span>
          <span class="grow"></span>
          <span class="chip cyan">${esc(file.language)}</span>
        </div>
        <div id="fileBody">${codeBlock(file.content, file.language, state.source.present ? 'present' : '')}</div>
      </section>
    </div>`}`;

  $('#view').addEventListener('click', async (event) => {
    const button = event.target.closest('button[data-act]');
    if (!button) return;
    const action = button.dataset.act;
    try {
      if (action === 'open-file') { state.source.path = button.dataset.path; state.source.find = ''; render(); }
      if (action === 'source-tab') { state.source.tab = button.dataset.tab; render(); }
      if (action === 'toggle-present') { state.source.present = !state.source.present; render(); }
      if (action === 'copy-file') {
        await navigator.clipboard.writeText(file.content);
        toast('File copied to the clipboard.', 'ok');
      }
      if (action === 'run-tests') {
        const box = $('#testResult');
        box.innerHTML = '<div class="loading"><span class="spinner"></span> running unittest against a fresh test schema…</div>';
        try {
          const result = await api.post('/api/tests/run', {});
          state.tests = result;
          box.innerHTML = renderTestResult(result);
          toast(`${result.passed} passed, ${result.failed} failed in ${result.duration}s.`,
            result.failed ? 'error' : 'ok');
        } catch (error) {
          box.innerHTML = `<div class="card accent bad" style="margin:0"><b class="bad">Test run failed</b>
            <p class="small" style="margin:6px 0 0">${esc(error.message)}</p></div>`;
        }
      }
    } catch (error) { toast(error.message, 'error'); }
  });

  if (state.source.tab === 'code' && $('#jumpFn')) {
    $('#jumpFn').addEventListener('change', (event) => {
      const line = Number(event.target.value);
      if (!line) return;
      const rows = $$('#fileBody tr');
      const target = rows[line - 1];
      if (target) {
        target.scrollIntoView({ block: 'center' });
        target.style.background = 'rgba(111,211,242,.18)';
        setTimeout(() => { target.style.background = ''; }, 1600);
      }
    });
  }
  if (state.source.tab === 'code' && $('#findBox')) {
    const box = $('#findBox');
    const run = () => {
      state.source.find = box.value;
      const needle = box.value.trim();
      const info = $('#findInfo');
      const rows = $$('#fileBody tr');
      let first = null, count = 0;
      rows.forEach((row) => {
        const cell = row.lastElementChild;
        if (!needle) { cell.innerHTML = cell.dataset.plain || cell.innerHTML; row.style.background = ''; return; }
        if (!cell.dataset.plain) cell.dataset.plain = cell.innerHTML;
        const raw = cell.dataset.plain;
        const text = cell.textContent;
        const lower = text.toLowerCase();
        const index = lower.indexOf(needle.toLowerCase());
        if (index === -1) { cell.innerHTML = raw; return; }
        count += 1;
        if (!first) first = row;
        cell.innerHTML = raw.replace(new RegExp(needle.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi'),
          (match) => `<mark>${esc(match)}</mark>`);
      });
      info.textContent = needle ? `${count} line(s) contain "${needle}"` : '';
      if (first) first.scrollIntoView({ block: 'center' });
    };
    let timer = null;
    box.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(run, 250); });
    if (state.source.find) run();
  }
}

function renderTestResult(result) {
  const rows = (result.tests || []).map((test) => `<div class="${test.status === 'ok' ? 'pass' : 'fail'}">
    ${test.status === 'ok' ? 'PASS' : 'FAIL'} · ${esc(test.name)}${test.message ? ' — ' + esc(test.message) : ''}</div>`).join('');
  return `<div class="row between">
      <span class="chip ${result.failed ? 'bad' : 'good'}">${esc(result.passed)} passed, ${esc(result.failed)} failed</span>
      <span class="chip">${esc(result.total)} tests</span>
      <span class="chip cyan">${esc(result.duration)} s</span>
      <span class="chip">schema ${esc(result.schema || '?')}</span>
    </div>
    <div class="tests-list" style="margin-top:12px;max-height:440px;overflow:auto">${rows}</div>`;
}

/* ----------------------------------------------------------- solution ----- */
function renderSolution() {
  const meta = state.meta;
  $('#view').innerHTML = `
    <section class="card accent">
      <div class="eyebrow">Project 65 · Project-Based Learning</div>
      <h1>Design &amp; implementation report</h1>
      <p class="muted" style="margin:0">Galaxy Cinemas — a cinema screening and ticket booking system:
        ${esc(meta.database.tables)} tables, 5 triggers, 6 views, 3 SQL functions and 8 indexes, all created and enforced
        inside Supabase PostgreSQL, reached by a Flask API that only ever sends plain SQL with %s placeholders.</p>
    </section>
    <div class="solution-grid" style="margin-top:18px">
      <aside class="card flat toc">
        <div class="eyebrow">Contents</div>
        <ol>
          <li><a href="#s1">Problem &amp; objectives</a></li>
          <li><a href="#s2">Users &amp; requirements</a></li>
          <li><a href="#s3">ER diagram</a></li>
          <li><a href="#s4">Relational schema</a></li>
          <li><a href="#s5">Functional dependencies</a></li>
          <li><a href="#s6">Normalization 1NF → 3NF</a></li>
          <li><a href="#s7">Business rules → mechanisms</a></li>
          <li><a href="#s8">SQL Lab</a></li>
          <li><a href="#s9">Triggers, views, functions</a></li>
          <li><a href="#s10">Testing summary</a></li>
        </ol>
      </aside>
      <div class="stack">
        <section id="s1" class="card accent"><div class="eyebrow">1</div><h2>Problem &amp; objectives</h2>
          <p>Running a multiplex on paper or in a spreadsheet fails in four specific, expensive ways. Each one becomes
            an explicit design objective, and each objective is enforced by the database rather than by hope.</p>
          <table class="norm-table"><thead><tr><th>Anomaly</th><th>What goes wrong</th><th>Objective</th>
            <th>Mechanism</th></tr></thead><tbody>
            <tr><td>Double booking</td><td>Two customers pay for seat C4 of the same show</td>
              <td>One seat, one active booking, per show — even under concurrent traffic</td>
              <td>partial unique index <span class="mono">ux_active_seat</span> + trigger T1</td></tr>
            <tr><td>Overlapping shows</td><td>A screen is booked twice at 21:15, or back-to-back shows leave no cleaning time</td>
              <td>No two live shows may overlap on a screen</td>
              <td>EXCLUDE constraint <span class="mono">ex_screening_no_overlap</span> (btree_gist + tsrange)</td></tr>
            <tr><td>Refund and cancellation anomalies</td><td>Refunds larger than the policy allows, or a cancelled booking keeps its seats</td>
              <td>Refund is capped by the policy tier; cancelling frees seats and voids tickets atomically</td>
              <td><span class="mono">fn_refund_percent()</span> + triggers T2, T3, T4</td></tr>
            <tr><td>Stock anomalies</td><td>Selling 12 caramel popcorns when 8 are on the shelf</td>
              <td>Stock can never go negative, and never silently drifts from the order lines</td>
              <td>trigger T5 + CHECK <span class="mono">stock_qty &gt;= 0</span></td></tr>
          </tbody></table>
          <p class="small muted" style="margin-top:10px">Secondary objectives: one transaction per booking, a fixed clock
            (<span class="mono">CINEMA_NOW</span>) so the rules are testable, and full visibility of the SQL the
            application sends.</p>
        </section>

        <section id="s2" class="card accent blue"><div class="eyebrow">2</div><h2>Users &amp; functional requirements</h2>
          <table class="norm-table"><thead><tr><th>Role</th><th>Functional requirements</th><th>Tables touched</th></tr></thead>
          <tbody>
            <tr><td><b>Customer</b></td><td>Browse shows by date/cinema/movie; pick seats from the map; pay by
              CARD/UPI/WALLET/CASH; receive one ticket per seat; view bookings; cancel and see the refund preview
              before confirming; order food.</td>
              <td>screening, seat, booking, booked_seat, ticket, payment, cancellation, refund, concession_order</td></tr>
            <tr><td><b>Box office staff</b></td><td>Sell seats for a walk-in customer (staff_id set, CASH), look up any
              booking, cancel on behalf of a customer, take counter food sales, register customers.</td>
              <td>booking, payment, concession_order, order_item, customer</td></tr>
            <tr><td><b>Administrator</b></td><td>Schedule/reschedule/cancel shows, watch occupancy and revenue,
              run the 17 named reports, maintain movies, menu and customers, audit data integrity.</td>
              <td>screening, movie, concession_item, customer + all reporting views</td></tr>
          </tbody></table>
        </section>

        <section id="s3" class="card accent pale"><div class="eyebrow">3</div><h2>Entity relationship diagram</h2>
          <p>Hand-drawn by <span class="mono">make_er.py</span>: 17 entity boxes, every attribute with PK/FK/U markers,
            crow's-foot cardinalities, relationship verbs, and a check that no connector crosses a box.</p>
          <div style="overflow:auto;border:1px solid var(--line);border-radius:12px;background:#050A14;padding:8px">
            <img src="static/er.svg" alt="ER diagram" style="min-width:1100px;width:100%;height:auto">
          </div>
          <div class="row" style="margin-top:10px">
            <button class="btn small primary" data-act="show-er" type="button">Open in a modal</button>
            <span class="small muted">20 relationships · 92 attributes</span></div>
        </section>

        <section id="s4" class="card accent"><div class="eyebrow">4</div><h2>Relational schema (live)</h2>
          <div id="schemaLive"><div class="loading"><span class="spinner"></span> reading information_schema…</div></div>
        </section>

        <section id="s5" class="card accent blue"><div class="eyebrow">5</div><h2>Functional dependencies</h2>
          <p class="small muted">X → Y means every value of X determines exactly one value of Y. Every determinant on the
            left is a candidate key of its table, except the two documented exceptions.</p>
          <div id="fdBox"></div>
        </section>

        <section id="s6" class="card accent good"><div class="eyebrow">6</div><h2>Normalization 1NF → 3NF</h2>
          <ol>
            <li><b>1NF</b> — every attribute holds a single atomic value. A booking never stores "C4,C5,C6" in one
              column: the seats live in one <span class="mono">booked_seat</span> row each, and an order's items live in
              one <span class="mono">order_item</span> row each. Repeating groups are impossible.</li>
            <li><b>2NF</b> — no partial dependency on part of a composite key. The only composite keys are
              <span class="mono">order_item(order_id, item_id)</span>, where <span class="mono">quantity</span> depends on
              both, and <span class="mono">booking(booking_id, screening_id)</span> used as an FK target;
              <span class="mono">unit_price</span> and <span class="mono">item_name</span> are stored once in
              <span class="mono">concession_item</span>, so the classic partial dependency is removed by that table.</li>
            <li><b>3NF</b> — no transitive dependency: a non-key attribute never determines another non-key attribute.
              Movie details live only in <span class="mono">movie</span>, screen details only in
              <span class="mono">screen</span>, ticket price is a fact about one ticket only.</li>
          </ol>
          <table class="norm-table"><thead><tr><th>Redundancy that a naive design would create</th><th>How this schema removes it</th></tr></thead>
          <tbody>
            <tr><td>Movie title / language / duration repeated on every show</td><td>Stored once in <span class="mono">movie</span>; <span class="mono">screening</span> keeps only <span class="mono">movie_id</span></td></tr>
            <tr><td>Cinema name, city, address repeated on every show</td><td><span class="mono">screening → screen → cinema</span>, each piece of text stored once</td></tr>
            <tr><td>Seat row/number/type repeated per booking</td><td>Seat inventory is master data in <span class="mono">seat</span>; bookings point at <span class="mono">seat_id</span></td></tr>
            <tr><td>Item name and price copied into every order line</td><td><span class="mono">order_item.unit_price</span> is filled by trigger T5 from <span class="mono">concession_item</span>, keeping a price history while the master data stays single-copy</td></tr>
            <tr><td>Refund percentage recalculated (and possibly changed) per cancellation</td><td>The tiers live in <span class="mono">cancellation_policy</span>; <span class="mono">fn_refund_percent()</span> derives the tier from hours-before-show</td></tr>
            <tr><td>Customer name and e-mail duplicated on every booking</td><td>One <span class="mono">customer</span> row per person; <span class="mono">booking</span> holds <span class="mono">customer_id</span></td></tr>
          </tbody></table>
          <p class="small muted" style="margin-top:10px">Documented exceptions, kept on purpose: ①
            <span class="mono">screening.end_time</span> is stored (not derived) because an EXCLUDE constraint can only
            index real columns; ② <span class="mono">booked_seat.screening_id</span> repeats data already implied by the
            booking, so that <span class="mono">ux_active_seat</span> can be a declarative partial unique index —
            consistency is guaranteed by the composite foreign key to <span class="mono">booking(booking_id, screening_id)</span>.</p>
        </section>

        <section id="s7" class="card accent bad"><div class="eyebrow">7</div><h2>Business rule → enforcing mechanism</h2>
          <table class="norm-table"><thead><tr><th>Business rule</th><th>Enforced by</th><th>Kind</th></tr></thead><tbody>
            <tr><td>A seat can be sold at most once per show</td><td><span class="mono">ux_active_seat</span></td><td>partial unique index</td></tr>
            <tr><td>Two live shows cannot overlap on a screen (15 min cleaning gap)</td><td><span class="mono">ex_screening_no_overlap</span></td><td>EXCLUDE USING gist</td></tr>
            <tr><td>A booked seat must belong to the show's screen, the show must be SCHEDULED and not started</td><td>T1 <span class="mono">fn_validate_booked_seat</span></td><td>BEFORE INSERT/UPDATE trigger</td></tr>
            <tr><td>Only a CONFIRMED booking of a future show can be cancelled (never twice)</td><td>T2 <span class="mono">fn_validate_cancellation</span> + UNIQUE</td><td>trigger + unique constraint</td></tr>
            <tr><td>Cancelling frees seats, voids tickets and cancels the booking atomically</td><td>T3 <span class="mono">fn_apply_cancellation</span></td><td>AFTER INSERT trigger</td></tr>
            <tr><td>Refunds never exceed paid amount × policy percent</td><td>T4 <span class="mono">fn_validate_refund</span> + <span class="mono">fn_refund_percent()</span></td><td>trigger + SQL function</td></tr>
            <tr><td>Order lines take their price from the menu and reduce stock; stock cannot go negative</td><td>T5 <span class="mono">fn_order_item_stock</span> + CHECK</td><td>BEFORE INSERT trigger + CHECK</td></tr>
            <tr><td>Email format and phone format</td><td>CHECK <span class="mono">email ~ regex</span>, <span class="mono">phone ~ '^[0-9]{10,15}$'</span></td><td>CHECK constraints</td></tr>
            <tr><td>Allowed values (screen type, seat type, certificate, role, method, status…)</td><td>12 CHECK constraints</td><td>CHECK constraints</td></tr>
            <tr><td>No orphan rows anywhere</td><td>14 foreign keys, all ON DELETE RESTRICT except seat→screen CASCADE</td><td>FOREIGN KEY</td></tr>
            <tr><td>Nobody reads the data with a browser key</td><td>RLS enabled on all 17 tables, zero policies; only the backend owner role connects (TLS)</td><td>Row Level Security</td></tr>
            <tr><td>The Walk-in Guest cannot be deleted; referenced rows cannot be deleted</td><td>guarded endpoint + FK RESTRICT</td><td>application + foreign key</td></tr>
          </tbody></table>
        </section>

        <section id="s8" class="card accent"><div class="eyebrow">8</div><h2>SQL Lab</h2>
          <p class="small muted">Run any of the 17 published queries and read the exact SQL from the database.</p>
          <div id="labBox"><div class="loading"><span class="spinner"></span> loading queries…</div></div>
        </section>

        <section id="s9" class="card accent blue"><div class="eyebrow">9</div><h2>Triggers, views, functions, indexes (live)</h2>
          <div id="objectsBox"><div class="loading"><span class="spinner"></span> reading pg_catalog…</div></div>
        </section>

        <section id="s10" class="card accent good"><div class="eyebrow">10</div><h2>Testing summary</h2>
          <p>46 unittest cases run against a separate test schema, so the demo data is never touched. They cover the
            positive path and the negative path of every rule: overlap (including the exact 15-minute boundary),
            double booking with a full rollback, seat from another screen, started/cancelled shows, all three refund
            tiers at their boundaries, re-booking a released seat with a new ticket code, over-limit refund,
            over-stock order, e-mail uniqueness and format, FK protection, every named report, both integrity audits,
            and the new endpoints (table whitelist, SQL console refusals, source whitelist, SQL log).</p>
          <div class="row"><button class="btn primary" data-act="run-tests" type="button">Run the suite now</button>
            <span class="small muted">or <span class="mono">python tests/test_cinema.py</span> in a terminal</span></div>
          <div id="labTests" style="margin-top:12px">${state.tests ? renderTestResult(state.tests) : ''}</div>
        </section>
      </div>
    </div>`;

  $('#view').addEventListener('click', async (event) => {
    const button = event.target.closest('button[data-act]');
    if (!button) return;
    try {
      if (button.dataset.act === 'show-er') {
        openModal(modalShell('Entity Relationship diagram', '17 entities · 20 relationships', `
          <div style="overflow:auto;border:1px solid var(--line);border-radius:12px;background:#050A14;padding:8px">
            <img src="static/er.svg" alt="ER diagram" style="min-width:1100px;width:100%;height:auto"></div>`,
        `<button class="btn ghost" data-act="close-modal" type="button">Close</button>`), 'full');
      }
      if (button.dataset.act === 'lab-run') {
        const key = button.dataset.key;
        const box = $('#labResult');
        box.innerHTML = '<div class="loading"><span class="spinner"></span> running…</div>';
        const result = await api.get('/api/reports/' + key);
        box.innerHTML = `<div class="row between"><span class="chip good">${esc(result.count)} row(s)</span>
            <span class="chip cyan">${esc(result.ms)} ms</span></div>
          <div class="table-wrap" style="max-height:320px;margin-top:10px"><table class="data">
            <thead><tr>${result.columns.map((column) => `<th>${esc(column)}</th>`).join('')}</tr></thead>
            <tbody>${result.rows.map((row) => `<tr>${row.map((cell) => `<td>${esc(cell === null ? 'NULL' : cell)}</td>`).join('')}</tr>`).join('')}</tbody>
          </table></div>
          <details class="disclose" style="margin-top:12px"><summary>Show the SQL</summary>${codeBlock(result.sql, 'sql')}</details>`;
      }
      if (button.dataset.act === 'run-tests') {
        const box = $('#labTests');
        box.innerHTML = '<div class="loading"><span class="spinner"></span> running unittest…</div>';
        try {
          const result = await api.post('/api/tests/run', {});
          state.tests = result;
          box.innerHTML = renderTestResult(result);
          toast(`${result.passed} passed, ${result.failed} failed.`, result.failed ? 'error' : 'ok');
        } catch (error) { box.innerHTML = `<p class="bad small">${esc(error.message)}</p>`; }
      }
    } catch (error) { toast(error.message, 'error'); }
  });

  /* live schema, FDs, SQL lab and object SQL */
  api.get('/api/schema').then((schema) => {
    $('#schemaLive').innerHTML = `<p class="small muted">Read live from
      <span class="mono">information_schema</span> and <span class="mono">pg_catalog</span>:
      ${esc(schema.summary.tables)} tables, ${esc(schema.summary.rows.toLocaleString('en-IN'))} rows.</p>
      <details class="disclose" open><summary>17 relations</summary>
      ${schema.tables.map((table) => `<details class="disclose" style="background:transparent">
        <summary style="text-transform:none;font-family:'JetBrains Mono',monospace;letter-spacing:0;font-size:13px">
          ${esc(table.name)} <span class="chip">${esc(table.row_count)} rows</span>
          ${table.primary_key.length ? `<span class="chip good">PK ${esc(table.primary_key.join(','))}</span>` : ''}
          ${table.foreign_keys.length ? `<span class="chip cyan">${esc(table.foreign_keys.length)} FK</span>` : ''}</summary>
        <div class="table-wrap"><table class="data"><thead><tr><th>Column</th><th>Type</th><th>Key</th></tr></thead>
        <tbody>${table.columns.map((column) => `<tr><td class="mono">${esc(column.name)}</td>
          <td class="mono small">${esc(column.data_type)}</td>
          <td>${column.is_pk ? '<span class="chip good">PK</span>' : ''}
            ${column.is_fk ? `<span class="chip cyan">FK → ${esc(column.references)}</span>` : ''}
            ${column.is_unique && !column.is_pk ? '<span class="chip bad">U</span>' : ''}</td></tr>`).join('')}
        </tbody></table></div>
      </details>`).join('')}</details>`;

    const fds = {
      cinema: ['cinema_id → cinema_name, city, address, phone', '(cinema_name, city) → cinema_id'],
      screen: ['screen_id → cinema_id, screen_name, screen_type', '(cinema_id, screen_name) → screen_id'],
      seat: ['seat_id → screen_id, row_label, seat_no, seat_type', '(screen_id, row_label, seat_no) → seat_id'],
      movie: ['movie_id → title, genre, language, duration_min, certificate, release_date',
        '(title, language, release_date) → movie_id'],
      screening: ['screening_id → movie_id, screen_id, start_time, end_time, base_price, status',
        '(screen_id, start_time) → screening_id', 'movie_id → duration_min (via movie)'],
      customer: ['customer_id → full_name, email, phone, created_at', 'email → customer_id'],
      staff: ['staff_id → cinema_id, full_name, username, password_hash, role', 'username → staff_id'],
      concession_item: ['item_id → item_name, category, unit_price, stock_qty', 'item_name → item_id'],
      cancellation_policy: ['policy_id → min_hours_before, refund_percent', 'min_hours_before → refund_percent'],
      booking: ['booking_id → customer_id, screening_id, staff_id, booking_time, status',
        '(booking_id, screening_id) → booking_id'],
      booked_seat: ['booked_seat_id → booking_id, screening_id, seat_id, status',
        '(screening_id, seat_id) → booked_seat_id  [where status = BOOKED]',
        'booking_id → screening_id  (implied; enforced by the composite FK)'],
      ticket: ['ticket_id → booked_seat_id, ticket_code, price, issued_at, status',
        'booked_seat_id → ticket_id', 'ticket_code → ticket_id'],
      concession_order: ['order_id → booking_id, staff_id, order_time, status'],
      order_item: ['(order_id, item_id) → quantity, unit_price'],
      payment: ['payment_id → booking_id, order_id, amount, method, status, paid_at, transaction_ref',
        'transaction_ref → payment_id'],
      cancellation: ['cancellation_id → booking_id, cancelled_at, cancelled_by, reason',
        'booking_id → cancellation_id'],
      refund: ['refund_id → cancellation_id, payment_id, refund_amount, refund_status, refunded_at'],
    };
    $('#fdBox').innerHTML = `<div class="table-wrap"><table class="data"><thead><tr><th>Table</th>
      <th>Functional dependencies</th><th>Normal form</th></tr></thead><tbody>
      ${Object.entries(fds).map(([table, list]) => `<tr><td class="mono">${esc(table)}</td>
        <td>${list.map((fd) => `<div class="mono small">${esc(fd)}</div>`).join('')}</td>
        <td><span class="chip ${['screening', 'booked_seat'].includes(table) ? 'warn' : 'good'}">
          ${['screening', 'booked_seat'].includes(table) ? '3NF + documented exception' : '3NF'}</span></td></tr>`).join('')}
      </tbody></table></div>`;

    $('#labBox').innerHTML = `<div id="labResult"></div>
      <div class="pills" style="margin-top:10px">${schema.summary ? '' : ''}</div>`;
    api.get('/api/queries').then((catalogue) => {
      $('#labBox').innerHTML = `<div class="pills">${catalogue.queries.map((query) =>
        `<button class="pill" data-act="lab-run" data-key="${escAttr(query.key)}" type="button">
          ${esc(query.title)}<small>${esc(query.kind)}</small></button>`).join('')}</div>
        <div id="labResult" style="margin-top:12px"></div>`;
    });

    $('#objectsBox').innerHTML = [
      ['Triggers (' + schema.triggers.length + ')', schema.triggers.map((trigger) =>
        `<details class="disclose"><summary>${esc(trigger.name)} · ${esc(trigger.timing)} ${esc(trigger.event)} on ${esc(trigger.table_name)}</summary>
          ${codeBlock(trigger.function_sql, 'sql')}</details>`).join('')],
      ['Views (' + schema.views.length + ')', schema.views.map((view) =>
        `<details class="disclose"><summary>${esc(view.name)} · ${esc(view.row_count)} rows</summary>
          ${codeBlock(view.sql, 'sql')}</details>`).join('')],
      ['Functions (' + schema.functions.length + ')', schema.functions.map((object) =>
        `<details class="disclose"><summary>${esc(object.name)}(${esc(object.arguments)}) → ${esc(object.returns)}</summary>
          ${codeBlock(object.sql, 'sql')}</details>`).join('')],
      ['Indexes (' + schema.indexes.length + ')', schema.indexes.map((index) =>
        `<details class="disclose"><summary>${esc(index.name)} · ${esc(index.table_name)}</summary>
          ${codeBlock(index.definition + ';', 'sql')}</details>`).join('')],
    ].map(([title, body]) => `<details class="disclose" open><summary>${esc(title)}</summary>${body}</details>`).join('');
  }).catch((error) => {
    $('#schemaLive').innerHTML = `<p class="bad small">${esc(error.message)}</p>`;
  });
}

/* ------------------------------------------------------------- boot ------- */
function renderTabs() {
  const tabs = $('#tabs');
  tabs.innerHTML = ROLE_TABS[state.role].map(([key, label]) =>
    `<button data-tab="${escAttr(key)}" type="button">${esc(label)}</button>`).join('');
  tabs.querySelectorAll('button').forEach((button) => {
    button.addEventListener('click', () => { location.hash = '#/' + button.dataset.tab; });
  });
}

async function globalFormSave(collection, id, body) {
  try {
    const result = id
      ? await api.put(`/api/${collection}/${id}`, body)
      : await api.post(`/api/${collection}`, body);
    closeModal();
    toast(result.message, 'ok');
    render();
  } catch (error) { toast(error.message, 'error'); }
}

async function boot() {
  try {
    state.meta = await api.get('/api/meta');
  } catch (error) {
    $('#view').innerHTML = `<div class="card accent bad"><div class="eyebrow">Backend not reachable</div>
      <h2>${esc(error.message)}</h2>
      <p class="muted small">Start everything with <span class="mono">pip install -r requirements.txt &amp;&amp;
        python setup_db.py &amp;&amp; python app.py</span>, then reload this page.</p>
      <button class="btn" data-act="reload" type="button">Reload</button></div>`;
    return;
  }
  const meta = state.meta;
  $('#dbBadge').innerHTML = `<span class="dot"></span>Connected to <b>Supabase PostgreSQL</b> ·
    <b>${esc(meta.database.tables)}</b> tables · <b>${esc(meta.database.rows.toLocaleString('en-IN'))}</b> rows`;
  $('#clockBadge').innerHTML = `<span>fn_now() ${esc(fmtDT(meta.cinema_now))}${meta.fixed_clock ? ' · fixed' : ''}</span>`;
  $('#footInfo').textContent = `${esc(meta.database.server || 'PostgreSQL')} · schema ${esc(meta.database.schema)} · 17 tables · 5 triggers · 6 views · 8 indexes`;

  $('#customerPicker').innerHTML = meta.customers.map((customer) =>
    `<option value="${customer.customer_id}" ${customer.customer_id === state.customerId ? 'selected' : ''}>
      ${esc(customer.full_name)}</option>`).join('');

  $('#roleSwitch').addEventListener('change', (event) => {
    state.role = event.target.value;
    renderTabs();
    const allowed = ROLE_TABS[state.role].map(([key]) => key);
    location.hash = '#/' + allowed[0];
    render();
  });
  $('#customerPicker').addEventListener('change', (event) => {
    state.customerId = Number(event.target.value);
    toast(`Now browsing as ${(CUSTOMER() || {}).full_name}.`);
    render();
  });
  $('#logBtn').addEventListener('click', () => toggleLog());
  $('#logClose').addEventListener('click', () => toggleLog(false));
  $('#logPause').addEventListener('click', (event) => {
    state.log.paused = !state.log.paused;
    event.target.textContent = state.log.paused ? 'Resume' : 'Pause';
  });
  $('#logClear').addEventListener('click', async () => {
    await api.post('/api/log/clear', {});
    state.log.entries = []; state.log.since = 0; renderLog();
    toast('SQL log cleared.', 'ok');
  });

  renderTabs();
  if (!location.hash) location.hash = '#/' + ROLE_TABS[state.role][0][0];
  window.addEventListener('hashchange', render);
  render();

  document.addEventListener('click', (event) => {
    const button = event.target.closest('button[data-act]');
    if (!button) return;
    const action = button.dataset.act;
    if (action === 'close-modal') closeModal();
    if (action === 'reload') location.reload();
    if (action === 'back') history.back();
    if (action === 'goto-seats') { location.hash = '#/seats/' + button.dataset.id; }
    if (action === 'view-booking') openBookingModal(Number(button.dataset.id)).catch((error) => toast(error.message, 'error'));
    if (action === 'cancel-booking') openCancelModal(Number(button.dataset.id)).catch((error) => toast(error.message, 'error'));
    if (action === 'confirm-cancel') confirmCancel(Number(button.dataset.id));
    /* buttons that live inside a modal, so the page-level delegate cannot see them */
    if (action === 'save-movie') globalFormSave('movie', button.dataset.id, {
      title: $('#f-title').value, genre: $('#f-genre').value, language: $('#f-language').value,
      duration_min: Number($('#f-duration_min').value), certificate: $('#f-certificate').value,
      release_date: $('#f-release_date').value,
    });
    if (action === 'save-item') globalFormSave('items', button.dataset.id, {
      item_name: $('#f-item_name').value, category: $('#f-category').value,
      unit_price: Number($('#f-unit_price').value), stock_qty: Number($('#f-stock_qty').value),
    });
    if (action === 'save-customer') globalFormSave('customers', button.dataset.id, {
      full_name: $('#f-full_name').value, email: $('#f-email').value, phone: $('#f-phone').value,
    });
    if (action === 'do-reschedule') (async () => {
      try {
        const result = await api.put('/api/screenings/' + button.dataset.id, {
          start_time: $('#rStart').value.trim(), base_price: Number($('#rPrice').value),
        });
        closeModal(); toast(result.message, 'ok'); render();
      } catch (error) { toast(error.message, 'error'); }
    })();
  });
}

boot();
