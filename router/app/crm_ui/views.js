// The pages. Each takes the context (snapshot, UI state, helpers) and returns {title, sub, node}.
// Every page only shows; a line or a row can open another page, nothing else (ADR 58).

import { createGlobe, REGIONS, routeView } from './globe.js';
import { barList, card, CATEGORIES, exportButton, fmt, h, icon, meter, needsYou, PIPELINE, ROLES, STATUS, statusBadge, table } from './lib.js';

const go = hash => { location.hash = hash; };
const has = (q, ...fields) => !q || fields.some(f => String(f ?? '').toLowerCase().includes(q));
const head = (title, note, right) => h('header', null, h('h3', null, title), note && h('span', { class: 'note' }, note), right && h('span', { class: 'right' }, right));
const fact = (label, value) => h('div', null, h('span', null, label), h('b', null, value ?? '–'));
const tile = (label, value, foot, onclick) => h(onclick ? 'button' : 'div', { class: `tile glass${onclick ? ' link' : ''}`, onclick },
  h('div', { class: 'label' }, label), h('div', { class: 'value' }, value), foot && h('div', { class: 'foot' }, foot));
const sortState = (ctx, name, key, dir = 'desc') => {
  const sort = (ctx.ui.sort[name] ||= { key, dir });
  sort.changed = ctx.rerender;
  return sort;
};
const lane = s => s.lane || '–';
const ticks = list => h('span', { class: 'ticks' }, list.map(t => h('i', {
  class: `tick ${t.tone}${t.href ? ' link' : ''}`, title: t.tip || null, onclick: t.href ? () => go(t.href) : null })));

// The globe: one line per route, coloured by how its requests ended. Click a route for its shipments.
function globeCard(ctx, span, { height = 400, title = 'Routes', lanes = ctx.snap.globe.lanes, compact = false } = {}) {
  const host = h('div'), g = ctx.snap.globe, ui = ctx.ui;
  const globe = createGlobe(host, {
    airports: g.airports, lanes, airlines: g.airlines, height, compact, view: compact ? ctx.focusView : ui.globe, coverage: ui.coverage,
    onLane: lane => { ui.status = ''; ui.nextFilter = lane.origin ? `${lane.origin} → ${lane.destination}` : lane.destination; go('#/shipments'); },
  });
  ctx.globes.push(globe);
  const routed = lanes.filter(l => l.origin);
  const controls = compact ? null : [
    h('div', { class: 'seg', role: 'group', 'aria-label': 'Turn the globe to' }, Object.entries(REGIONS).map(([name, [lon, lat]]) => h('button', { onclick: () => globe.lookAt(lon, lat) }, name))),
    h('label', { class: 'chip', style: { cursor: 'pointer' } },
      h('input', { type: 'checkbox', checked: ui.coverage || null, onchange: e => { ui.coverage = e.target.checked; globe.setCoverage(ui.coverage); } }), 'All served destinations')];
  return h('section', { class: `card glass ${span}` },
    h('header', null, h('h3', null, title), !compact && h('span', { class: 'note' }, `${routed.length} routes`), controls && h('span', { class: 'right' }, controls)),
    host,
    compact ? null : h('div', { class: 'legend', style: { marginTop: '10px' } },
      h('span', null, h('i', { class: 'line' }), 'Booked'), h('span', null, h('i', { class: 'line dash' }), 'Quoted or open'),
      h('span', null, h('i', { class: 'line off' }), 'Lost'), h('span', null, h('i', { class: 'ring' }), 'Departure airport'),
      h('span', { class: 'muted' }, 'Drag to turn. Line width: number of requests. Click a route for its shipments.')));
}

const customerLink = (company, email) => h('a', { href: `#/customer/${encodeURIComponent(email)}`, onclick: e => e.stopPropagation() }, company);

// "13 h", "5 min", "2 d": how long ago, the way a person says it
export function ago(iso) {
  if (!iso) return '–';
  const min = Math.max(0, (Date.now() - new Date(iso)) / 60000);
  return min < 1 ? 'just now' : min < 90 ? `${Math.round(min)} min` : min < 48 * 60 ? `${Math.round(min / 60)} h` : `${Math.round(min / 1440)} d`;
}
// The customer's reason: the first line that says something, not "Hi there," or "Dear Alex,"
const saying = text => (text || '').split('\n').map(line => line.trim()).find(line => line.split(/\s+/).length > 4) || (text || '').trim();
const minutes = m => (m == null ? '–' : m < 90 ? `${fmt.one(m)} min` : `${fmt.one(m / 60)} h`);

