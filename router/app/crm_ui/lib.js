// Shared pieces: DOM building, number and date formats, icons, the tooltip and the small charts.
// Text from the database always goes in as text nodes, never as HTML.

export function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value == null || value === false) continue;
    if (key === 'class') el.className = value;
    else if (key === 'style') Object.assign(el.style, value);
    else if (key.startsWith('on')) el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? '' : value);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

// --- formats (all times are shown in UTC, like the validity of a quote) -------------------

const eur0 = new Intl.NumberFormat('en-GB', { style: 'currency', currency: 'EUR', maximumFractionDigits: 0 });
const eur2 = new Intl.NumberFormat('en-GB', { style: 'currency', currency: 'EUR', minimumFractionDigits: 2 });
const int = new Intl.NumberFormat('en-GB', { maximumFractionDigits: 0 });
const one = new Intl.NumberFormat('en-GB', { maximumFractionDigits: 1 });
const two = new Intl.NumberFormat('en-GB', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const dayFmt = new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'short', timeZone: 'UTC' });
const timeFmt = new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit', timeZone: 'UTC' });
const clockFmt = new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit', timeZone: 'UTC' });

export const fmt = {
  eur: v => (v == null ? '–' : eur0.format(v)),
  eurExact: v => (v == null ? '–' : eur2.format(v)),
  int: v => (v == null ? '–' : int.format(v)),
  one: v => (v == null ? '–' : one.format(v)),
  two: v => (v == null ? '–' : two.format(v)),
  pct: v => (v == null ? '–' : `${one.format(v)}%`),
  kg: v => (v == null ? '–' : `${one.format(v)} kg`),
  cbm: v => (v == null ? '–' : `${two.format(v)} cbm`),
  km: v => (v == null ? '–' : `${int.format(v)} km`),
  days: v => (v == null ? '–' : `${one.format(v)} ${v === 1 ? 'day' : 'days'}`),
  day: v => (v ? dayFmt.format(new Date(v)) : '–'),
  time: v => (v ? timeFmt.format(new Date(v)) : '–'),
  clock: v => clockFmt.format(v),
  when: v => (v ? `${dayFmt.format(new Date(v))}, ${timeFmt.format(new Date(v))}` : '–'),
  duration: s => (s == null ? '–' : s < 90 ? `${int.format(s)} s` : `${one.format(s / 60)} min`),
  figure(value, unit, n) {
    if (unit === 'count') return n == null ? fmt.int(value) : `${fmt.int(value)} of ${fmt.int(n)}`;
    if (unit === 'pct') return fmt.pct(value);
    if (unit === 'eur') return fmt.eur(value);
    if (unit === 'min') return `${one.format(value)} min`;
    return fmt.int(value);
  },
};

export const words = text => (text ?? '').replaceAll('_', ' ').replace(/^./, c => c.toUpperCase());

// --- icons (fixed markup, 16 px, stroked) -------------------------------------------------

