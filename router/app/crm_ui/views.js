// The pages. Each takes the context (snapshot, UI state, helpers) and returns
// {title, sub, node}; pages that need more than the snapshot fetch it themselves.

import { createGlobe, REGIONS, routeView } from './globe.js';
import { barList, card, category, columns, exportButton, fmt, h, icon, KINDS, meter, ROLES, splitBar, stateBadge, STATES, table, tipRows, words } from './lib.js';

const go = hash => { location.hash = hash; };
const matches = (q, ...fields) => !q || fields.some(f => String(f ?? '').toLowerCase().includes(q));
const tile = (label, value, foot, unit, onclick) => h(onclick ? 'button' : 'div', { class: `tile glass${onclick ? ' link' : ''}`, onclick },
  h('div', { class: 'label' }, label), h('div', { class: 'value' }, value, unit && h('small', null, unit)), foot && h('div', { class: 'foot' }, foot));
const codes = list => h('span', { class: 'codes' }, list.map(c => h('span', { class: 'tag' }, c)));
const laneText = d => `${d.origin || '?'} → ${d.destination || '?'}`;
const state = d => STATES[d.state]?.[0] || d.state;
const head = (title, note, right) => h('header', null, h('h3', null, title), note && h('span', { class: 'note' }, note), right && h('span', { class: 'right' }, right));
const fact = (label, value) => h('div', null, h('span', null, label), h('b', null, value ?? '–'));
const sortState = (ctx, name, key, dir = 'desc') => {
  const sort = (ctx.ui.sort[name] ||= { key, dir });
  sort.changed = ctx.rerender;
  return sort;
};
const priceSplit = d => (d.all_in_eur == null ? null : splitBar([
  { name: 'Air freight', value: d.air_eur, text: fmt.eur(d.air_eur), cls: 's1' },
  { name: d.truck_mode === 'estimate' ? 'Trucking (estimate)' : 'Trucking', value: d.truck_eur, text: fmt.eur(d.truck_eur), cls: 's2' }]));

// One line on the desk; the full wording sits behind it as a tooltip and on the analysis page.
function dryRunNotice(snap) {
  if (!snap.limits.length) return null;
  return h('div', { class: 'notice glass c12', title: snap.limits.join('\n') }, icon('info'),
    h('div', null, h('b', null, 'Recorded dry run. '), 'Timings and acceptance rates are not reported; bookings are simulated.'));
}

function globeCard(ctx, span, { height, title = 'Routes', lanes = ctx.snap.lanes, compact = false } = {}) {
  const { snap, ui } = ctx;
  const host = h('div');
  const routed = lanes.filter(l => l.origin);
  const globe = createGlobe(host, {
    airports: snap.airports, lanes, deals: snap.deals, airlines: snap.airlines, height, compact,
    view: compact ? ctx.focusView : ui.globe, coverage: ui.coverage,
    onLane: lane => { ui.pipeline.lane = lane.lane; ui.pipeline.state = null; go('#/pipeline'); },
  });
  ctx.globes.push(globe);
  const controls = compact ? null : [
    h('div', { class: 'seg', role: 'group', 'aria-label': 'Turn the globe to' }, Object.entries(REGIONS).map(([name, [lon, lat]]) => h('button', { onclick: () => globe.lookAt(lon, lat) }, name))),
    h('label', { class: 'chip', style: { cursor: 'pointer' } },
      h('input', { type: 'checkbox', checked: ui.coverage || null, onchange: e => { ui.coverage = e.target.checked; globe.setCoverage(ui.coverage); } }),
      'All served destinations')];
  const transit = routed.reduce((n, l) => n + l.in_transit.length, 0);
  return h('section', { class: `card glass ${span}` },
    h('header', null, h('h3', null, title),
      !compact && h('span', { class: 'note' }, `${routed.length} ${routed.length === 1 ? 'route' : 'routes'}${transit ? ` · ${transit} in transit` : ''}`),
      controls && h('span', { class: 'right' }, controls)),
    host,
    compact
      ? h('div', { class: 'muted', style: { marginTop: '8px', fontSize: '12.5px' } }, 'Great-circle path between the two airports. Drag to turn.')
      : h('div', { class: 'legend', style: { marginTop: '10px' } },
          h('span', null, h('i', { class: 'line' }), 'Accepted'), h('span', null, h('i', { class: 'line dash' }), 'Quoted or open'),
          h('span', null, h('i', { class: 'line off' }), 'Declined'), h('span', null, h('i', { class: 'ring' }), 'Departure airport'),
          h('span', { class: 'muted' }, 'Drag to turn. Line width: number of requests. Great-circle paths; a shipment in transit is placed by its planned dates.')));
}

function actionList(items) {
  if (!items.length) return h('div', { class: 'empty' }, 'No open actions.');
  const glyph = { critical: 'alert', warning: 'alert', info: 'clock' };
  return h('div', { class: 'list' }, items.map(item => h('div', {
    class: `item ${item.level}${item.shipment_id ? ' link' : ''}`,
    onclick: item.shipment_id ? () => go(`#/shipment/${item.shipment_id}`) : null,
  }, icon(glyph[item.level]),
    h('div', null, h('b', null, item.title), h('p', null, item.detail)),
    h('div', { class: 'meta' }, item.company, item.lane && h('div', null, item.lane.replace('-', ' → '))))));
}

// --- desk ---------------------------------------------------------------------------------

const KEY_FIGURES = ['time_to_quote', 'win_rate', 'truck_estimates', 'air_spread', 'speed_cost', 'truck_share', 'clarifications'];