// A filter box that belongs to the page (the header search looks through everything).
function filterBox(ctx, placeholder) {
  const input = h('input', { type: 'search', class: 'pagefilter', placeholder, value: ctx.ui.filter, 'aria-label': placeholder });
  let typing;
  input.addEventListener('input', () => { clearTimeout(typing); typing = setTimeout(() => { ctx.ui.filter = input.value; ctx.rerender().then(() => document.querySelector('.pagefilter')?.focus()); }, 200); });
  return input;
}

// --- Today: what needs a person, what waits for the customer, what the desk is doing -----------

function shipmentItem(s, level, detail, meta) {
  return h('div', { class: `item ${level} link`, onclick: () => go(`#/shipment/${s.id}`) },
    icon(level === 'critical' ? 'alert' : s.running ? 'spin' : 'clock'),
    h('div', null, h('b', null, `${s.company} · ${lane(s)}`), h('p', null, detail)),
    h('div', { class: 'meta' }, statusBadge(s), h('div', null, meta)));
}

export async function today(ctx) {
  const t = ctx.snap.today, k = t.kpis, all = ctx.snap.shipments;
  const open = all.filter(s => ['new', 'needs_info', 'quoted'].includes(s.status)).length;
  const show = status => () => { ctx.ui.status = status; ctx.ui.filter = ''; go('#/shipments'); };
  const node = h('div', { class: 'grid' },
    t.desk_note && h('div', { class: 'notice glass c12' }, icon('alert'), h('div', null, h('b', null, 'The desk asks for a person: '), t.desk_note.text)),
    h('div', { class: 'health glass c12' }, ctx.snap.strips.map(st => h('span', { class: 'hstrip' }, h('b', null, st.name), ticks(st.ticks), h('small', null, st.text))),
      h('span', { class: 'hlegend' }, [['ok', 'booked'], ['open', 'open'], ['warn', 'waiting for info'], ['bad', 'needs you'], ['lost', 'lost or cancelled']]
        .map(([tone, label]) => h('span', null, h('i', { class: `tick ${tone}` }), label)))),
    h('div', { class: 'tiles' },
      tile('New requests', fmt.int(k.new), 'today', show('')),
      tile('Quoted', fmt.int(k.quoted), 'today', show('quoted')),
      tile('Booked', fmt.int(k.booked), 'today', show('booked')),
      tile('Lost', fmt.int(k.lost), 'today', show('lost')),
      tile('Open now', fmt.int(open), 'new, needs info or quoted', show('open'))),
    h('section', { class: 'card glass c6 flush' }, head('Needs you', t.needs_you.length ? `${t.needs_you.length} ${t.needs_you.length === 1 ? 'shipment' : 'shipments'} where the desk got stuck` : null),
      t.needs_you.length
        ? h('div', { class: 'list' }, t.needs_you.map(s => shipmentItem(s, 'critical', s.needs_you.join(' · '), ago(s.last_at))))
        : h('div', { class: 'empty' }, 'Nothing needs you. The desk handles every shipment on its own.')),
    globeCard(ctx, 'c6'),
    h('section', { class: 'card glass c7 flush' }, head('Waiting for the customer', `${t.waiting.length} open`),
      t.waiting.length
        ? h('div', { class: 'list' }, t.waiting.map(s => shipmentItem(s, 'info',
          s.status === 'quoted' ? `Quote of ${fmt.eurExact(s.price_eur)} sent, no answer yet` : 'We asked for information and wait for it',
          `waiting ${ago(s.waiting_since || s.last_at)}`)))
        : h('div', { class: 'empty' }, 'No customer owes us an answer.')),
    h('section', { class: 'card glass c5 flush' }, head('Desk working now'),
      t.running.length
        ? h('div', { class: 'list' }, t.running.map(s => shipmentItem(s, 'info', 'The desk is reading mail, pricing or writing', `since ${ago(s.last_at)}`)))
        : h('div', { class: 'empty' }, ctx.snap.desk.state === 'quiet' ? ctx.snap.desk.label : 'Not working on anything right now.')));
  return { title: 'Today', sub: fmt.day(t.date), node };
}

// --- Shipments: the pipeline, as a table or as a board -----------------------------------------