const ICONS = {
  now: '<circle cx="8" cy="8" r="6"/><path d="M8 4.8V8l2.2 1.4"/>',
  rounds: '<path d="M2.5 4h11M2.5 8h11M2.5 12h7"/>',
  performance: '<path d="M2.5 13.5h11M4.5 11V7.5M8 11V3.5M11.5 11V6"/>',
  lookup: '<path d="M2.5 3h11v10h-11zM2.5 6.5h11M6.5 6.5V13"/>',
  system: '<rect x="2.5" y="3" width="11" height="7.5" rx="1.2"/><path d="M6 13.5h4M8 10.5v3"/>',
  bell: '<path d="M4 11V7.5a4 4 0 0 1 8 0V11l1.2 1.5H2.8zM6.5 14h3"/>',
  'bell-off': '<path d="M4 11V7.5a4 4 0 0 1 6.5-3.1M12 8.5V11l1.2 1.5H5.5M6.5 14h3M2.5 2.5l11 11"/>',
  search: '<circle cx="7" cy="7" r="4.2"/><path d="m10.200 10.2 3.3 3.3"/>',
  check: '<path d="m3 8.500 3.200 3.2L13 4.800"/>',
  cross: '<path d="m4 4 8 8M12 4l-8 8"/>',
  clock: '<circle cx="8" cy="8" r="6"/><path d="M8 4.800V8l2.200 1.400"/>',
  alert: '<path d="M8 2.200 14.200 13H1.800z"/><path d="M8 6.500v3M8 11.300v.2"/>',
  info: '<circle cx="8" cy="8" r="6"/><path d="M8 7.200v3.600M8 5.100v.2"/>',
  dot: '<circle cx="8" cy="8" r="3.200" fill="currentColor" stroke="none"/>',
  spin: '<path d="M13.500 8A5.500 5.500 0 1 1 8 2.500"/><path d="M8 .800 10 2.500 8 4.200"/>',
  sun: '<circle cx="8" cy="8" r="3"/><path d="M8 1.500v1.700M8 12.800v1.700M1.500 8h1.700M12.800 8h1.700M3.400 3.400l1.200 1.200M11.400 11.400l1.200 1.200M3.400 12.600l1.200-1.200M11.400 4.600l1.200-1.200"/>',
  moon: '<path d="M13.500 9.600A5.800 5.800 0 0 1 6.400 2.500a5.800 5.800 0 1 0 7.100 7.100z"/>',
  back: '<path d="M9.500 3.500 5 8l4.500 4.500"/>',
  plane: '<path d="M14 2 9.200 14l-2-5.200L2 6.800z"/>',
  truck: '<path d="M1.500 4h7.500v6.500H1.500zM9 6.500h2.800l2.200 2.200v1.800H9"/><circle cx="4.500" cy="11.500" r="1.300"/><circle cx="11.500" cy="11.500" r="1.300"/>',
  download: '<path d="M8 2.500v7.500M5 7.200 8 10.200l3-3M3 13h10"/>',
};

export function icon(name) {
  const span = document.createElement('span');
  span.className = 'icon';
  span.setAttribute('aria-hidden', 'true');
  span.innerHTML = `<svg viewBox="0 0 16 16">${ICONS[name] || ICONS.dot}</svg>`;
  return span;
}

// --- shipment states ----------------------------------------------------------------------

export const STATES = {
  NEW: ['New', 'neutral', 'dot'],
  AWAITING_INFO: ['Awaiting information', 'warning', 'clock'],
  PRICING: ['Pricing', 'accent', 'spin'],
  QUOTED: ['Quoted', 'accent', 'dot'],
  BOOKING: ['Booking in progress', 'warning', 'spin'],
  BOOKED: ['Booked', 'good', 'check'],
  CONFIRMED: ['Confirmed', 'good', 'check'],
  REJECTED: ['Declined', 'neutral', 'cross'],
  FAILED: ['Failed', 'critical', 'alert'],
};

export function stateBadge(deal) {
  const [label, tone, glyph] = STATES[deal.state] || [deal.state, 'neutral', 'dot'];
  const text = deal.dry_run_booking && deal.state === 'BOOKED' ? 'Booked (dry run)' : label;
  return h('span', { class: `state ${tone}` }, icon(glyph), text);
}

export const CATEGORIES = {
  rfq: 'Quote request', customer_info: 'Customer information', customer_accept: 'Acceptance',
  customer_reject: 'Decline', customer_question: 'Customer enquiry', trucker_quote: 'Trucker offer',
  trucker_decline: 'Trucker decline', booking_confirmation: 'Booking confirmation', rate_offer: 'Rate offer',
  tariff_update: 'Tariff update', tracking_request: 'Tracking enquiry', other: 'Other',
};
export const category = key => CATEGORIES[key] || `${key} (legacy label)`;
export const KINDS = {
  quote: 'Quote', clarification: 'Clarification request', booking_note: 'Booking confirmation', answer: 'Reply',
  tracking_answer: 'Tracking reply', trucker_request: 'Trucking request', booking_request: 'Booking request',
};
export const ROLES = { customer: 'Customer', trucker: 'Trucker', airline: 'Airline', ratedesk: 'Rate desk', tracking: 'Tracking desk' };

// --- tooltip: one element, shown on hover and on keyboard focus -----------------------------

const tipEl = () => document.getElementById('tip');