export function desk(ctx) {
  const { snap } = ctx, k = snap.kpis, live = snap.source === 'live';
  const stages = snap.pipeline.filter(p => p.count).map(p => ({
    name: STATES[p.state][0], value: p.count, text: fmt.int(p.count), note: p.eur ? fmt.eur(p.eur) : null, href: '#/pipeline', state: p.state,
    tip: () => tipRows(STATES[p.state][0], [['Shipments', fmt.int(p.count)], ['Quoted value', fmt.eur(p.eur)]]),
  }));
  const stageBars = barList(stages, { label: 150, solid: true });
  [...stageBars.children].forEach((row, i) => row.addEventListener('click', () => { ctx.ui.pipeline.state = stages[i].state; ctx.ui.pipeline.lane = null; }, true));
  const figures = KEY_FIGURES.map(key => snap.findings.find(f => f.key === key)).filter(Boolean).slice(0, 6);
  const open = stage => { Object.assign(ctx.ui.pipeline, { state: stage, lane: null, account: null, goal: '', truck: '' }); go('#/pipeline'); };
  const noLive = live && !snap.deals.length ? h('div', { class: 'notice glass c12' }, icon('info'), h('div', null,
    `The live database contains no priced shipments${snap.legacy.length ? ` (${snap.legacy.length} requests predate this router and carry no prices)` : ''}. `,
    ctx.sources.replay.available ? [`The recorded dry run contains ${ctx.sources.replay.shipments}. `, h('button', { class: 'linkbtn', onclick: () => ctx.pick('replay') }, 'Show dry run')] : null)) : null;
  const node = h('div', { class: 'grid' },
    dryRunNotice(snap), noLive,
    h('div', { class: 'tiles' },
      tile('Requests', fmt.int(k.requests), `${k.open} open · ${k.failed} failed`, null, () => open(null)),
      tile('Quoted', fmt.eur(k.quoted_eur), `${fmt.int(k.quoted)} quotes`, null, () => open(null)),
      tile('Accepted', fmt.eur(k.accepted_eur), k.win_rate_pct != null ? `${k.accepted} quotes · ${fmt.pct(k.win_rate_pct)} acceptance` : `${k.accepted} accepted · ${k.rejected} declined`, null, () => open('BOOKED')),
      tile('Awaiting decision', fmt.eur(k.awaiting_decision_eur), `${fmt.int(k.awaiting_decision)} ${k.awaiting_decision === 1 ? 'quote' : 'quotes'}`, null, () => open('QUOTED')),
      tile('Median quote', fmt.eur(k.median_all_in_eur), 'all-in', null, () => go('#/analysis')),
      live ? tile('Time to quote', k.median_minutes_to_quote == null ? '–' : fmt.one(k.median_minutes_to_quote),
        k.quotes_timed ? `median · ${k.quotes_in_window} of ${k.quotes_timed} within 6 min` : 'no quote issued yet', k.median_minutes_to_quote == null ? null : 'min', () => go('#/analysis')) : null),
    h('section', { class: 'card glass c7 flush' }, head('Action required', `${snap.attention.length} open`), actionList(snap.attention)),
    card('c5', 'Pipeline status', snap.legacy.length ? `${snap.legacy.length} legacy requests excluded` : null,
      stages.length ? stageBars : h('div', { class: 'empty' }, 'No shipments.')),
    globeCard(ctx, 'c7', { height: 440 }),
    h('section', { class: 'card glass c5' }, head('Key figures', null, h('a', { href: '#/analysis' }, 'Full analysis')),
      figures.length
        ? h('div', { class: 'figures' }, figures.map(f => h('div', { class: 'figure', title: f.text },
            h('span', null, f.title), h('b', null, fmt.figure(f.value, f.unit, f.n)), h('small', null, f.unit === 'count' ? '' : `n = ${f.n}`))))
        : h('div', { class: 'empty' }, 'No priced shipments to analyse.')));
  return { title: 'Desk', sub: `${fmt.int(k.requests)} requests · ${fmt.int(k.quoted)} quoted · ${fmt.int(k.accepted)} accepted`, node };
}

// --- pipeline -----------------------------------------------------------------------------

export function pipeline(ctx) {
  const { snap, ui, q } = ctx, f = ui.pipeline;
  const rows = snap.deals.filter(d => (!f.state || d.state === f.state) && (!f.lane || d.lane === f.lane || `?-${d.destination}` === f.lane)
    && (!f.account || d.account === f.account) && (!f.goal || (d.goal || 'none') === f.goal) && (!f.truck || d.truck_mode === f.truck)
    && matches(q, d.id, d.company, d.contact, d.lane, d.subject, d.airline, d.commodity, state(d), d.pickup_city));
  const pill = (label, count, on, choose) => h('button', { class: `pill${on ? ' on' : ''}`, onclick: () => { choose(); ctx.rerender(); } }, label, count != null && h('b', null, count));
  const pick = (key, label, options) => h('select', { 'aria-label': label, onchange: e => { f[key] = e.target.value; ctx.rerender(); } },
    h('option', { value: '' }, `${label}: all`), options.map(([value, text]) => h('option', { value, selected: f[key] === value || null }, text)));
  const filters = h('div', { class: 'filters' },
    pill('All', snap.deals.length, !f.state, () => { f.state = null; }),
    snap.pipeline.filter(p => p.count).map(p => pill(STATES[p.state][0], p.count, f.state === p.state, () => { f.state = f.state === p.state ? null : p.state; })),
    f.lane && pill(`Lane ${f.lane.replace('-', ' → ')} ×`, null, true, () => { f.lane = null; }),
    f.account && pill(`${snap.accounts.find(a => a.email === f.account)?.company || f.account} ×`, null, true, () => { f.account = null; }),
    pick('goal', 'Priority', [['fastest', 'Fastest'], ['cheapest', 'Cheapest'], ['none', 'Not stated']]),
    pick('truck', 'Trucking', [['quoted', 'Offer received'], ['estimate', 'Estimated'], ['none', 'Not required']]));
  const cols = [
    { key: 'id', label: 'No.', render: d => h('span', { class: 'num soft' }, d.id) },
    { key: 'company', label: 'Account', cls: 'clip', render: d => [d.company, h('span', { class: 'sub' }, d.contact || d.account)] },
    { key: 'lane', label: 'Route', get: d => d.lane || `?-${d.destination}`, render: d => [laneText(d),
      h('span', { class: 'sub' }, d.pickup ? `pickup ${d.pickup_city || d.pickup_postal_code || ''}`.trim() : 'airport delivery')] },
    { key: 'distance_km', label: 'Distance km', hidden: true },
    { key: 'gross_kg', label: 'Gross kg', right: true, render: d => [fmt.kg(d.gross_kg), h('span', { class: 'sub', title: d.volumetric ? 'Billed on volume weight' : null }, `${fmt.cbm(d.volume_cbm)}${d.volumetric ? ' · vol.' : ''}`)] },
    { key: 'volume_cbm', label: 'Volume cbm', csv: d => d.volume_cbm, render: () => null, hidden: true },
    { key: 'goal', label: 'Priority', render: d => d.goal || h('span', { class: 'muted' }, 'not stated'), csv: d => d.goal || '' },
    { key: 'all_in_eur', label: 'All-in EUR', right: true, render: d => (d.all_in_eur == null ? '–' : fmt.eurExact(d.all_in_eur)) },
    { key: 'air_eur', label: 'Air EUR', hidden: true }, { key: 'truck_eur', label: 'Trucking EUR', hidden: true },
    { key: 'split', label: 'Split', sort: false, csv: false, render: d => priceSplit(d) || h('span', { class: 'muted' }, '–') },
    { key: 'airline', label: 'Airline', cls: 'clip', render: d => (d.airline ? [d.airline, h('span', { class: 'sub' }, `${fmt.days(d.transit_days)}${d.truck_mode === 'estimate' ? ' · est. trucking' : ''}`)] : '–') },
    { key: 'transit_days', label: 'Transit days', hidden: true },
    { key: 'state', label: 'Status', get: d => Object.keys(STATES).indexOf(d.state), csv: state, render: stateBadge },
    { key: 'ready_date', label: 'Ready', render: d => fmt.day(d.ready_date), csv: d => d.ready_date },
  ];
  const shown = cols.filter(c => !c.hidden);
  const node = h('div', { class: 'grid' }, filters,
    h('section', { class: 'card glass c12 flush' },
      head('Shipments', `${rows.length} of ${snap.deals.length}`, [
        h('span', { class: 'legend' }, h('span', null, h('i', { class: 's1' }), 'Air freight'), h('span', null, h('i', { class: 's2' }), 'Trucking')),
        exportButton('talos-shipments.csv', rows, cols)]),
      table(rows, shown, { state: sortState(ctx, 'pipeline', 'id'), onRow: d => go(`#/shipment/${d.id}`), empty: 'No shipment matches the filters.' })));
  return { title: 'Pipeline', sub: `${snap.deals.length} shipments · ${fmt.eur(snap.kpis.quoted_eur)} quoted`, node };
}