const STATUS_FILTERS = [['', 'All'], ['open', 'Open'], ['needs_you', 'Needs you'], ...Object.entries(STATUS).map(([k, v]) => [k, v[0]])];
const statusMatch = (s, f) => !f || (f === 'open' ? ['new', 'needs_info', 'quoted'].includes(s.status) : f === 'needs_you' ? s.needs_you.length > 0 : s.status === f);

const shipmentCols = (withCustomer = true) => [
  { key: 'id', label: 'Shipment', render: s => h('div', null, h('b', null, s.id), h('div', { class: 'sub' }, fmt.when(s.received_at))) },
  { key: 'status', label: 'Status', get: s => PIPELINE.indexOf(['lost', 'cancelled'].includes(s.status) ? 'closed' : s.status), render: s => h('div', { class: 'stack2' }, statusBadge(s), needsYou(s)), csv: s => STATUS[s.status]?.[0] },
  withCustomer && { key: 'company', label: 'Customer', render: s => customerLink(s.company, s.customer) },
  { key: 'lane', label: 'Lane', get: s => lane(s) },
  { key: 'price_eur', label: 'Price all-in', right: true, render: s => fmt.eurExact(s.price_eur) },
  { key: 'booking', label: 'Booking', render: s => (s.booking ? h('div', null, s.booking, h('div', { class: 'sub' }, s.airline)) : '–'), csv: s => [s.booking, s.airline].filter(Boolean).join(' ') },
  { key: 'last_at', label: 'Last activity', right: true, render: s => h('span', { title: fmt.when(s.last_at) }, ago(s.last_at)) },
].filter(Boolean);

function board(rows) {
  const columns = { new: [], needs_info: [], quoted: [], booked: [], closed: [] };
  rows.forEach(s => columns[['lost', 'cancelled'].includes(s.status) ? 'closed' : s.status]?.push(s));
  const title = { new: 'New', needs_info: 'Needs info', quoted: 'Quoted', booked: 'Booked', closed: 'Closed' };
  return h('div', { class: 'kanban c12' }, PIPELINE.map(key => h('section', { class: 'kcol glass' },
    h('header', null, h('h3', null, title[key]), h('span', { class: 'note' }, columns[key].length)),
    h('div', { class: 'kcards' }, columns[key].map(s => h('button', { class: `kcard${s.needs_you.length ? ' hot' : ''}`, onclick: () => go(`#/shipment/${s.id}`) },
      h('b', null, s.company), h('span', null, `${lane(s)} · ${s.id}`),
      h('span', { class: 'krow' }, s.price_eur != null ? fmt.eur(s.price_eur) : '', h('small', null, ago(s.last_at))),
      key === 'closed' ? statusBadge(s) : null, needsYou(s)))))));
}

export async function shipments(ctx, view) {
  if (view === 'board' || view === 'table') ctx.ui.view = view;  // a link can open either view: #/shipments/board
  const q = ctx.ui.filter.trim().toLowerCase(), all = ctx.snap.shipments;
  const rows = all.filter(s => statusMatch(s, ctx.ui.status) && has(q, s.id, s.company, s.customer, s.booking, s.lane, s.airline, s.subject));
  const count = f => all.filter(s => statusMatch(s, f)).length;
  const toolbar = h('div', { class: 'toolbar c12' },
    h('div', { class: 'seg', role: 'group', 'aria-label': 'Status' }, STATUS_FILTERS.map(([key, label]) => h('button', {
      class: ctx.ui.status === key ? 'on' : '', onclick: () => { ctx.ui.status = key; ctx.rerender(); } }, label, h('b', null, count(key))))),
    filterBox(ctx, 'Filter: customer, lane, BK number …'),
    h('div', { class: 'seg', role: 'group', 'aria-label': 'View' },
      h('button', { class: ctx.ui.view === 'table' ? 'on' : '', onclick: () => ctx.setView('table'), title: 'Table' }, icon('table'), 'Table'),
      h('button', { class: ctx.ui.view === 'board' ? 'on' : '', onclick: () => ctx.setView('board'), title: 'Board by status' }, icon('board'), 'Board')));
  const cols = shipmentCols();
  const body = ctx.ui.view === 'board'
    ? board(rows)
    : h('section', { class: 'card glass c12 flush' }, head('Shipments', `${rows.length} of ${all.length}`, exportButton('talos-shipments.csv', rows, cols)),
        table(rows, cols, { state: sortState(ctx, 'shipments', 'last_at'), onRow: s => go(`#/shipment/${s.id}`), empty: 'No shipment matches.' }));
  return { title: 'Shipments', sub: `${all.length} customer requests handled by the desk`, node: h('div', { class: 'grid' }, toolbar, body) };
}