export function showTip(content, x, y) {
  const el = tipEl();
  el.replaceChildren(...[content].flat());
  el.classList.add('on');
  const box = el.getBoundingClientRect();
  el.style.left = `${Math.max(8, Math.min(x + 14, innerWidth - box.width - 8))}px`;
  el.style.top = `${Math.max(8, y + 16 + box.height > innerHeight ? y - box.height - 10 : y + 16)}px`;
}
export const hideTip = () => tipEl().classList.remove('on');

export function tip(el, build) {
  el.addEventListener('pointermove', e => showTip(build(), e.clientX, e.clientY));
  el.addEventListener('pointerleave', hideTip);
  el.addEventListener('focus', () => { const r = el.getBoundingClientRect(); showTip(build(), r.left, r.bottom - 12); });
  el.addEventListener('blur', hideTip);
  return el;
}
export const tipRows = (title, rows) => [h('b', null, title), ...rows.filter(Boolean).map(([k, v]) => h('div', { class: 'rowt' }, h('span', null, k), h('span', null, v)))];

// --- small charts ---------------------------------------------------------------------------

// Ranked horizontal bars on one baseline. rows: {name, value, text?, note?, em?, tip?, href?}
export function barList(rows, { label = 150, solid = false, max } = {}) {
  const top = max ?? Math.max(...rows.map(r => r.value || 0), 0);
  return h('div', { class: `bars${solid ? ' solid' : ''}`, style: { '--label': `${label}px` } }, rows.map(r => {
    const row = h('div', { class: `row${r.em ? ' em' : ''}`, tabindex: r.tip ? 0 : null, role: r.href ? 'link' : null,
      onclick: r.href ? () => { location.hash = r.href; } : null },
      h('span', { class: 'name', title: r.name }, r.name),
      h('div', { class: 'track' }, r.value > 0 && h('div', { class: 'fill', style: { width: `${100 * r.value / top}%` } })),
      h('span', { class: 'val' }, r.text ?? fmt.int(r.value), r.note && h('small', null, r.note)));
    return r.tip ? tip(row, r.tip) : row;
  }));
}

// One whole split in parts, thin, without text: the figures stand beside it.
export function splitBar(parts) {
  const total = parts.reduce((a, p) => a + (p.value || 0), 0);
  const bar = h('div', { class: 'split', tabindex: 0, role: 'img',
    'aria-label': parts.map(p => `${p.name} ${p.text}`).join(', ') },
    parts.filter(p => p.value > 0).map(p => h('i', { class: p.cls, style: { flex: `${p.value} 0 0` } })));
  return tip(bar, () => tipRows('All-in price', parts.map(p => [p.name, `${p.text} · ${fmt.pct(total ? 100 * p.value / total : 0)}`])));
}

export function meter(pctValue, text) {
  return h('span', { class: 'meter' }, h('span', { class: 'track' }, h('i', { style: { width: `${Math.max(0, Math.min(100, pctValue || 0))}%` } })), h('span', { class: 'num' }, text));
}

// Columns over time, one series. buckets: {at, count}
export function columns(buckets, { unit = 'mails' } = {}) {
  const W = 640, H = 150, pad = { l: 26, r: 6, t: 8, b: 20 };
  const top = Math.max(1, ...buckets.map(b => b.count));
  const step = (W - pad.l - pad.r) / Math.max(1, buckets.length);
  const width = Math.max(2, Math.min(24, step - 2));
  const y = v => pad.t + (H - pad.t - pad.b) * (1 - v / top);
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', `${unit} per 10 minutes`);
  const add = (name, attrs, text) => {
    const el = document.createElementNS('http://www.w3.org/2000/svg', name);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
    if (text != null) el.textContent = text;
    svg.append(el);
    return el;
  };
  for (const tick of [0, top]) {
    add('line', { x1: pad.l, x2: W - pad.r, y1: y(tick), y2: y(tick) });
    add('text', { x: pad.l - 6, y: y(tick) + 4, 'text-anchor': 'end' }, tick);
  }
  buckets.forEach((b, i) => {
    const x = pad.l + i * step + (step - width) / 2;
    const hit = add('rect', { class: 'hit', x: pad.l + i * step, y: pad.t, width: step, height: H - pad.t - pad.b, tabindex: 0 });
    const top4 = Math.min(4, (H - pad.b - y(b.count)) / 2);
    // rounded at the data end, square at the baseline
    const bar = add('path', { class: 'col', d: b.count ? `M${x},${H - pad.b}V${y(b.count) + top4}q0,${-top4} ${top4},${-top4}h${width - 2 * top4}q${top4},0 ${top4},${top4}V${H - pad.b}z` : '' });
    bar.setAttribute('fill', 'var(--series-1)');
    tip(hit, () => tipRows(`${fmt.time(b.at)} UTC`, [[unit, fmt.int(b.count)], ['Date', fmt.day(b.at)]]));
    hit.addEventListener('pointerenter', () => bar.setAttribute('opacity', '0.72'));
    hit.addEventListener('pointerleave', () => bar.removeAttribute('opacity'));
    const every = Math.ceil(buckets.length / 7);
    if (i % every === 0) add('text', { x: pad.l + i * step, y: H - 5 }, fmt.time(b.at));
  });
  return h('div', { class: 'colchart' }, svg);
}