// --- one shipment -------------------------------------------------------------------------

const STEPS = ['Request', 'Details complete', 'Priced', 'Quoted', 'Decision', 'Booked'];
const STEP_OF = { NEW: 0, AWAITING_INFO: 0, PRICING: 1, QUOTED: 3, BOOKING: 4, BOOKED: 5, CONFIRMED: 5, REJECTED: 4, FAILED: -1 };

function correspondence(detail) {
  if (!detail.timeline.length) return h('div', { class: 'empty' }, 'No mail on record for this shipment.');
  return h('div', { class: 'thread' }, detail.timeline.map(m => {
    const out = m.direction === 'out';
    const long = m.body.length > 320 || m.body.split('\n').length > 7;
    const body = h('pre', { class: long ? 'short' : '' }, m.body);
    const more = long ? h('button', { class: 'more', onclick: e => { const folded = body.classList.toggle('short'); e.target.textContent = folded ? 'Show full text' : 'Collapse'; } }, 'Show full text') : null;
    return h('div', { class: `msg ${out ? 'out' : 'in'}` },
      h('div', { class: 'who' }, h('b', null, out ? 'TALOS' : m.who),
        h('span', null, out ? `${KINDS[m.kind] || words(m.kind)} to ${m.who}` : category(m.category)),
        m.at && h('span', null, `${fmt.when(m.at)} UTC`),
        out && !m.sent && h('span', { class: 'tag' }, detail.source === 'replay' ? 'simulated' : 'draft, not sent'),
        out && m.used_template && h('span', { class: 'tag' }, 'template'),
        !out && m.confidence < 0.7 && h('span', { class: 'tag' }, `low confidence, ${fmt.int(100 * m.confidence)}%`)),
      body, more);
  }));
}

export async function shipment(ctx, id) {
  let detail;
  try { detail = await ctx.api(`/crm/api/shipments/${id}`); } catch (error) {
    return { title: 'Shipment', sub: '', node: h('div', { class: 'grid' }, h('div', { class: 'empty glass c12' }, error.message)) };
  }
  const d = detail.deal, reached = STEP_OF[d.state];
  const steps = h('div', { class: 'steps' }, STEPS.map((label, i) => h('div', {
    class: `step${reached >= i ? ' done' : ''}${d.state === 'FAILED' && i === 0 ? ' stop' : ''}${d.state === 'REJECTED' && i === 4 ? ' lost' : ''}`,
  }, d.state === 'REJECTED' && i === 4 ? 'Declined' : d.state === 'FAILED' && i === 0 ? 'Failed' : label)));
  const account = ctx.snap.accounts.find(a => a.email === d.account);

  const request = card('', 'Request', d.subject, h('div', { class: 'facts' },
    fact('Origin', d.pickup ? `${d.pickup_postal_code || ''} ${d.pickup_city || ''}`.trim() || '–' : `delivered to ${d.origin || 'the airport'}`),
    fact('Departure airport', d.origin ? `${d.origin}${d.zone ? ` (zone ${d.zone})` : ''}` : '–'),
    fact('Destination', d.destination ? `${d.destination} · ${ctx.snap.airports[d.destination]?.city || ''}` : '–'),
    fact('Ready date', fmt.day(d.ready_date)), fact('Pieces', fmt.int(d.pieces)), fact('Gross weight', fmt.kg(d.gross_kg)),
    fact('Volume', fmt.cbm(d.volume_cbm)), fact('Chargeable weight', `${fmt.kg(d.chargeable_kg)}${d.volumetric ? ' (volumetric)' : ''}`),
    fact('Commodity', d.commodity), fact('Priority', d.goal || 'not stated (priced as cheapest)'), fact('Contact', d.contact),
    d.extra_questions.length ? fact('Further questions', d.extra_questions.join('; ')) : null,
    d.assumptions.length ? fact('Assumptions', d.assumptions.join('; ')) : null,
    d.missing.length ? fact('Missing', d.missing.join('; ')) : null));

  const offer = d.all_in_eur == null ? null : card('', 'Offer', d.valid_until ? `valid until ${d.valid_until}` : null,
    h('div', { style: { display: 'grid', gap: '14px' } },
      h('div', { class: 'hero' }, fmt.eurExact(d.all_in_eur), h('small', null, 'all-in')),
      h('div', { style: { display: 'grid', gap: '8px', maxWidth: '520px' } }, priceSplit(d),
        h('div', { class: 'legend' }, h('span', null, h('i', { class: 's1' }), `Air freight ${fmt.eurExact(d.air_eur)}`),
          d.truck_eur ? h('span', null, h('i', { class: 's2' }), `Trucking ${fmt.eurExact(d.truck_eur)}${d.truck_mode === 'estimate' ? ' (estimate, no offer received)' : ''}`) : h('span', { class: 'muted' }, 'No trucking: airport delivery by the customer'))),
      h('div', { class: 'facts' },
        fact('Airline', d.airline), fact('Transit', fmt.days(d.transit_days)), fact('Rate source', d.air_source === 'tariff' ? 'tariff file' : 'portal'),
        fact('Billed weight', `${fmt.kg(d.billed_kg)}${d.weight_break ? ' (next weight break)' : ''}`), fact('Air rate', d.air_eur_per_kg ? `${fmt.two(d.air_eur_per_kg)} €/kg` : '–'),
        fact('Pickup', fmt.day(d.estimated_pickup)), fact('Departure', fmt.day(d.estimated_departure)), fact('Arrival', fmt.day(d.estimated_arrival)),
        d.booking_ref ? fact('Booking reference', d.dry_run_booking ? 'none (dry run)' : d.booking_ref) : null)));

  const top = Math.max(...detail.options.map(o => o.total_eur), 0);
  const flights = detail.options.length ? card('', 'Air options', `${detail.options.length} priced · selected option highlighted`,
    barList(detail.options.map(o => ({
      name: o.airline, value: o.total_eur, em: o.chosen, text: fmt.eurExact(o.total_eur),
      note: `${fmt.days(o.transit_days)}${o.cheapest ? ' · lowest' : ''}${o.fastest ? ' · fastest' : ''}`,
      tip: () => tipRows(o.airline, [['Total', fmt.eurExact(o.total_eur)], ['Transit', fmt.days(o.transit_days)], ['Billed weight', fmt.kg(o.billed_kg)],
        ['Rate', `${fmt.two(o.eur_per_kg)} €/kg`], ['Source', o.source === 'tariff' ? 'tariff file' : 'portal'], o.chosen && ['Selected', 'yes']]),
    })), { label: 190, max: top })) : null;

  const answered = detail.truckers.filter(t => t.price_eur != null).length;
  const trucks = detail.truckers.length ? card('', 'Trucking offers', `${answered} of ${detail.truckers.length} received`,
    barList(detail.truckers.map(t => ({
      name: t.name, value: t.price_eur || 0, em: t.chosen, text: t.price_eur == null ? 'no offer' : fmt.eurExact(t.price_eur),
      note: t.price_eur == null ? null : `${t.pickup_hours != null ? `pickup within ${fmt.one(t.pickup_hours)} h` : ''}${t.cheapest ? ' · lowest' : ''}`,
      tip: () => tipRows(t.name, [['Offer', t.price_eur == null ? 'none' : fmt.eurExact(t.price_eur)], t.pickup_hours != null && ['Pickup within', `${fmt.one(t.pickup_hours)} h`],
        t.ref && ['Reference', t.ref], t.response_s != null && ['Response time', fmt.duration(t.response_s)], t.chosen && ['Selected', 'yes']]),
    })), { label: 190 })) : null;

  const a = d.air, readings = [];
  if (a) {
    if (a.premium_eur > 0) readings.push(['Selected flight vs lowest price', `+${fmt.eur(a.premium_eur)} (${fmt.pct(a.premium_pct)})`]);
    else if (a.premium_eur === 0) readings.push(['Selected flight', 'lowest price']);
    if (a.speed_days > 0) readings.push([`Faster transit (${fmt.days(a.speed_days)} less)`, `+${fmt.eur(a.speed_eur)}`]);
    if (a.spread_pct != null) readings.push(['Air rate spread', fmt.pct(a.spread_pct)]);
  }
  if (d.truck?.spread_pct != null) readings.push(['Trucking offer spread', fmt.pct(d.truck.spread_pct)]);
  if (d.truck_eur && d.all_in_eur) readings.push(['Trucking share of price', fmt.pct(100 * d.truck_eur / d.all_in_eur)]);
  if (d.all_in_eur && d.chargeable_kg) readings.push(['All-in per chargeable kg', `${fmt.two(d.all_in_eur / d.chargeable_kg)} €`]);
  if (d.minutes_to_quote != null) readings.push(['Time to quote', `${fmt.one(d.minutes_to_quote)} min`]);

  const lane = ctx.snap.lanes.filter(l => l.shipments.includes(d.id));
  const here = ctx.snap.airports[d.destination], from = ctx.snap.airports[d.origin];
  ctx.focusView = here && from ? routeView(from, here) : { lon: 12, lat: 30, zoom: 1 };
  const side = h('div', { class: 'stack c4' },
    lane.some(l => l.origin) ? globeCard(ctx, '', { height: 260, title: `${laneText(d)}${d.distance_km ? ` · ${fmt.km(d.distance_km)}` : ''}`, lanes: lane, compact: true }) : null,
    readings.length ? card('', 'Offer analysis', null, h('div', { class: 'figures' }, readings.map(([k, v]) => h('div', { class: 'figure' }, h('span', null, k), h('b', null, v))))) : null,
    account ? h('section', { class: 'card glass', style: { cursor: 'pointer' }, onclick: () => go(`#/account/${encodeURIComponent(account.email)}`) },
      head(account.company, 'account'),
      h('div', { class: 'facts' }, fact('Requests', fmt.int(account.requests)), fact('Accepted', fmt.int(account.accepted)), fact('Quoted value', fmt.eur(account.quoted_eur)), fact('Mails', fmt.int(account.mails_in)))) : null,
    d.error ? card('', 'Error', null, h('p', { class: 'soft' }, d.error)) : null);

  const node = h('div', { class: 'grid' },
    h('div', { class: 'crumbs' }, h('a', { href: '#/pipeline' }, 'Pipeline'), '/', h('span', null, `Shipment ${d.id}`)),
    h('div', { class: 'title' }, h('h2', null, d.company), stateBadge(d), h('span', { class: 'muted' }, laneText(d))),
    h('section', { class: 'card glass c12' }, steps),
    h('div', { class: 'stack c8' }, request, offer, flights, trucks, card('', 'Correspondence', `${detail.timeline.length} mails`, correspondence(detail))),
    side);
  return { title: `Shipment ${d.id}`, sub: `${d.company} · ${laneText(d)}`, node };
}