// --- one shipment: header, what happened (live), and what the desk noted ------------------------

// A mail as a person reads it: paragraphs, the greeting and the signature quieter, quoted lines set off.
const CLOSING = /^(best|kind|many|warm|viele|beste|freundliche|mit freundlichen|cheers|regards|thanks|thank you|danke)\b/i;
function mailText(text) {
  const paras = (text || '').replace(/\r/g, '').split(/\n\s*\n/).map(p => p.trim()).filter(Boolean);
  let signed = false;
  return h('div', { class: 'mailtext' }, paras.map((p, i) => {
    if (CLOSING.test(p) && i > 0) signed = true;
    const quoted = p.split('\n').every(line => line.startsWith('>'));
    return h('p', { class: signed ? 'sig' : quoted ? 'quote' : i === 0 && p.length < 40 ? 'greet' : null }, p);
  }));
}
// The first lines that say something, for the closed view of a mail
const preview = text => {
  const lines = (text || '').split('\n').map(l => l.trim()).filter(l => l && !/^(hi|hello|dear|hallo|guten tag)\b.{0,30}$/i.test(l) && !l.startsWith('>'));
  const joined = lines.join(' ');
  return joined.length > 160 ? `${joined.slice(0, 160).replace(/\s\S*$/, '')} …` : joined;
};

// Money and booking references stand out in the desk's notes
function marked(text) {
  return text.split(/((?:EUR|€)\s?[\d,]+(?:\.\d+)?|[\d,]+(?:\.\d+)?\s?EUR\b|BK-[A-Z0-9]+)/).map((part, i) => (i % 2 ? h('b', null, part) : part));
}
// notes.md as a list: the time of each line on the left, its keyword (QUOTE SENT, BOOKED) as a tag
const STAMP = /^(\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?Z?)?(?:\s?UTC)?)[\s:]*/;
const KEYWORD = /^([A-Z][A-Z\- ]{2,}[A-Z])\b[\s:]*/;
function notesView(text) {
  const lines = text.split('\n');
  if (lines[0]?.startsWith('#')) lines.shift();
  const items = [];
  for (const raw of lines) {
    const line = raw.trim();
    if (!line) continue;
    if (/^[-*]\s/.test(line) || !items.length) items.push(line.replace(/^[-*]\s+/, ''));
    else items[items.length - 1] += ` ${line}`;
  }
  return h('div', { class: 'notesview' }, items.map(item => {  // the title line repeats the page's own header
    const stamp = item.match(STAMP);
    let rest = stamp ? item.slice(stamp[0].length) : item;
    const key = rest.match(KEYWORD);
    if (key) rest = rest.slice(key[0].length);
    const label = rest.match(/^([a-z][a-z \-]{2,30}):\s/i);  // "request: …", "air options: …"
    if (!key && label) rest = rest.slice(label[0].length);
    return h('div', { class: 'nitem' },
      h('span', { class: 'ntime' }, stamp ? (stamp[1].match(/\d{2}:\d{2}/)?.[0] || fmt.day(stamp[1])) : ''),
      h('div', null, key ? h('span', { class: 'tag on' }, key[1]) : label ? h('span', { class: 'tag' }, label[1]) : null, ' ', marked(rest)));
  }));
}

const TYPES = { mail_in: ['mail', 'Mail in'], mail_out: ['mail', 'Mail out'], desk: ['note', 'Desk'], note: ['note', 'Desk note'], booking: ['check', 'Booking'], cancel: ['cross', 'Cancellation'] };