// --- tables -----------------------------------------------------------------------------------

// columns: {key, label, right?, get?, render?, sort?}. `state` keeps the sort across re-renders.
export function table(rows, cols, { state, onRow, empty = 'Nothing to show.' } = {}) {
  if (!rows.length) return h('div', { class: 'empty' }, empty);
  const sortCol = cols.find(c => c.key === state?.key);
  const value = (row, col) => (col.get ? col.get(row) : row[col.key]);
  const sorted = sortCol ? [...rows].sort((a, b) => {
    const [x, y] = [value(a, sortCol), value(b, sortCol)];
    if (x == null || x === '') return 1;
    if (y == null || y === '') return -1;
    const cmp = typeof x === 'number' && typeof y === 'number' ? x - y : String(x).localeCompare(String(y));
    return state.dir === 'asc' ? cmp : -cmp;
  }) : rows;
  const head = h('tr', null, cols.map(col => h('th', {
    class: `${col.right ? 'r ' : ''}${col.sort === false ? '' : 'sortable'}`,
    onclick: col.sort === false || !state ? null : () => {
      state.dir = state.key === col.key && state.dir === 'desc' ? 'asc' : 'desc';
      state.key = col.key;
      state.changed();
    },
  }, col.label, state?.key === col.key && h('span', { class: 'arrow' }, state.dir === 'asc' ? '↑' : '↓'))));
  const body = sorted.map(row => h('tr', { class: onRow ? 'link' : null, onclick: onRow ? () => onRow(row) : null },
    cols.map(col => h('td', { class: `${col.right ? 'r' : ''} ${col.cls || ''}` }, col.render ? col.render(row) : (value(row, col) ?? '–')))));
  return h('div', { class: 'tablewrap' }, h('table', null, h('thead', null, head), h('tbody', null, body)));
}

// Download the rows of a table as CSV. A column is exported by its `csv` function, else by
// the value it sorts on; `csv: false` leaves it out.
export function exportCsv(filename, rows, cols) {
  const cell = value => {
    const text = value == null ? '' : Array.isArray(value) ? value.join(' ') : String(value);
    return /[",\n\r]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
  };
  const used = cols.filter(col => col.csv !== false);
  const lines = [used.map(col => cell(col.label)), ...rows.map(row => used.map(col => cell(col.csv ? col.csv(row) : col.get ? col.get(row) : row[col.key])))];
  const link = document.createElement('a');
  link.href = URL.createObjectURL(new Blob(['\ufeff' + lines.map(line => line.join(',')).join('\r\n')], { type: 'text/csv;charset=utf-8' }));
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}
export const exportButton = (filename, rows, cols) =>
  h('button', { class: 'pill small', title: 'Download this table as CSV', onclick: () => exportCsv(filename, rows, cols) }, icon('download'), 'CSV');

export const card = (span, title, note, ...body) =>
  h('section', { class: `card glass ${span}` }, title && h('header', null, h('h3', null, title), note && h('span', { class: 'note' }, note)), body);