// --- accounts -----------------------------------------------------------------------------

const priorities = goals => Object.entries(goals).map(([goal, n]) => `${n} ${goal}`).join(', ');

export function accounts(ctx) {
  const { snap, q } = ctx;
  const rows = snap.accounts.filter(a => matches(q, a.company, a.email, a.contacts.join(' '), a.destinations.join(' ')));
  const top = Math.max(...snap.accounts.map(a => a.requests), 1);
  const cols = [
    { key: 'company', label: 'Account', cls: 'clip', render: a => [a.company, h('span', { class: 'sub' }, a.contacts.join(', ') || a.email)] },
    { key: 'email', label: 'Email', hidden: true },
    { key: 'requests', label: 'Requests', right: true, render: a => [meter(100 * a.requests / top, fmt.int(a.requests)), a.legacy_requests ? h('span', { class: 'sub' }, `+${a.legacy_requests} legacy`) : null] },
    { key: 'quoted', label: 'Quoted', right: true }, { key: 'accepted', label: 'Accepted', right: true },
    { key: 'rejected', label: 'Declined', right: true }, { key: 'open', label: 'Open', right: true },
    { key: 'quoted_eur', label: 'Quoted EUR', right: true, render: a => fmt.eur(a.quoted_eur) },
    { key: 'accepted_eur', label: 'Accepted EUR', right: true, render: a => fmt.eur(a.accepted_eur) },
    { key: 'avg_gross_kg', label: 'Avg. kg', right: true, render: a => fmt.int(a.avg_gross_kg) },
    { key: 'goals', label: 'Priority', sort: false, render: a => priorities(a.goals) || '–', csv: a => priorities(a.goals) },
    { key: 'destinations', label: 'Destinations', sort: false, render: a => codes(a.destinations), csv: a => a.destinations.join(' ') },
    { key: 'mails_in', label: 'Mails', right: true },
    { key: 'last_mail_at', label: 'Last mail (UTC)', render: a => fmt.when(a.last_mail_at) },
  ];
  const node = h('div', { class: 'grid' }, h('section', { class: 'card glass c12 flush' },
    head('Accounts', `${rows.length} customers`, exportButton('talos-accounts.csv', rows, cols)),
    table(rows, cols.filter(c => !c.hidden), { state: sortState(ctx, 'accounts', 'requests'), onRow: a => go(`#/account/${encodeURIComponent(a.email)}`), empty: 'No account matches.' })));
  return { title: 'Accounts', sub: `${snap.accounts.length} customers`, node };
}