function timelineItem(ctx, sid, item, index) {
  const key = `${sid}:${index}`, [glyph] = TYPES[item.type] || ['dot'];
  const who = item.type === 'mail_in' ? [ROLES[item.role] || item.role, CATEGORIES[item.category] || item.category].filter(Boolean).join(' · ') : null;
  const fold = (k, summary, text, extra) => h('details', { class: 'mailbody', open: ctx.ui.open.has(k) || null,
    ontoggle: e => (e.target.open ? ctx.ui.open.add(k) : ctx.ui.open.delete(k)) },
    h('summary', null, summary), mailText(text), extra);
  const body = item.parts && item.type === 'mail_out'
    ? h('div', null, h('div', { class: 'msubject' }, item.subject), h('div', { class: 'codes', style: { margin: '6px 0' } }, item.parts.map(p => h('span', { class: 'tag' }, p.who))),
        fold(`${key}:0`, h('span', { class: 'muted' }, `The same request to all ${item.parts.length}: ${preview(item.parts[0].body)}`), item.parts[0].body))
    : item.parts
    ? h('div', null, h('div', { class: 'msubject' }, item.subject), item.parts.map((p, i) => fold(`${key}:${i}`, h('span', null, h('b', null, p.who), ` · ${fmt.time(p.at)} · `, h('span', { class: 'muted' }, marked(preview(p.body)))), p.body)))
    : item.body ? [h('div', { class: 'msubject' }, item.subject || '(no subject)'),
      fold(key, h('span', { class: 'muted' }, marked(preview(item.body) || 'Show the mail')), item.body, item.attachments?.length ? h('div', { class: 'sub' }, `Attachments: ${item.attachments.join(', ')}`) : null)] : null;
  return h('div', { class: `tl ${item.type}` }, h('div', { class: 'tlicon' }, icon(glyph)),
    h('div', null, h('div', { class: 'tlhead' }, h('b', null, item.title), who && h('span', { class: 'sub' }, who), h('span', { class: 'sub right' }, fmt.when(item.at))),
      item.text && !item.parts && h('p', null, item.text), body));
}

export async function shipment(ctx, id) {
  const s = await ctx.api(`/crm/api/shipments/${encodeURIComponent(id)}`);
  const own = ctx.snap.globe.lanes.filter(l => l.origin === s.origin && l.destination === s.destination && s.origin);
  const [a, b] = [ctx.snap.globe.airports[s.origin], ctx.snap.globe.airports[s.destination]];
  ctx.focusView = a && b ? routeView(a, b) : null;
  const route = own.length && ctx.focusView ? globeCard(ctx, '', { height: 240, title: lane(s), lanes: own, compact: true }) : null;
  const node = h('div', { class: 'grid' },
    h('div', { class: 'title c12' }, h('a', { class: 'pill small', href: '#/shipments' }, icon('back'), 'Shipments'),
      h('h2', null, `${s.id} · `, customerLink(s.company, s.customer)), statusBadge(s), h('span', { class: 'muted' }, lane(s))),
    s.needs_you.length ? h('div', { class: 'notice glass c12 critical' }, icon('alert'), h('div', null, h('b', null, 'Needs you: '), s.needs_you.join(' · '))) : null,
    card('c12', null, null, h('div', { class: 'facts' },
      fact('Customer', s.customer), fact('Lane', lane(s)), fact('Request', s.subject), fact('Received', `${fmt.when(s.received_at)} UTC`),
      fact('Price all-in (quoted)', fmt.eurExact(s.price_eur)),
      fact('Booking', s.booking ? `${s.booking}${s.status === 'cancelled' ? ' (cancelled)' : ''}` : '–'),
      fact('Airline', s.airline), fact('Air freight booked', s.air_eur != null ? fmt.eurExact(s.air_eur) : '–'),
      fact('Mails', `${s.mails_in} in · ${s.mails_out} out`), fact('Last activity', `${ago(s.last_at)} ago`))),
    h('section', { class: 'card glass c7 flush' }, head('What happened', `${s.timeline.length} steps, oldest first${s.running ? ' · the desk is working on it' : ''}`),
      h('div', { class: 'timeline' }, s.timeline.map((item, i) => timelineItem(ctx, s.id, item, i)))),
    h('div', { class: 'stack c5' }, route,
      h('section', { class: 'card glass' }, head('Desk notes', 'what the desk wrote down, oldest first'),
        s.notes ? notesView(s.notes) : h('div', { class: 'empty' }, 'The desk wrote no notes for this shipment.'))));
  return { title: `Shipment ${s.id}`, sub: `${s.company} · ${lane(s)}`, node };
}

// --- Customers ---------------------------------------------------------------------------------

const customerCols = [
  { key: 'company', label: 'Customer', render: c => h('div', null, h('b', null, c.company), h('div', { class: 'sub' }, c.email)) },
  { key: 'shipments', label: 'Shipments', right: true },
  { key: 'open', label: 'Open', right: true },
  { key: 'booked', label: 'Booked', right: true },
  { key: 'lost', label: 'Lost', right: true },
  { key: 'win_rate_pct', label: 'Win rate', right: true, render: c => (c.win_rate_pct == null ? '–' : `${c.win_rate_pct}%`) },
  { key: 'booked_eur', label: 'Booked value', right: true, render: c => fmt.eur(c.booked_eur) },
  { key: 'destinations', label: 'Destinations', sort: false, render: c => h('span', { class: 'codes' }, c.destinations.map(d => h('span', { class: 'tag' }, d))), csv: c => c.destinations.join(' ') },
  { key: 'last_at', label: 'Last activity', right: true, render: c => ago(c.last_at) },
];

export async function customers(ctx) {
  const q = ctx.ui.filter.trim().toLowerCase(), all = ctx.snap.customers;
  const rows = all.filter(c => has(q, c.company, c.email, ...c.destinations));
  return { title: 'Customers', sub: `${all.length} customers`, node: h('div', { class: 'grid' },
    h('div', { class: 'toolbar c12' }, filterBox(ctx, 'Filter: name, address, destination …')),
    h('section', { class: 'card glass c12 flush' }, head('Customers', `${rows.length} of ${all.length}`, exportButton('talos-customers.csv', rows, customerCols)),
      table(rows, customerCols, { state: sortState(ctx, 'customers', 'shipments'), onRow: c => go(`#/customer/${encodeURIComponent(c.email)}`), empty: 'No customer matches.' }))) };
}

export async function customer(ctx, email) {
  const c = ctx.snap.customers.find(x => x.email === email);
  if (!c) return { title: 'Customer', node: h('div', { class: 'grid' }, h('div', { class: 'empty glass c12' }, `No customer ${email}.`)) };
  const items = ctx.snap.shipments.filter(s => s.customer === email);
  const byDest = Object.entries(items.reduce((m, s) => { if (s.destination) m[s.destination] = (m[s.destination] || 0) + 1; return m; }, {}))
    .map(([name, value]) => ({ name, value })).sort((a, b) => b.value - a.value);
  const node = h('div', { class: 'grid' },
    h('div', { class: 'title c12' }, h('a', { class: 'pill small', href: '#/customers' }, icon('back'), 'Customers'), h('h2', null, c.company), h('span', { class: 'muted' }, c.email)),
    h('div', { class: 'tiles' },
      tile('Shipments', fmt.int(c.shipments), `${c.open} open`), tile('Booked', fmt.int(c.booked)), tile('Lost', fmt.int(c.lost)),
      tile('Win rate', c.win_rate_pct == null ? '–' : `${c.win_rate_pct}%`, 'booked of decided'), tile('Booked value', fmt.eur(c.booked_eur), 'all-in')),
    h('section', { class: 'card glass c8 flush' }, head('Shipments', `${items.length}`),
      table(items, shipmentCols(false), { state: sortState(ctx, 'customer', 'last_at'), onRow: s => go(`#/shipment/${s.id}`) })),
    card('c4', 'Destinations', 'shipments per airport', byDest.length ? barList(byDest, { label: 60, solid: true }) : h('div', { class: 'empty' }, 'No destination on record.')));
  return { title: c.company, sub: `${c.shipments} shipments · ${fmt.eur(c.booked_eur)} booked`, node };
}

// --- Carriers ----------------------------------------------------------------------------------