export async function account(ctx, email) {
  const { snap } = ctx, a = snap.accounts.find(x => x.email === email);
  if (!a) return { title: 'Account', sub: '', node: h('div', { class: 'grid' }, h('div', { class: 'empty glass c12' }, `No account ${email} in this data.`)) };
  const deals = snap.deals.filter(d => d.account === email);
  const mail = await ctx.api(`/crm/api/mails?limit=40&sender=${encodeURIComponent(email)}`);
  const cols = [
    { key: 'id', label: 'No.', render: d => h('span', { class: 'num soft' }, d.id) },
    { key: 'lane', label: 'Route', render: d => [laneText(d), h('span', { class: 'sub' }, d.pickup ? `pickup ${d.pickup_city || d.pickup_postal_code || ''}`.trim() : 'airport delivery')] },
    { key: 'gross_kg', label: 'Cargo', right: true, render: d => [fmt.kg(d.gross_kg), h('span', { class: 'sub' }, fmt.cbm(d.volume_cbm))] },
    { key: 'commodity', label: 'Commodity', cls: 'clip', render: d => d.commodity || '–' },
    { key: 'goal', label: 'Priority', render: d => d.goal || '–' },
    { key: 'all_in_eur', label: 'All-in', right: true, render: d => (d.all_in_eur == null ? '–' : fmt.eurExact(d.all_in_eur)) },
    { key: 'airline', label: 'Airline', render: d => d.airline || '–' }, { key: 'state', label: 'Status', render: stateBadge },
    { key: 'ready_date', label: 'Ready', render: d => fmt.day(d.ready_date) },
  ];
  const byDest = Object.entries(deals.reduce((m, d) => { if (d.destination) m[d.destination] = (m[d.destination] || 0) + 1; return m; }, {}))
    .sort((x, y) => y[1] - x[1]).map(([code, n]) => ({ name: `${code} · ${snap.airports[code]?.city || ''}`, value: n }));
  const node = h('div', { class: 'grid' },
    h('div', { class: 'crumbs' }, h('a', { href: '#/accounts' }, 'Accounts'), '/', h('span', null, a.company)),
    h('div', { class: 'title' }, h('h2', null, a.company), h('span', { class: 'muted' }, a.email), a.contacts.length ? h('span', { class: 'chip' }, `Contact: ${a.contacts.join(', ')}`) : null,
      a.languages.length ? h('span', { class: 'chip' }, `Language: ${a.languages.join(', ')}`) : null),
    h('div', { class: 'tiles' },
      tile('Requests', fmt.int(a.requests), a.legacy_requests ? `+${a.legacy_requests} legacy` : `${fmt.pct(a.share_pct)} of all requests`),
      tile('Quoted', fmt.eur(a.quoted_eur), `${a.quoted} quotes`),
      tile('Accepted', fmt.eur(a.accepted_eur), a.win_rate_pct != null ? `${a.accepted} quotes · ${fmt.pct(a.win_rate_pct)} acceptance` : `${a.accepted} accepted · ${a.rejected} declined`),
      tile('Average quote', fmt.eur(a.avg_all_in_eur), 'all-in'),
      tile('Average weight', a.avg_gross_kg == null ? '–' : fmt.int(a.avg_gross_kg), `${a.pickup_requests} of ${a.requests} with pickup`, a.avg_gross_kg == null ? null : 'kg'),
      tile('Mails received', fmt.int(a.mails_in), a.last_mail_at ? `last ${fmt.when(a.last_mail_at)} UTC` : null)),
    h('section', { class: 'card glass c12 flush' }, head('Shipments'),
      table(deals, cols, { state: sortState(ctx, 'account', 'id'), onRow: d => go(`#/shipment/${d.id}`), empty: 'No priced shipment for this account.' })),
    card('c6', 'Destinations', null, byDest.length ? barList(byDest, { label: 190, solid: true }) : h('div', { class: 'empty' }, 'No destination on record.')),
    card('c6', 'Request profile', null, h('div', { class: 'facts' },
      Object.entries(a.goals).map(([g, n]) => fact(`Priority: ${g}`, `${n} of ${a.requests}`)),
      fact('With pickup', `${a.pickup_requests} of ${a.requests}`), fact('Average weight', fmt.kg(a.avg_gross_kg)))),
    h('section', { class: 'card glass c12 flush' }, head('Correspondence', `${mail.total} mails received`), mailRows(mail.items, new Set())));
  return { title: a.company, sub: `${a.requests} requests · ${fmt.eur(a.quoted_eur)} quoted`, node };
}

// --- network ------------------------------------------------------------------------------

export function network(ctx) {
  const { snap, q } = ctx;
  const rows = snap.lanes.filter(l => matches(q, l.lane, l.best_airline, l.accounts.join(' '), snap.airports[l.destination]?.city));
  const dests = Object.entries(snap.lanes.reduce((m, l) => { m[l.destination] = (m[l.destination] || 0) + l.requests; return m; }, {}))
    .sort((a, b) => b[1] - a[1]).slice(0, 12).map(([code, n]) => ({ name: `${code} · ${snap.airports[code]?.city || ''}`, value: n }));
  const served = Object.values(snap.airports).filter(a => !a.origin).length, asked = new Set(snap.lanes.map(l => l.destination)).size;
  const cols = [
    { key: 'lane', label: 'Lane', render: l => [l.lane.replace('-', ' → '), h('span', { class: 'sub' }, `${snap.airports[l.origin]?.city || 'origin unknown'} to ${snap.airports[l.destination]?.city || l.destination}`)] },
    { key: 'distance_km', label: 'Distance km', right: true, render: l => fmt.int(l.distance_km) },
    { key: 'requests', label: 'Requests', right: true }, { key: 'quoted', label: 'Quoted', right: true },
    { key: 'accepted', label: 'Accepted', right: true }, { key: 'rejected', label: 'Declined', right: true },
    { key: 'avg_all_in_eur', label: 'Avg. all-in EUR', right: true, render: l => fmt.eur(l.avg_all_in_eur) },
    { key: 'avg_air_eur_per_kg', label: 'Quoted air €/kg', right: true, render: l => fmt.two(l.avg_air_eur_per_kg) },
    { key: 'best_eur_per_kg', label: 'Lowest rate €/kg', right: true, render: l => (l.best_airline ? [fmt.two(l.best_eur_per_kg), h('span', { class: 'sub' }, l.best_airline)] : '–') },
    { key: 'best_airline', label: 'Lowest-rate airline', hidden: true },
    { key: 'fastest_days', label: 'Fastest transit', right: true, render: l => fmt.days(l.fastest_days), csv: l => l.fastest_days },
    { key: 'airlines_priced', label: 'Airlines priced', right: true },
  ];
  const node = h('div', { class: 'grid' },
    globeCard(ctx, 'c8', { height: 560, title: 'Route network' }),
    h('div', { class: 'stack c4' },
      h('div', { class: 'pair' },
        tile('Lanes', fmt.int(snap.lanes.filter(l => l.origin).length), 'origin and destination known'),
        tile('Destinations', `${asked}`, `of ${served} served`)),
      card('', 'Requests by destination', null, dests.length ? barList(dests, { label: 170, solid: true }) : h('div', { class: 'empty' }, 'No requests.'))),
    h('section', { class: 'card glass c12 flush' }, head('Lanes', `${rows.length} lanes`, exportButton('talos-lanes.csv', rows, cols)),
      table(rows, cols.filter(c => !c.hidden), { state: sortState(ctx, 'lanes', 'requests'), onRow: l => { ctx.ui.pipeline.lane = l.lane; ctx.ui.pipeline.state = null; go('#/pipeline'); }, empty: 'No lane matches.' })));
  return { title: 'Network', sub: `${snap.lanes.length} lanes · ${asked} destinations`, node };
}