export async function carriers(ctx, tab) {
  const which = tab === 'truckers' ? 'truckers' : 'airlines', data = ctx.snap.carriers[which];
  const q = ctx.ui.filter.trim().toLowerCase(), rows = data.filter(c => has(q, c.name, c.email));
  const cols = which === 'airlines' ? [
    { key: 'name', label: 'Airline', render: a => h('div', null, h('b', null, a.name), h('div', { class: 'sub' }, a.email)) },
    { key: 'bookings', label: 'Bookings', right: true, render: a => meter(100 * a.bookings / Math.max(1, ...data.map(x => x.bookings)), fmt.int(a.bookings)) },
    { key: 'cancelled', label: 'Cancelled', right: true },
    { key: 'booked_air_eur', label: 'Air freight booked', right: true, render: a => fmt.eur(a.booked_air_eur) },
    { key: 'destinations', label: 'Destinations served', right: true },
    { key: 'tariff_lanes', label: 'Tariff lanes', right: true },
    { key: 'last_booked_at', label: 'Last booking', right: true, render: a => (a.last_booked_at ? ago(a.last_booked_at) : '–') },
  ] : [
    { key: 'name', label: 'Trucker', render: t => h('div', null, h('b', null, t.name), h('div', { class: 'sub' }, t.email)) },
    { key: 'asked', label: 'Asked for pickups', right: true, render: t => meter(100 * t.asked / Math.max(1, ...data.map(x => x.asked)), fmt.int(t.asked)) },
    { key: 'offers', label: 'Offers', right: true },
    { key: 'declines', label: 'Declines', right: true },
    { key: 'reply_rate_pct', label: 'Reply rate', right: true, render: t => (t.reply_rate_pct == null ? '–' : `${t.reply_rate_pct}%`) },
    { key: 'median_reply_min', label: 'Typical reply time', right: true, render: t => minutes(t.median_reply_min) },
  ];
  const tabs = h('div', { class: 'toolbar c12' },
    h('div', { class: 'seg' }, [['airlines', 'Airlines'], ['truckers', 'Truckers']].map(([key, label]) =>
      h('button', { class: key === which ? 'on' : '', onclick: () => go(`#/carriers/${key}`) }, label, h('b', null, ctx.snap.carriers[key].length)))),
    filterBox(ctx, `Filter ${which} …`));
  const note = which === 'airlines' ? 'bookings from the platform\'s own answers' : 'pickup requests we sent and the replies to them';
  return { title: 'Carriers', sub: which === 'airlines' ? 'Airlines' : 'Truckers', node: h('div', { class: 'grid' }, tabs,
    h('section', { class: 'card glass c12 flush' }, head(which === 'airlines' ? 'Airlines' : 'Truckers', note, exportButton(`talos-${which}.csv`, rows, cols)),
      table(rows, cols, { state: sortState(ctx, which, which === 'airlines' ? 'bookings' : 'asked'), empty: 'No carrier matches.' }))) };
}

// --- Rates -------------------------------------------------------------------------------------

export async function rates(ctx) {
  const { lanes, zones } = await ctx.api('/crm/api/rates'), f = ctx.ui.rates;
  const pick = (key, label) => h('select', { 'aria-label': label, onchange: e => { f[key] = e.target.value; ctx.rerender(); } },
    h('option', { value: '' }, `${label}: all`), [...new Set(lanes.map(l => l[key]))].sort().map(v => h('option', { value: v, selected: f[key] === v || null }, v)));
  const rows = lanes.filter(l => (!f.origin || l.origin === f.origin) && (!f.destination || l.destination === f.destination) && (!f.airline || l.airline === f.airline));
  const breaks = l => { const kg = l.breaks_kg.split(' '), eur = l.rates_per_kg.split(' '); return kg.map((k, i) => `${k}+ kg ${eur[i]}`).join(' · '); };
  const cols = [
    { key: 'airline', label: 'Airline' }, { key: 'origin', label: 'From' }, { key: 'destination', label: 'To' },
    { key: 'transit_days', label: 'Transit', right: true, get: l => Number(l.transit_days), render: l => fmt.days(Number(l.transit_days)) },
    { key: 'minimum_eur', label: 'Minimum', right: true, get: l => Number(l.minimum_eur), render: l => fmt.eurExact(Number(l.minimum_eur)) },
    { key: 'rates_per_kg', label: 'EUR per kg by weight break', sort: false, cls: 'free', render: l => h('span', { class: 'sub' }, breaks(l)) },
    { key: 'fuel_per_kg', label: 'Fuel /kg', right: true }, { key: 'security_per_kg', label: 'Security /kg', right: true },
    { key: 'valid_to', label: 'Valid until', right: true, render: l => fmt.day(l.valid_to) },
    { key: 'source', label: 'Source', render: l => (l.source === 'start_kit' ? 'tariff file' : `mail ${l.source_message_id || ''}`) },
  ];
  return { title: 'Rates', sub: `${lanes.length} tariff lanes · portal airlines are priced live by the desk`, node: h('div', { class: 'grid' },
    h('div', { class: 'toolbar c12 filters' }, pick('origin', 'From'), pick('destination', 'To'), pick('airline', 'Airline')),
    h('section', { class: 'card glass c12 flush' }, head('Tariff lanes', `${rows.length} of ${lanes.length}`, exportButton('talos-rates.csv', rows, cols)),
      table(rows, cols, { state: sortState(ctx, 'rates', 'airline', 'asc'), empty: 'No lane for this choice.' })),
    h('section', { class: 'card glass c12 flush' }, head('Pickup zones', 'postcode zone → departure airport'),
      h('div', { class: 'zones' }, zones.map(z => h('span', { class: 'tag' }, `${z.zone} ${z.area} → ${z.airport}`))))) };
}

// --- Reports -----------------------------------------------------------------------------------

export async function reports(ctx) {
  const r = ctx.snap.reports, top = r.funnel[0].count;
  const funnel = r.funnel.map(f => ({ name: f.stage, value: f.count, text: `${fmt.int(f.count)}`, note: top ? ` ${Math.round(100 * f.count / top)}%` : '' }));
  const node = h('div', { class: 'grid' },
    h('div', { class: 'tiles' },
      tile('Requests', fmt.int(top)), tile('Booked', fmt.int(r.funnel.at(-1).count), `${r.cancelled} cancelled later`),
      tile('Win rate', r.win_rate_pct == null ? '–' : `${r.win_rate_pct}%`, `booked of decided · ${r.lost} lost`),
      tile('First answer', minutes(r.median_first_answer_min), 'median, request to first mail'),
      tile('Booked value', fmt.eur(r.booked_eur), 'all-in, from the desk notes')),
    card('c6', 'From request to booking', 'share of all requests', barList(funnel, { label: 90, solid: true })),
    card('c6', 'Busiest lanes', 'requests per lane', r.top_lanes.length ? barList(r.top_lanes.map(l => ({ name: l.lane, value: l.count })), { label: 110, solid: true }) : h('div', { class: 'empty' }, 'No lane on record.')),
    h('section', { class: 'card glass c12 flush' }, head('Why we lost', `${r.losses.length} declined, in the customer's own words`),
      r.losses.length ? h('div', { class: 'list' }, r.losses.map(l => h('div', { class: 'item info link', onclick: () => go(`#/shipment/${l.id}`) }, icon('cross'),
        h('div', null, h('b', null, `${l.company} · ${lane(l)}${l.price_eur != null ? ` · quoted ${fmt.eurExact(l.price_eur)}` : ''}`), h('p', { class: 'words' }, saying(l.words))),
        h('div', { class: 'meta' }, ago(l.at)))))
        : h('div', { class: 'empty' }, 'No request was lost.')),
    card('c6', 'Bookings by airline', null, barList(ctx.snap.carriers.airlines.filter(a => a.bookings).map(a => ({ name: a.name, value: a.bookings })), { label: 170, solid: true })),
    card('c6', 'Other work', null, h('div', { class: 'facts' }, fact('Tracking requests answered', fmt.int(r.tracking_requests)),
      fact('Median time to booking', minutes(r.median_to_booking_min)), fact('Cancelled bookings', fmt.int(r.cancelled)))));
  return { title: 'Reports', sub: 'since the desk started', node };
}

// --- Settings: where the figures come from -----------------------------------------------------

export async function settings(ctx) {
  const s = await ctx.api('/crm/api/settings');
  const cols = [
    { key: 'name', label: 'Source', render: x => h('b', null, x.name) }, { key: 'path', label: 'File', render: x => h('code', null, x.path) },
    { key: 'rows', label: 'Entries', right: true, render: x => fmt.int(x.rows) }, { key: 'note', label: 'What the CRM takes from it', cls: 'free' },
  ];
  const node = h('div', { class: 'grid' },
    card('c6', 'Desk', s.desk.label, h('div', { class: 'facts' }, fact('Last activity', s.desk.last_activity_at ? `${fmt.when(s.desk.last_activity_at)} UTC` : '–'),
      fact('Last mail read', s.desk.last_mail_id), fact('Build', s.build),
      s.dry_until ? fact('Dry run until', `${fmt.when(s.dry_until)} UTC · ${s.dry_hidden} requests left out`) : null)),
    card('c6', 'This console', null, h('p', { class: 'soft' }, 'Read-only. It shows what the ultra-talos desk has done and changes nothing: no mail, no booking, no file. It reads the files below and follows them every 2 seconds.'),
      s.needs_human ? h('p', { class: 'soft' }, h('b', null, 'Desk note to a person: '), s.needs_human.text, s.needs_human.outdated ? ' (outdated: the shipment is closed)' : '') : null),
    h('section', { class: 'card glass c12 flush' }, head('Data sources', s.root), table(s.sources, cols)));
  return { title: 'Settings', sub: 'Data sources', node };
}