// --- carriers -----------------------------------------------------------------------------

export async function carriers(ctx, tab = 'airlines') {
  const { snap, q, ui } = ctx, live = snap.source === 'live';
  const tabs = h('div', { class: 'tabs' }, h('div', { class: 'seg' }, [['airlines', 'Airlines'], ['truckers', 'Truckers'], ['zones', 'Pickup zones'], ['tariffs', 'Tariffs']]
    .map(([key, label]) => h('button', { class: key === tab ? 'on' : '', onclick: () => go(`#/carriers/${key}`) }, label))));
  let body, sub;
  if (tab === 'truckers') {
    const rows = snap.truckers.filter(t => matches(q, t.name, t.email, t.zones.join(' '), t.airports.join(' ')));
    const cols = [
      { key: 'name', label: 'Trucker', render: t => [t.name, h('span', { class: 'sub' }, t.email)] }, { key: 'email', label: 'Email', hidden: true },
      { key: 'airports', label: 'Airports', sort: false, render: t => h('span', { class: 'num' }, t.airports.join(' ')), csv: t => t.airports.join(' ') },
      { key: 'zones', label: 'Postal zones', sort: false, render: t => h('span', { class: 'soft num' }, t.zones.join(' ')), cls: 'wrap', csv: t => t.zones.join(' ') },
      { key: 'asked', label: 'Requests', right: true }, { key: 'answered', label: 'Offers', right: true },
      { key: 'answer_rate_pct', label: 'Response rate %', right: true, render: t => (t.answer_rate_pct == null ? '–' : meter(t.answer_rate_pct, fmt.pct(t.answer_rate_pct))) },
      { key: 'cheapest_in', label: 'Lowest offer', right: true }, { key: 'chosen', label: 'Selected', right: true },
      { key: 'avg_price_eur', label: 'Avg. offer EUR', right: true, render: t => fmt.eur(t.avg_price_eur) },
      { key: 'avg_eur_per_100kg', label: '€ per 100 kg', right: true, render: t => fmt.two(t.avg_eur_per_100kg) },
      { key: 'median_pickup_hours', label: 'Pickup within h', right: true, render: t => fmt.one(t.median_pickup_hours) },
      live ? { key: 'median_response_s', label: 'Response time', right: true, render: t => fmt.duration(t.median_response_s) } : null,
    ].filter(Boolean);
    sub = `${snap.truckers.length} truckers`;
    body = h('section', { class: 'card glass c12 flush' }, head('Truckers', `${rows.filter(t => t.asked).length} contacted`, exportButton('talos-truckers.csv', rows, cols)),
      table(rows, cols.filter(c => !c.hidden), { state: sortState(ctx, 'truckers', 'asked') }));
  } else if (tab === 'zones') {
    const rows = snap.zones.filter(z => matches(q, z.zone, z.area, z.airport, z.truckers.join(' ')));
    const cols = [
      { key: 'zone', label: 'Zone', render: z => h('span', { class: 'num' }, z.zone) }, { key: 'area', label: 'Area' }, { key: 'airport', label: 'Airport' },
      { key: 'requests', label: 'Requests', right: true },
      { key: 'truckers', label: 'Truckers', get: z => z.truckers.length, render: z => h('span', { class: 'soft' }, z.truckers.join(', ')), cls: 'wrap', csv: z => z.truckers.join('; ') },
    ];
    sub = `${snap.zones.length} pickup zones`;
    body = h('section', { class: 'card glass c12 flush' }, head('Pickup zones', 'postal code prefix determines airport and truckers', exportButton('talos-zones.csv', rows, cols)),
      table(rows, cols, { state: sortState(ctx, 'zones', 'zone', 'asc') }));
  } else if (tab === 'tariffs') {
    ctx.cache.tariffs ||= await ctx.api('/crm/api/tariffs', false);
    const all = ctx.cache.tariffs, f = ui.tariffs;
    const options = key => ['', ...new Set(all.map(t => t[key]))].sort();
    const select = (key, label) => h('select', { 'aria-label': label, onchange: e => { f[key] = e.target.value; ctx.rerender(); } },
      options(key).map(v => h('option', { value: v, selected: f[key] === v || null }, v || `${label}: all`)));
    const rows = all.filter(t => (!f.origin || t.origin === f.origin) && (!f.destination || t.destination === f.destination) && (!f.airline || t.airline === f.airline)
      && matches(q, t.airline, t.origin, t.destination));
    const rates = t => t.breaks_kg.map((b, i) => `${fmt.int(b)}: ${fmt.two(t.rates_per_kg[i])}`).join('  ·  ');
    const cols = [
      { key: 'airline', label: 'Airline' }, { key: 'origin', label: 'Lane', get: t => `${t.origin}-${t.destination}`, render: t => `${t.origin} → ${t.destination}` },
      { key: 'transit_days', label: 'Transit days', right: true, render: t => fmt.int(t.transit_days) },
      { key: 'minimum_eur', label: 'Minimum EUR', right: true, render: t => fmt.eur(t.minimum_eur) },
      { key: 'rates', label: 'Rate by weight break (kg: €/kg)', sort: false, cls: 'wrap', render: t => h('span', { class: 'num soft' }, rates(t)), csv: rates },
      { key: 'fuel_per_kg', label: 'Fuel €/kg', right: true, render: t => fmt.two(t.fuel_per_kg) },
      { key: 'security_per_kg', label: 'Security €/kg', right: true, render: t => fmt.two(t.security_per_kg) },
      { key: 'valid_to', label: 'Valid until', render: t => fmt.day(t.valid_to), csv: t => t.valid_to }, { key: 'source_file', label: 'Source', render: t => h('span', { class: 'muted' }, t.source_file) },
    ];
    sub = `${all.length} tariff lanes`;
    body = [h('div', { class: 'filters' }, select('origin', 'Origin'), select('destination', 'Destination'), select('airline', 'Airline')),
      h('section', { class: 'card glass c12 flush' }, head('Airline tariffs', `${rows.length} of ${all.length} lanes · per kg of chargeable weight`, exportButton('talos-tariffs.csv', rows, cols)),
        table(rows, cols, { state: sortState(ctx, 'tariffs', 'airline', 'asc'), empty: 'No tariff lane matches.' }))];
  } else {
    const rows = snap.airlines.filter(a => matches(q, a.name, a.code, a.serves.join(' ')));
    const cols = [
      { key: 'name', label: 'Airline', render: a => [a.name, h('span', { class: 'sub' }, [a.code, a.email].filter(Boolean).join(' · '))] }, { key: 'email', label: 'Email', hidden: true },
      { key: 'source', label: 'Rate source', render: a => (a.source === 'tariff' ? 'tariff file' : a.source === 'portal' ? 'portal' : h('span', { class: 'muted' }, 'not yet priced')) },
      { key: 'compared', label: 'Priced', right: true }, { key: 'cheapest_in', label: 'Lowest price', right: true }, { key: 'fastest_in', label: 'Fastest', right: true },
      { key: 'chosen', label: 'Quoted', right: true }, { key: 'accepted', label: 'Accepted', right: true },
      { key: 'avg_eur_per_kg', label: 'Avg. €/kg', right: true, render: a => fmt.two(a.avg_eur_per_kg) },
      { key: 'avg_transit_days', label: 'Avg. transit days', right: true, render: a => fmt.one(a.avg_transit_days) },
      { key: 'serves', label: 'Destinations', get: a => a.serves.length, render: a => h('span', { class: 'soft num' }, a.serves.join(' ')), cls: 'wrap', csv: a => a.serves.join(' ') },
    ];
    sub = `${snap.airlines.length} airlines`;
    body = h('section', { class: 'card glass c12 flush' }, head('Airlines', `${snap.airlines.filter(a => a.compared).length} of ${snap.airlines.length} priced`, exportButton('talos-airlines.csv', rows, cols)),
      table(rows, cols.filter(c => !c.hidden), { state: sortState(ctx, 'airlines', 'compared') }));
  }
  return { title: 'Carriers', sub, node: h('div', { class: 'grid' }, tabs, body) };
}

// --- inbox --------------------------------------------------------------------------------

function mailRows(items, open) {
  if (!items.length) return h('div', { class: 'empty' }, 'No mail matches.');
  return h('div', null, items.map(m => {
    const body = h('div', { class: 'body', hidden: !open.has(m.id) || null }, h('pre', null, m.body),
      m.reasoning ? h('div', { class: 'why' }, `Classification note: ${m.reasoning}`) : null);
    const line = h('div', { class: 'line', tabindex: 0, onclick: () => { if (open.has(m.id)) open.delete(m.id); else open.add(m.id); body.hidden = !open.has(m.id); } },
      h('span', { class: 'muted num' }, `${fmt.day(m.at)} ${fmt.time(m.at)}`),
      h('span', null, m.who, h('span', { class: 'muted' }, ` · ${ROLES[m.role] || m.role}`)),
      h('span', { class: 'soft' }, m.subject), h('span', null, category(m.category)),
      h('span', { class: `num ${m.confidence < 0.7 ? '' : 'muted'}`, title: 'Classifier confidence' }, `${fmt.int(100 * m.confidence)}%`),
      m.shipment_id ? h('a', { href: `#/shipment/${m.shipment_id}`, onclick: e => e.stopPropagation() }, `#${m.shipment_id}`) : h('span', { class: 'muted' }, '–'));
    return h('div', { class: 'mail' }, line, body);
  }));
}

export async function inbox(ctx) {
  const { snap, ui, q } = ctx, f = ui.inbox;
  const params = new URLSearchParams({ limit: f.limit, q: q || '', category: f.category, role: f.role });
  const data = await ctx.api(`/crm/api/mails?${params}`);
  const select = (key, label, options) => h('select', { 'aria-label': label, onchange: e => { f[key] = e.target.value; f.limit = 100; ctx.rerender(); } },
    h('option', { value: '' }, `${label}: all`), options.map(([value, text]) => h('option', { value, selected: f[key] === value || null }, text)));
  const node = h('div', { class: 'grid' },
    h('div', { class: 'filters' },
      select('category', 'Category', snap.mail.by_category.map(c => [c.category, `${category(c.category)} (${c.count})`])),
      select('role', 'Sender', snap.mail.by_role.map(r => [r.role, `${ROLES[r.role] || r.role} (${r.count})`])),
      h('span', { class: 'muted' }, 'Search covers the full mail text.')),
    h('section', { class: 'card glass c12 flush' }, head('Inbound mail', `${data.items.length} of ${data.total} · newest first`),
      mailRows(data.items, ui.openMails),
      data.items.length < data.total ? h('div', { class: 'empty' }, h('button', { class: 'pill', onclick: () => { f.limit += 100; ctx.rerender(); } }, 'Show 100 more')) : null));
  return { title: 'Inbox', sub: `${fmt.int(snap.mail.total)} mails · ${snap.mail.low_confidence} with low classification confidence`, node };
}

// --- analysis -----------------------------------------------------------------------------

export function analysis(ctx) {
  const { snap, q } = ctx;
  const metrics = [
    { key: 'title', label: 'Metric', render: f => h('b', { style: { fontWeight: 600 } }, f.title) },
    { key: 'value', label: 'Value', right: true, sort: false, render: f => fmt.figure(f.value, f.unit, f.n), csv: f => f.value },
    { key: 'unit', label: 'Unit', hidden: true }, { key: 'n', label: 'Cases', right: true },
    { key: 'text', label: 'Definition', sort: false, cls: 'desc' },
  ];
  const quoted = snap.deals.filter(d => d.all_in_eur != null && matches(q, d.id, d.company, d.lane, d.airline, d.goal));
  const share = d => (d.truck_eur ? 100 * d.truck_eur / d.all_in_eur : null);
  const perShipment = [
    { key: 'id', label: 'No.', render: d => h('span', { class: 'num soft' }, d.id) },
    { key: 'company', label: 'Account', cls: 'clip' }, { key: 'lane', label: 'Lane', render: laneText, csv: d => d.lane },
    { key: 'goal', label: 'Priority', render: d => d.goal || '–' },
    { key: 'all_in_eur', label: 'All-in EUR', right: true, render: d => fmt.eurExact(d.all_in_eur) },
    { key: 'options', label: 'Air options', hidden: true, get: d => d.air?.n ?? null },
    { key: 'rank', label: 'Price rank', right: true, get: d => d.air?.chosen_rank ?? null, render: d => (d.air?.chosen_rank ? `${d.air.chosen_rank} of ${d.air.n}` : '–') },
    { key: 'premium', label: 'Over lowest EUR', right: true, get: d => d.air?.premium_eur ?? null,
      render: d => (d.air?.premium_eur == null ? '–' : d.air.premium_eur === 0 ? '–' : [`+${fmt.eur(d.air.premium_eur)}`, h('span', { class: 'sub' }, fmt.pct(d.air.premium_pct))]) },
    { key: 'premium_pct', label: 'Over lowest %', hidden: true, get: d => d.air?.premium_pct ?? null },
    { key: 'speed', label: 'Faster transit EUR', right: true, get: d => (d.air?.speed_days > 0 ? d.air.speed_eur : null),
      render: d => (d.air?.speed_days > 0 ? [`+${fmt.eur(d.air.speed_eur)}`, h('span', { class: 'sub' }, `${fmt.days(d.air.speed_days)} less`)] : '–') },
    { key: 'speed_days', label: 'Faster transit days', hidden: true, get: d => d.air?.speed_days ?? null },
    { key: 'spread', label: 'Air spread %', right: true, get: d => d.air?.spread_pct ?? null, render: d => fmt.pct(d.air?.spread_pct) },
    { key: 'truck_mode', label: 'Trucking', render: d => ({ quoted: 'offer', estimate: 'estimate', none: 'not required' }[d.truck_mode] || '–') },
    { key: 'truck_share', label: 'Trucking share %', right: true, get: share, render: d => fmt.pct(share(d)) },
    { key: 'truck_spread', label: 'Trucking spread %', right: true, get: d => d.truck?.spread_pct ?? null, render: d => fmt.pct(d.truck?.spread_pct) },
    snap.source === 'live' ? { key: 'minutes_to_quote', label: 'Time to quote min', right: true, render: d => fmt.one(d.minutes_to_quote) } : null,
  ].filter(Boolean);
  const roles = snap.mail.by_role.map(r => ({ name: ROLES[r.role] || r.role, value: r.count }));
  const node = h('div', { class: 'grid' },
    snap.limits.length ? h('div', { class: 'notice glass c12' }, icon('info'), h('div', null, h('b', null, 'Recorded dry run. '), snap.limits.join(' '))) : null,
    h('section', { class: 'card glass c12 flush' }, head('Pricing and demand metrics', 'medians over priced shipments', exportButton('talos-metrics.csv', snap.findings, metrics)),
      table(snap.findings, metrics.filter(c => !c.hidden), { empty: 'No priced shipments to analyse.' })),
    h('section', { class: 'card glass c12 flush' }, head('Quote comparison by shipment', `${quoted.length} quotes`, exportButton('talos-quote-comparison.csv', quoted, perShipment)),
      table(quoted, perShipment.filter(c => !c.hidden), { state: sortState(ctx, 'analysis', 'id'), onRow: d => go(`#/shipment/${d.id}`), empty: 'No quote matches.' })),
    card('c7', 'Inbound mail volume', snap.mail.total ? `${fmt.int(snap.mail.total)} mails · per 10 minutes, UTC` : null,
      snap.mail.per_10_minutes.length ? columns(snap.mail.per_10_minutes) : h('div', { class: 'empty' }, 'No mail on record.')),
    card('c5', 'Inbound mail by sender', null, roles.length ? barList(roles, { label: 120, solid: true }) : h('div', { class: 'empty' }, 'No mail on record.')));
  return { title: 'Analysis', sub: `${snap.findings.length} metrics · ${quoted.length} quotes`, node };
}

// --- system -------------------------------------------------------------------------------

export async function system(ctx) {
  const { snap } = ctx, sys = await ctx.api('/crm/api/system', false);
  const modes = { test: ['Test mode', 'Nothing is sent or booked. LLM calls use the evaluation key; shipments are written to the test database.'],
    dry_run: ['Drafts only', 'Mails are composed and stored. Nothing is sent and nothing is booked.'],
    sending: ['Sending and booking', 'Quotes are sent to customers and accepted quotes are booked.'] };
  const [modeName, modeText] = modes[sys.mode];
  const p = sys.poller, c = snap.mail.classification;
  const kv = rows => h('div', { class: 'tablewrap' }, h('table', null, h('tbody', null, rows.map(([k, v]) => h('tr', null, h('td', { class: 'soft' }, k), h('td', { class: 'free' }, v))))));
  const node = h('div', { class: 'grid' },
    card('c4', 'Operating mode', null, h('div', { style: { display: 'grid', gap: '8px' } },
      h('div', { class: 'hero', style: { fontSize: '24px' } }, modeName), h('p', { class: 'soft' }, modeText))),
    card('c4', 'Mailbox poller', null, h('div', { class: 'facts' },
      fact('Status', p.running ? 'running' : p.enabled ? 'enabled, not running' : 'disabled'), fact('Interval', `${p.interval_seconds} s`),
      fact('Last poll', p.last_poll_at ? `${fmt.when(p.last_poll_at)} UTC` : 'none in this process'), fact('Last mail id read', fmt.int(p.cursor)),
      p.last_error ? fact('Last error', p.last_error) : null)),
    card('c4', 'Outbound mail', snap.source === 'replay' ? 'dry run' : 'live', snap.outbound.total
      ? h('div', { style: { display: 'grid', gap: '12px' } },
          barList(snap.outbound.by_kind.map(k => ({ name: KINDS[k.kind] || words(k.kind), value: k.count })), { label: 150, solid: true }),
          h('p', { class: 'muted', style: { fontSize: '12.5px' } }, `${snap.outbound.sent} of ${snap.outbound.total} sent · ${snap.outbound.templates_used} template fallbacks`))
      : h('div', { class: 'empty' }, 'No mail composed.')),
    card('c6', 'Inbound mail by category', `${fmt.int(snap.mail.total)} mails`,
      snap.mail.total ? barList(snap.mail.by_category.map(k => ({ name: category(k.category), value: k.count })), { label: 170, solid: true }) : h('div', { class: 'empty' }, 'No mail on record.')),
    c ? card('c6', 'Classifier accuracy', 'against hand labels, dry run',
          h('div', { style: { display: 'grid', gap: '10px' } }, h('div', { class: 'hero', style: { fontSize: '24px' } }, `${c.correct} of ${c.labelled}`, h('small', null, 'classified as labelled')),
            c.wrong.length ? table(c.wrong, [{ key: 'id', label: 'Mail' }, { key: 'from', label: 'Sender' }, { key: 'category', label: 'Classified as', render: r => category(r.category) }, { key: 'expected', label: 'Label', render: r => category(r.expected) }])
              : h('p', { class: 'soft' }, 'No deviations.')))
      : card('c6', 'Classifier accuracy', null, h('div', { class: 'empty' }, 'Hand labels exist for the recorded dry run only.')),
    h('section', { class: 'card glass c6 flush' }, head('Configuration', 'credentials are not displayed'), kv(sys.settings.map(s => [s.name, s.value]))),
    h('section', { class: 'card glass c6 flush' }, head('Data inventory'), kv(sys.inventory.map(i => [i.name, [h('span', { class: 'num' }, fmt.int(i.rows)), h('span', { class: 'sub' }, i.where)]]))),
    h('section', { class: 'card glass c12 flush' }, head('Prompts', 'local files; the running router reads Langfuse first'),
      sys.prompts.map(pr => h('details', { class: 'prompt' }, h('summary', null, pr.name), h('pre', null, pr.text)))));
  return { title: 'System', sub: modeName, node };
}
