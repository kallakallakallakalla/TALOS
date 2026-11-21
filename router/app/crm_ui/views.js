// The pages. Each takes the context (snapshot, UI state, helpers) and returns
// {title, sub, node}; pages that need more than the snapshot fetch it themselves.

import { createGlobe, REGIONS, routeView } from './globe.js';
import { barList, card, category, columns, fmt, h, icon, KINDS, meter, ROLES, splitBar, stateBadge, STATES, table, tip, tipRows, words } from './lib.js';

const go = hash => { location.hash = hash; };
const matches = (q, ...fields) => !q || fields.some(f => String(f ?? '').toLowerCase().includes(q));
const tile = (label, value, foot, unit) => h('div', { class: 'tile glass' },
  h('div', { class: 'label' }, label), h('div', { class: 'value' }, value, unit && h('small', null, unit)), foot && h('div', { class: 'foot' }, foot));
const codes = list => h('span', { class: 'codes' }, list.map(c => h('span', { class: 'tag' }, c)));
const laneText = d => `${d.origin || '?'} → ${d.destination || '?'}`;
const sortState = (ctx, name, key, dir = 'desc') => {
  const state = (ctx.ui.sort[name] ||= { key, dir });
  state.changed = ctx.rerender;
  return state;
};
const priceSplit = d => (d.all_in_eur == null ? null : splitBar([
  { name: 'Air freight', value: d.air_eur, text: fmt.eur(d.air_eur), cls: 's1' },
  { name: d.truck_mode === 'estimate' ? 'Pickup (estimate)' : 'Pickup', value: d.truck_eur, text: fmt.eur(d.truck_eur), cls: 's2' }]));

function limitsNotice(snap) {
  if (!snap.limits.length) return null;
  return h('div', { class: 'notice glass c12' }, icon('info'),
    h('div', null, h('b', null, 'This is the recorded dry run, not live traffic. '), h('ul', null, snap.limits.map(text => h('li', null, text)))));
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
  const toggle = compact ? null : [
    h('div', { class: 'seg', role: 'group', 'aria-label': 'Turn the globe to' }, Object.entries(REGIONS).map(([name, [lon, lat]]) => h('button', { onclick: () => globe.lookAt(lon, lat) }, name))),
    h('label', { class: 'chip', style: { cursor: 'pointer' } },
      h('input', { type: 'checkbox', checked: ui.coverage || null, onchange: e => { ui.coverage = e.target.checked; globe.setCoverage(ui.coverage); } }),
      'All served destinations')];
  const transit = routed.reduce((n, l) => n + l.in_transit.length, 0);
  return h('section', { class: `card glass ${span}` },
    h('header', null, h('h3', null, title),
      !compact && h('span', { class: 'note' }, `${routed.length} ${routed.length === 1 ? 'route' : 'routes'}${transit ? ` · ${transit} in transit` : ''}`),
      toggle && h('span', { class: 'right' }, toggle)),
    host,
    compact
      ? h('div', { class: 'muted', style: { marginTop: '8px', fontSize: '12.5px' } }, 'Great-circle path between the two airports. Drag to turn.')
      : h('div', { class: 'legend', style: { marginTop: '10px' } },
          h('span', null, h('i', { class: 'line' }), 'Accepted'), h('span', null, h('i', { class: 'line dash' }), 'Quoted or open'),
          h('span', null, h('i', { class: 'line off' }), 'Declined'), h('span', null, h('i', { class: 'ring' }), 'Departure airport'),
          h('span', { class: 'muted' }, 'Drag to turn. Line width: number of requests. Great-circle paths; a shipment in transit is placed by its planned dates.')));
}

function attentionList(ctx, items) {
  if (!items.length) return h('div', { class: 'empty' }, 'Nothing needs a person right now.');
  const glyph = { critical: 'alert', warning: 'alert', info: 'clock' };
  return h('div', { class: 'list' }, items.map(item => h('div', {
    class: `item ${item.level}${item.shipment_id ? ' link' : ''}`,
    onclick: item.shipment_id ? () => go(`#/shipment/${item.shipment_id}`) : null,
  }, icon(glyph[item.level]),
    h('div', null, h('b', null, item.title), h('p', null, item.detail)),
    h('div', { class: 'meta' }, item.company, item.lane && h('div', null, item.lane.replace('-', ' → '))))));
}

const findingsGrid = list => (list.length
  ? h('div', { class: 'findings' }, list.map(f => h('div', { class: 'finding' },
      h('div', { class: 'head' }, h('span', { class: 'fig' }, fmt.figure(f.value, f.unit)), h('span', { class: 'title' }, f.title)),
      h('p', null, f.text), h('div', { class: 'basis' }, `Based on ${f.n} ${f.n === 1 ? 'case' : 'cases'}`))))
  : h('div', { class: 'empty' }, 'No priced shipments yet, so there is nothing to compare.'));

function mailFeed(items) {
  if (!items.length) return h('div', { class: 'empty' }, 'No mail stored.');
  return h('div', { class: 'list' }, items.map(m => h('div', {
    class: `item info${m.shipment_id ? ' link' : ''}`, onclick: m.shipment_id ? () => go(`#/shipment/${m.shipment_id}`) : null,
  }, icon('inbox'),
    h('div', null, h('b', null, m.who), h('p', null, m.subject)),
    h('div', { class: 'meta' }, category(m.category), h('div', null, `${fmt.time(m.at)} UTC`)))));
}

// --- desk ---------------------------------------------------------------------------------

export async function desk(ctx) {
  const { snap } = ctx, k = snap.kpis, live = snap.source === 'live';
  const urgent = snap.attention.filter(a => a.level !== 'info').length;
  const feed = await ctx.api('/crm/api/mails?limit=7');
  const stages = snap.pipeline.filter(p => p.count).map(p => ({
    name: STATES[p.state][0], value: p.count, text: fmt.int(p.count), note: p.eur ? fmt.eur(p.eur) : null,
    href: '#/pipeline', tip: () => tipRows(STATES[p.state][0], [['Shipments', fmt.int(p.count)], ['Quoted value', fmt.eur(p.eur)]]),
    state: p.state,
  }));
  const stageBars = barList(stages, { label: 150, solid: true });
  [...stageBars.children].forEach((row, i) => row.addEventListener('click', () => { ctx.ui.pipeline.state = stages[i].state; ctx.ui.pipeline.lane = null; }, true));
  const roles = snap.mail.by_role.map(r => ({ name: ROLES[r.role] || r.role, value: r.count }));
  const emptyLive = live && !snap.deals.length ? h('div', { class: 'notice glass c12' }, icon('info'), h('div', null,
    `The live database holds no priced shipment yet${snap.legacy.length ? `: its ${snap.legacy.length} requests were answered by the previous process, before this router took over` : ''}. `,
    ctx.sources.replay.available ? [`The recorded dry run has ${ctx.sources.replay.shipments} priced shipments. `, h('button', { class: 'linkbtn', onclick: () => ctx.pick('replay') }, 'Open the dry run')] : null)) : null;
  const node = h('div', { class: 'grid' },
    limitsNotice(snap), emptyLive,
    h('div', { class: 'tiles' },
      tile('Requests', fmt.int(k.requests), `${k.open} open, ${k.failed} failed`),
      tile('Quoted', fmt.int(k.quoted), `${fmt.eur(k.quoted_eur)} in total`),
      tile('Accepted', fmt.int(k.accepted), k.win_rate_pct != null ? `${fmt.eur(k.accepted_eur)} · ${fmt.pct(k.win_rate_pct)} of decided` : `${fmt.eur(k.accepted_eur)} · ${k.rejected} declined`),
      tile('Awaiting decision', fmt.int(k.awaiting_decision), fmt.eur(k.awaiting_decision_eur)),
      tile('Median all-in price', fmt.eur(k.median_all_in_eur), 'per quote'),
      live
        ? tile('Median time to quote', k.median_minutes_to_quote == null ? '–' : fmt.one(k.median_minutes_to_quote), k.quotes_timed ? `${k.quotes_in_window} of ${k.quotes_timed} within 6 min` : 'no quote sent yet', k.median_minutes_to_quote == null ? null : 'min')
        : tile('Needs attention', fmt.int(urgent), urgent ? 'see the list' : 'nothing open')),
    globeCard(ctx, 'c7', { height: 440 }),
    h('div', { class: 'stack fill c5' },
      card('', 'Where shipments stand', null, stages.length ? stageBars : h('div', { class: 'empty' }, 'No shipments yet.')),
      h('section', { class: 'card glass flush' }, h('header', null, h('h3', null, 'Needs attention'), h('span', { class: 'note' }, `${snap.attention.length} ${snap.attention.length === 1 ? 'item' : 'items'}`)),
        attentionList(ctx, snap.attention))),
    card('c12', 'What the prices say', 'computed from every priced shipment', findingsGrid(snap.findings)),
    card('c7', 'Incoming mail', snap.mail.total ? `${fmt.int(snap.mail.total)} mails · per 10 minutes, UTC` : null,
      snap.mail.per_10_minutes.length ? [columns(snap.mail.per_10_minutes),
        h('div', { style: { marginTop: '16px' } }, h('div', { class: 'muted', style: { fontSize: '12.5px', marginBottom: '8px' } }, 'Who wrote'), barList(roles, { label: 110, solid: true }))]
        : h('div', { class: 'empty' }, 'No mail stored.')),
    h('section', { class: 'card glass c5 flush' }, h('header', null, h('h3', null, 'Latest mail'),
      h('span', { class: 'right' }, h('a', { href: '#/inbox', class: 'note' }, 'Open the inbox'))), mailFeed(feed.items)));
  return { title: 'Desk', sub: `${fmt.int(k.requests)} requests · ${fmt.int(k.quoted)} quoted · ${fmt.int(k.accepted)} accepted`, node };
}

// --- pipeline -----------------------------------------------------------------------------

export function pipeline(ctx) {
  const { snap, ui, q } = ctx, f = ui.pipeline;
  const rows = snap.deals.filter(d => (!f.state || d.state === f.state) && (!f.lane || d.lane === f.lane || `?-${d.destination}` === f.lane)
    && (!f.account || d.account === f.account)
    && matches(q, d.id, d.company, d.contact, d.lane, d.subject, d.airline, d.commodity, STATES[d.state]?.[0], d.pickup_city));
  const pill = (label, count, on, pick) => h('button', { class: `pill${on ? ' on' : ''}`, onclick: () => { pick(); ctx.rerender(); } }, label, count != null && h('b', null, count));
  const filters = h('div', { class: 'filters' },
    pill('All', snap.deals.length, !f.state, () => { f.state = null; }),
    snap.pipeline.filter(p => p.count).map(p => pill(STATES[p.state][0], p.count, f.state === p.state, () => { f.state = f.state === p.state ? null : p.state; })),
    f.lane && pill(`Lane ${f.lane.replace('-', ' → ')} ×`, null, true, () => { f.lane = null; }),
    f.account && pill(`${snap.accounts.find(a => a.email === f.account)?.company || f.account} ×`, null, true, () => { f.account = null; }));
  const cols = [
    { key: 'id', label: 'No.', render: d => h('span', { class: 'num soft' }, d.id) },
    { key: 'company', label: 'Account', cls: 'clip', render: d => [d.company, h('span', { class: 'sub' }, d.contact || d.account)] },
    { key: 'lane', label: 'Route', get: d => d.lane || `~${d.destination}`, render: d => [laneText(d),
      h('span', { class: 'sub' }, d.pickup ? `pickup ${d.pickup_city || d.pickup_postal_code || ''}`.trim() : 'delivered to the airport', d.distance_km ? ` · ${fmt.km(d.distance_km)}` : '')] },
    { key: 'gross_kg', label: 'Cargo', right: true, render: d => [fmt.kg(d.gross_kg), h('span', { class: 'sub', title: d.volumetric ? 'The volume weight is above the gross weight, so the airline bills the volume' : null }, `${fmt.cbm(d.volume_cbm)}${d.volumetric ? ' · by volume' : ''}`)] },
    { key: 'goal', label: 'Asked for', render: d => d.goal || h('span', { class: 'muted' }, 'not stated') },
    { key: 'all_in_eur', label: 'All-in', right: true, render: d => (d.all_in_eur == null ? '–' : fmt.eurExact(d.all_in_eur)) },
    { key: 'truck_eur', label: 'Air / pickup', sort: false, render: d => priceSplit(d) || h('span', { class: 'muted' }, '–') },
    { key: 'airline', label: 'Airline', cls: 'clip', render: d => (d.airline ? [d.airline, h('span', { class: 'sub' }, `${fmt.days(d.transit_days)}${d.truck_mode === 'estimate' ? ' · pickup estimated' : ''}`)] : '–') },
    { key: 'state', label: 'Status', get: d => Object.keys(STATES).indexOf(d.state), render: stateBadge },
    { key: 'ready_date', label: 'Ready', render: d => fmt.day(d.ready_date) },
  ];
  const node = h('div', { class: 'grid' }, filters,
    h('section', { class: 'card glass c12 flush' },
      h('header', null, h('h3', null, 'Shipments'), h('span', { class: 'note' }, `${rows.length} of ${snap.deals.length}`),
        h('span', { class: 'right legend' }, h('span', null, h('i', { class: 's1' }), 'Air freight'), h('span', null, h('i', { class: 's2' }), 'Pickup'))),
      table(rows, cols, { state: sortState(ctx, 'pipeline', 'id'), onRow: d => go(`#/shipment/${d.id}`), empty: 'No shipment matches these filters.' })),
    snap.legacy.length ? h('div', { class: 'notice glass c12' }, icon('info'),
      h('div', null, `${snap.legacy.length} older requests were answered by the previous process before cutover. They have no stored prices, so they are not listed here; their mails are in the inbox.`)) : null);
  return { title: 'Pipeline', sub: `${snap.deals.length} shipments · ${fmt.eur(snap.kpis.quoted_eur)} quoted`, node };
}

// --- one shipment -------------------------------------------------------------------------

const STEPS = ['Request', 'Details complete', 'Priced', 'Quoted', 'Decided', 'Booked'];
const STEP_OF = { NEW: 0, AWAITING_INFO: 0, PRICING: 1, QUOTED: 3, BOOKING: 4, BOOKED: 5, CONFIRMED: 5, REJECTED: 4, FAILED: -1 };
const fact = (label, value) => h('div', null, h('span', null, label), h('b', null, value ?? '–'));

function thread(detail) {
  if (!detail.timeline.length) return h('div', { class: 'empty' }, 'No mail stored for this shipment.');
  return h('div', { class: 'thread' }, detail.timeline.map(m => {
    const out = m.direction === 'out';
    const long = m.body.length > 320 || m.body.split('\n').length > 7;
    const body = h('pre', { class: long ? 'short' : '' }, m.body);
    const more = long ? h('button', { class: 'more', onclick: e => { const open = body.classList.toggle('short'); e.target.textContent = open ? 'Show all' : 'Show less'; } }, 'Show all') : null;
    return h('div', { class: `msg ${out ? 'out' : 'in'}` },
      h('div', { class: 'who' }, h('b', null, out ? 'TALOS' : m.who),
        h('span', null, out ? `${KINDS[m.kind] || words(m.kind)} to ${m.who}` : category(m.category)),
        m.at && h('span', null, `${fmt.when(m.at)} UTC`),
        out && !m.sent && h('span', { class: 'tag' }, detail.source === 'replay' ? 'simulated' : 'draft, not sent'),
        out && m.used_template && h('span', { class: 'tag' }, 'template'),
        !out && m.confidence < 0.7 && h('span', { class: 'tag' }, `unsure, ${fmt.int(100 * m.confidence)}%`)),
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
    fact('From', d.pickup ? `${d.pickup_postal_code || ''} ${d.pickup_city || ''}`.trim() || '–' : `delivered to ${d.origin || 'the airport'}`),
    fact('Departure airport', d.origin ? `${d.origin}${d.zone ? ` (zone ${d.zone})` : ''}` : '–'),
    fact('Destination', d.destination ? `${d.destination} · ${ctx.snap.airports[d.destination]?.city || ''}` : '–'),
    fact('Ready', fmt.day(d.ready_date)), fact('Pieces', fmt.int(d.pieces)), fact('Gross weight', fmt.kg(d.gross_kg)),
    fact('Volume', fmt.cbm(d.volume_cbm)), fact('Chargeable weight', `${fmt.kg(d.chargeable_kg)}${d.volumetric ? ' (volume)' : ''}`),
    fact('Commodity', d.commodity), fact('Asked for', d.goal || 'not stated, priced as cheapest'), fact('Contact', d.contact),
    d.extra_questions.length ? fact('Also asked', d.extra_questions.join('; ')) : null,
    d.assumptions.length ? fact('Assumed', d.assumptions.join('; ')) : null,
    d.missing.length ? fact('Still missing', d.missing.join('; ')) : null));

  const offer = d.all_in_eur == null ? null : card('', 'Offer', d.valid_until ? `valid until ${d.valid_until}` : null,
    h('div', { style: { display: 'grid', gap: '14px' } },
      h('div', { class: 'hero' }, fmt.eurExact(d.all_in_eur), h('small', null, 'all-in')),
      h('div', { style: { display: 'grid', gap: '8px', maxWidth: '520px' } }, priceSplit(d),
        h('div', { class: 'legend' }, h('span', null, h('i', { class: 's1' }), `Air freight ${fmt.eurExact(d.air_eur)}`),
          d.truck_eur ? h('span', null, h('i', { class: 's2' }), `Pickup ${fmt.eurExact(d.truck_eur)}${d.truck_mode === 'estimate' ? ' (estimate, no trucker answered)' : ''}`) : h('span', { class: 'muted' }, 'No pickup: the customer delivers to the airport'))),
      h('div', { class: 'facts' },
        fact('Airline', d.airline), fact('Transit', fmt.days(d.transit_days)), fact('Rate source', d.air_source === 'tariff' ? 'tariff file' : 'portal'),
        fact('Billed weight', `${fmt.kg(d.billed_kg)}${d.weight_break ? ' (next weight break)' : ''}`), fact('Air rate', d.air_eur_per_kg ? `${fmt.two(d.air_eur_per_kg)} €/kg` : '–'),
        fact('Pickup', fmt.day(d.estimated_pickup)), fact('Departure', fmt.day(d.estimated_departure)), fact('Arrival', fmt.day(d.estimated_arrival)),
        d.booking_ref ? fact('Booking reference', d.dry_run_booking ? 'none, dry run' : d.booking_ref) : null)));

  const top = Math.max(...detail.options.map(o => o.total_eur), 0);
  const flights = detail.options.length ? card('', 'Flights compared', `${detail.options.length} priced · the chosen one is highlighted`,
    barList(detail.options.map(o => ({
      name: o.airline, value: o.total_eur, em: o.chosen, text: fmt.eurExact(o.total_eur),
      note: `${fmt.days(o.transit_days)}${o.cheapest ? ' · cheapest' : ''}${o.fastest ? ' · fastest' : ''}`,
      tip: () => tipRows(o.airline, [['Total', fmt.eurExact(o.total_eur)], ['Transit', fmt.days(o.transit_days)], ['Billed weight', fmt.kg(o.billed_kg)],
        ['Rate', `${fmt.two(o.eur_per_kg)} €/kg`], ['Source', o.source === 'tariff' ? 'tariff file' : 'portal'], o.chosen && ['Chosen', 'yes']]),
    })), { label: 190, max: top })) : null;

  const trucks = detail.truckers.length ? card('', 'Pickup offers', `${detail.truckers.filter(t => t.price_eur != null).length} of ${detail.truckers.length} truckers answered`,
    barList(detail.truckers.map(t => ({
      name: t.name, value: t.price_eur || 0, em: t.chosen, text: t.price_eur == null ? 'no answer' : fmt.eurExact(t.price_eur),
      note: t.price_eur == null ? null : `${t.pickup_hours != null ? `pickup in ${fmt.one(t.pickup_hours)} h` : ''}${t.cheapest ? ' · cheapest' : ''}`,
      tip: () => tipRows(t.name, [['Offer', t.price_eur == null ? 'no answer' : fmt.eurExact(t.price_eur)], t.pickup_hours != null && ['Pickup within', `${fmt.one(t.pickup_hours)} h`],
        t.ref && ['Reference', t.ref], t.response_s != null && ['Answered after', fmt.duration(t.response_s)], t.chosen && ['Chosen', 'yes']]),
    })), { label: 190 })) : null;

  const a = d.air, readings = [];
  if (a) {
    if (a.premium_eur > 0) readings.push(['Chosen flight over the cheapest', `${fmt.eur(a.premium_eur)} (+${fmt.pct(a.premium_pct)})`]);
    else if (a.premium_eur === 0) readings.push(['Chosen flight', 'the cheapest of those priced']);
    if (a.speed_days > 0) readings.push([`Arriving ${fmt.days(a.speed_days)} sooner would cost`, fmt.eur(a.speed_eur)]);
    if (a.spread_pct != null) readings.push(['Dearest flight over the cheapest', `+${fmt.pct(a.spread_pct)}`]);
  }
  if (d.truck?.spread_pct != null) readings.push(['Dearest trucker over the cheapest', `+${fmt.pct(d.truck.spread_pct)}`]);
  if (d.truck_eur && d.all_in_eur) readings.push(['Pickup share of the price', fmt.pct(100 * d.truck_eur / d.all_in_eur)]);
  if (d.distance_km && d.all_in_eur && d.chargeable_kg) readings.push(['All-in per chargeable kg', `${fmt.two(d.all_in_eur / d.chargeable_kg)} €`]);
  if (d.minutes_to_quote != null) readings.push(['Request to quote', `${fmt.one(d.minutes_to_quote)} min`]);

  const lane = ctx.snap.lanes.filter(l => l.shipments.includes(d.id));
  const here = ctx.snap.airports[d.destination], from = ctx.snap.airports[d.origin];
  ctx.focusView = here && from ? routeView(from, here) : { lon: 12, lat: 30, zoom: 1 };
  const side = h('div', { class: 'stack c4' },
    lane.some(l => l.origin) ? globeCard(ctx, '', { height: 260, title: `${laneText(d)}${d.distance_km ? ` · ${fmt.km(d.distance_km)}` : ''}`, lanes: lane, compact: true }) : null,
    readings.length ? card('', 'What the comparison says', null, h('div', { class: 'facts', style: { gridTemplateColumns: '1fr' } }, readings.map(([k, v]) => fact(k, v)))) : null,
    account ? h('section', { class: 'card glass', style: { cursor: 'pointer' }, onclick: () => go(`#/account/${encodeURIComponent(account.email)}`) },
      h('header', null, h('h3', null, account.company), h('span', { class: 'note' }, 'account')),
      h('div', { class: 'facts' }, fact('Requests', fmt.int(account.requests)), fact('Accepted', fmt.int(account.accepted)), fact('Quoted value', fmt.eur(account.quoted_eur)), fact('Mails', fmt.int(account.mails_in)))) : null,
    d.error ? card('', 'Error', null, h('p', { class: 'soft' }, d.error)) : null);

  const node = h('div', { class: 'grid' },
    h('div', { class: 'crumbs' }, h('a', { href: '#/pipeline' }, 'Pipeline'), '/', h('span', null, `Shipment ${d.id}`)),
    h('div', { class: 'title' }, h('h2', null, d.company), stateBadge(d), h('span', { class: 'muted' }, laneText(d))),
    h('section', { class: 'card glass c12' }, steps),
    h('div', { class: 'stack c8' }, request, offer, flights, trucks, card('', 'Conversation', `${detail.timeline.length} mails`, thread(detail))),
    side);
  return { title: `Shipment ${d.id}`, sub: `${d.company} · ${laneText(d)}`, node };
}

// --- accounts -----------------------------------------------------------------------------

export function accounts(ctx) {
  const { snap, q } = ctx;
  const rows = snap.accounts.filter(a => matches(q, a.company, a.email, a.contacts.join(' '), a.destinations.join(' ')));
  const top = Math.max(...snap.accounts.map(a => a.requests), 1);
  const cols = [
    { key: 'company', label: 'Account', cls: 'clip', render: a => [a.company, h('span', { class: 'sub' }, a.contacts.join(', ') || a.email)] },
    { key: 'requests', label: 'Requests', right: true, render: a => [meter(100 * a.requests / top, fmt.int(a.requests)), a.legacy_requests ? h('span', { class: 'sub' }, `+${a.legacy_requests} before cutover`) : null] },
    { key: 'quoted', label: 'Quoted', right: true }, { key: 'accepted', label: 'Accepted', right: true },
    { key: 'rejected', label: 'Declined', right: true }, { key: 'open', label: 'Open', right: true },
    { key: 'quoted_eur', label: 'Quoted value', right: true, render: a => fmt.eur(a.quoted_eur) },
    { key: 'accepted_eur', label: 'Accepted value', right: true, render: a => fmt.eur(a.accepted_eur) },
    { key: 'avg_gross_kg', label: 'Avg. weight', right: true, render: a => fmt.kg(a.avg_gross_kg) },
    { key: 'goals', label: 'Asks for', sort: false, render: a => Object.entries(a.goals).map(([g, n]) => `${n}× ${g}`).join(', ') || '–' },
    { key: 'destinations', label: 'Destinations', sort: false, render: a => codes(a.destinations) },
    { key: 'mails_in', label: 'Mails', right: true },
    { key: 'last_mail_at', label: 'Last mail', render: a => fmt.when(a.last_mail_at) },
  ];
  const node = h('div', { class: 'grid' }, h('section', { class: 'card glass c12 flush' },
    h('header', null, h('h3', null, 'Accounts'), h('span', { class: 'note' }, `${rows.length} customers that wrote to us`)),
    table(rows, cols, { state: sortState(ctx, 'accounts', 'requests'), onRow: a => go(`#/account/${encodeURIComponent(a.email)}`), empty: 'No account matches.' })));
  return { title: 'Accounts', sub: `${snap.accounts.length} customers`, node };
}

export async function account(ctx, email) {
  const { snap } = ctx, a = snap.accounts.find(x => x.email === email);
  if (!a) return { title: 'Account', sub: '', node: h('div', { class: 'grid' }, h('div', { class: 'empty glass c12' }, `No account ${email} in this data.`)) };
  const deals = snap.deals.filter(d => d.account === email);
  const mail = await ctx.api(`/crm/api/mails?limit=40&sender=${encodeURIComponent(email)}`);
  const cols = [
    { key: 'id', label: 'No.', render: d => h('span', { class: 'num soft' }, d.id) },
    { key: 'lane', label: 'Route', render: d => [laneText(d), h('span', { class: 'sub' }, d.pickup ? `pickup ${d.pickup_city || d.pickup_postal_code || ''}`.trim() : 'delivered to the airport')] },
    { key: 'gross_kg', label: 'Cargo', right: true, render: d => [fmt.kg(d.gross_kg), h('span', { class: 'sub' }, fmt.cbm(d.volume_cbm))] },
    { key: 'commodity', label: 'Goods', cls: 'clip', render: d => d.commodity || '–' },
    { key: 'goal', label: 'Asked for', render: d => d.goal || '–' },
    { key: 'all_in_eur', label: 'All-in', right: true, render: d => (d.all_in_eur == null ? '–' : fmt.eurExact(d.all_in_eur)) },
    { key: 'airline', label: 'Airline', render: d => d.airline || '–' }, { key: 'state', label: 'Status', render: stateBadge },
    { key: 'ready_date', label: 'Ready', render: d => fmt.day(d.ready_date) },
  ];
  const byDest = Object.entries(deals.reduce((m, d) => { if (d.destination) m[d.destination] = (m[d.destination] || 0) + 1; return m; }, {}))
    .sort((x, y) => y[1] - x[1]).map(([code, n]) => ({ name: `${code} · ${snap.airports[code]?.city || ''}`, value: n }));
  const node = h('div', { class: 'grid' },
    h('div', { class: 'crumbs' }, h('a', { href: '#/accounts' }, 'Accounts'), '/', h('span', null, a.company)),
    h('div', { class: 'title' }, h('h2', null, a.company), h('span', { class: 'muted' }, a.email), a.contacts.length ? h('span', { class: 'chip' }, `Contact: ${a.contacts.join(', ')}`) : null,
      a.languages.length ? h('span', { class: 'chip' }, `Writes in: ${a.languages.join(', ')}`) : null),
    h('div', { class: 'tiles' },
      tile('Requests', fmt.int(a.requests), a.legacy_requests ? `+${a.legacy_requests} before cutover` : `${fmt.pct(a.share_pct)} of all requests`),
      tile('Quoted value', fmt.eur(a.quoted_eur), `${a.quoted} quotes`),
      tile('Accepted', fmt.int(a.accepted), a.win_rate_pct != null ? `${fmt.eur(a.accepted_eur)} · ${fmt.pct(a.win_rate_pct)} of decided` : `${fmt.eur(a.accepted_eur)} · ${a.rejected} declined`),
      tile('Average all-in', fmt.eur(a.avg_all_in_eur), 'per quote'),
      tile('Average weight', a.avg_gross_kg == null ? '–' : fmt.int(a.avg_gross_kg), `${a.pickup_requests} of ${a.requests} need a pickup`, a.avg_gross_kg == null ? null : 'kg'),
      tile('Mails received', fmt.int(a.mails_in), a.last_mail_at ? `last ${fmt.when(a.last_mail_at)} UTC` : null)),
    h('section', { class: 'card glass c12 flush' }, h('header', null, h('h3', null, 'Shipments')),
      table(deals, cols, { state: sortState(ctx, 'account', 'id'), onRow: d => go(`#/shipment/${d.id}`), empty: 'No priced shipment for this account in this data.' })),
    card('c6', 'Where they ship', null, byDest.length ? barList(byDest, { label: 190, solid: true }) : h('div', { class: 'empty' }, 'No destination on record.')),
    card('c6', 'What they ask for', null, h('div', { class: 'facts' },
      Object.entries(a.goals).map(([g, n]) => fact(words(g), `${n} of ${a.requests}`)),
      fact('Pickup at their site', `${a.pickup_requests} of ${a.requests}`), fact('Average weight', fmt.kg(a.avg_gross_kg)))),
    h('section', { class: 'card glass c12 flush' }, h('header', null, h('h3', null, 'Mail from this account'), h('span', { class: 'note' }, `${mail.total} mails`)), mailRows(ctx, mail.items, new Set())));
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
    { key: 'lane', label: 'Lane', render: l => [l.lane.replace('-', ' → '), h('span', { class: 'sub' }, `${snap.airports[l.origin]?.city || 'origin not known yet'} to ${snap.airports[l.destination]?.city || l.destination}`)] },
    { key: 'distance_km', label: 'Distance', right: true, render: l => fmt.km(l.distance_km) },
    { key: 'requests', label: 'Requests', right: true }, { key: 'quoted', label: 'Quoted', right: true },
    { key: 'accepted', label: 'Accepted', right: true }, { key: 'rejected', label: 'Declined', right: true },
    { key: 'avg_all_in_eur', label: 'Avg. all-in', right: true, render: l => fmt.eur(l.avg_all_in_eur) },
    { key: 'avg_air_eur_per_kg', label: 'Quoted air €/kg', right: true, render: l => fmt.two(l.avg_air_eur_per_kg) },
    { key: 'best_eur_per_kg', label: 'Lowest rate seen', right: true, render: l => (l.best_airline ? [`${fmt.two(l.best_eur_per_kg)} €/kg`, h('span', { class: 'sub' }, l.best_airline)] : '–') },
    { key: 'fastest_days', label: 'Fastest', right: true, render: l => fmt.days(l.fastest_days) },
    { key: 'airlines_priced', label: 'Airlines priced', right: true },
  ];
  const node = h('div', { class: 'grid' },
    globeCard(ctx, 'c8', { height: 560, title: 'Route network' }),
    h('div', { class: 'stack c4' },
      h('div', { class: 'pair' },
        tile('Lanes requested', fmt.int(snap.lanes.filter(l => l.origin).length), 'origin and destination known'),
        tile('Destinations', `${asked}`, `of ${served} served by our carriers`)),
      card('', 'Requests by destination', null, dests.length ? barList(dests, { label: 170, solid: true }) : h('div', { class: 'empty' }, 'No requests yet.'))),
    h('section', { class: 'card glass c12 flush' }, h('header', null, h('h3', null, 'Lanes'), h('span', { class: 'note' }, 'the same figures as on the globe')),
      table(rows, cols, { state: sortState(ctx, 'lanes', 'requests'), onRow: l => { ctx.ui.pipeline.lane = l.lane; ctx.ui.pipeline.state = null; go('#/pipeline'); }, empty: 'No lane matches.' })));
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
    sub = `${snap.truckers.length} truckers`;
    body = h('section', { class: 'card glass c12 flush' }, h('header', null, h('h3', null, 'Truckers'), h('span', { class: 'note' }, 'asked for every pickup in their zones')),
      table(rows, [
        { key: 'name', label: 'Trucker', render: t => [t.name, h('span', { class: 'sub' }, t.email)] },
        { key: 'airports', label: 'Airports', sort: false, render: t => h('span', { class: 'num' }, t.airports.join(' ')) },
        { key: 'zones', label: 'Postal zones', sort: false, render: t => h('span', { class: 'soft num' }, t.zones.join(' ')), cls: 'wrap' },
        { key: 'asked', label: 'Asked', right: true }, { key: 'answered', label: 'Offers', right: true },
        { key: 'answer_rate_pct', label: 'Answer rate', right: true, render: t => (t.answer_rate_pct == null ? '–' : meter(t.answer_rate_pct, fmt.pct(t.answer_rate_pct))) },
        { key: 'cheapest_in', label: 'Cheapest', right: true }, { key: 'chosen', label: 'Chosen', right: true },
        { key: 'avg_price_eur', label: 'Avg. offer', right: true, render: t => fmt.eur(t.avg_price_eur) },
        { key: 'avg_eur_per_100kg', label: '€ per 100 kg', right: true, render: t => fmt.two(t.avg_eur_per_100kg) },
        { key: 'median_pickup_hours', label: 'Pickup within', right: true, render: t => (t.median_pickup_hours == null ? '–' : `${fmt.one(t.median_pickup_hours)} h`) },
        live ? { key: 'median_response_s', label: 'Answers after', right: true, render: t => fmt.duration(t.median_response_s) } : null,
      ].filter(Boolean), { state: sortState(ctx, 'truckers', 'asked') }));
  } else if (tab === 'zones') {
    const rows = snap.zones.filter(z => matches(q, z.zone, z.area, z.airport, z.truckers.join(' ')));
    sub = `${snap.zones.length} pickup zones`;
    body = h('section', { class: 'card glass c12 flush' }, h('header', null, h('h3', null, 'Pickup zones'), h('span', { class: 'note' }, 'first two digits of the postal code decide the airport and who is asked')),
      table(rows, [
        { key: 'zone', label: 'Zone', render: z => h('span', { class: 'num' }, z.zone) }, { key: 'area', label: 'Area' }, { key: 'airport', label: 'Airport' },
        { key: 'requests', label: 'Requests', right: true },
        { key: 'truckers', label: 'Truckers asked', get: z => z.truckers.length, render: z => h('span', { class: 'soft' }, z.truckers.join(', ')), cls: 'wrap' },
      ], { state: sortState(ctx, 'zones', 'zone', 'asc') }));
  } else if (tab === 'tariffs') {
    ctx.cache.tariffs ||= await ctx.api('/crm/api/tariffs', false);
    const all = ctx.cache.tariffs, f = ui.tariffs;
    const options = key => ['', ...new Set(all.map(t => t[key]))].sort();
    const select = (key, label) => h('select', { 'aria-label': label, onchange: e => { f[key] = e.target.value; ctx.rerender(); } },
      options(key).map(v => h('option', { value: v, selected: f[key] === v || null }, v || `${label}: all`)));
    const rows = all.filter(t => (!f.origin || t.origin === f.origin) && (!f.destination || t.destination === f.destination) && (!f.airline || t.airline === f.airline)
      && matches(q, t.airline, t.origin, t.destination));
    sub = `${all.length} tariff lanes`;
    body = [h('div', { class: 'filters' }, select('origin', 'Origin'), select('destination', 'Destination'), select('airline', 'Airline')),
      h('section', { class: 'card glass c12 flush' }, h('header', null, h('h3', null, 'Airline tariffs'), h('span', { class: 'note' }, `${rows.length} of ${all.length} lanes · EUR per kg of chargeable weight`)),
        table(rows, [
          { key: 'airline', label: 'Airline' }, { key: 'origin', label: 'Lane', get: t => `${t.origin}-${t.destination}`, render: t => `${t.origin} → ${t.destination}` },
          { key: 'transit_days', label: 'Transit', right: true, render: t => fmt.days(t.transit_days) },
          { key: 'minimum_eur', label: 'Minimum', right: true, render: t => fmt.eur(t.minimum_eur) },
          { key: 'rates', label: 'Rate from each weight break (kg: €/kg)', sort: false, cls: 'wrap', render: t => h('span', { class: 'num soft' }, t.breaks_kg.map((b, i) => `${fmt.int(b)}: ${fmt.two(t.rates_per_kg[i])}`).join('  ·  ')) },
          { key: 'fuel_per_kg', label: 'Fuel', right: true, render: t => fmt.two(t.fuel_per_kg) },
          { key: 'security_per_kg', label: 'Security', right: true, render: t => fmt.two(t.security_per_kg) },
          { key: 'valid_to', label: 'Valid until', render: t => fmt.day(t.valid_to) }, { key: 'source_file', label: 'File', render: t => h('span', { class: 'muted' }, t.source_file) },
        ], { state: sortState(ctx, 'tariffs', 'airline', 'asc'), empty: 'No tariff lane matches.' }))];
  } else {
    const rows = snap.airlines.filter(a => matches(q, a.name, a.code, a.serves.join(' ')));
    sub = `${snap.airlines.length} airlines`;
    body = h('section', { class: 'card glass c12 flush' }, h('header', null, h('h3', null, 'Airlines'), h('span', { class: 'note' }, `${snap.airlines.filter(a => a.compared).length} priced so far`)),
      table(rows, [
        { key: 'name', label: 'Airline', render: a => [a.name, h('span', { class: 'sub' }, [a.code, a.email].filter(Boolean).join(' · '))] },
        { key: 'source', label: 'Priced from', render: a => (a.source === 'tariff' ? 'tariff file' : a.source === 'portal' ? 'portal' : h('span', { class: 'muted' }, 'not priced yet')) },
        { key: 'compared', label: 'Priced', right: true }, { key: 'cheapest_in', label: 'Cheapest', right: true }, { key: 'fastest_in', label: 'Fastest', right: true },
        { key: 'chosen', label: 'Quoted', right: true }, { key: 'accepted', label: 'Accepted', right: true },
        { key: 'avg_eur_per_kg', label: 'Avg. €/kg', right: true, render: a => fmt.two(a.avg_eur_per_kg) },
        { key: 'avg_transit_days', label: 'Avg. transit', right: true, render: a => fmt.days(a.avg_transit_days) },
        { key: 'serves', label: 'Destinations served', get: a => a.serves.length, render: a => h('span', { class: 'soft num' }, a.serves.join(' ')), cls: 'wrap' },
      ], { state: sortState(ctx, 'airlines', 'compared') }));
  }
  return { title: 'Carriers', sub, node: h('div', { class: 'grid' }, tabs, body) };
}

// --- inbox --------------------------------------------------------------------------------

function mailRows(ctx, items, open) {
  if (!items.length) return h('div', { class: 'empty' }, 'No mail matches.');
  return h('div', null, items.map(m => {
    const body = h('div', { class: 'body', hidden: !open.has(m.id) || null }, h('pre', null, m.body),
      m.reasoning ? h('div', { class: 'why' }, `Classifier: ${m.reasoning}`) : null);
    const line = h('div', { class: 'line', tabindex: 0, onclick: () => { if (open.has(m.id)) open.delete(m.id); else open.add(m.id); body.hidden = !open.has(m.id); } },
      h('span', { class: 'muted num' }, `${fmt.day(m.at)} ${fmt.time(m.at)}`),
      h('span', null, m.who, h('span', { class: 'muted' }, ` · ${ROLES[m.role] || m.role}`)),
      h('span', { class: 'soft' }, m.subject), h('span', null, category(m.category)),
      h('span', { class: `num ${m.confidence < 0.7 ? '' : 'muted'}`, title: 'classifier confidence' }, fmt.int(100 * m.confidence) + '%'),
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
      select('category', 'Type', snap.mail.by_category.map(c => [c.category, `${category(c.category)} (${c.count})`])),
      select('role', 'From', snap.mail.by_role.map(r => [r.role, `${ROLES[r.role] || r.role} (${r.count})`])),
      h('span', { class: 'muted' }, 'The search box above also looks inside the mail text.')),
    h('section', { class: 'card glass c12 flush' }, h('header', null, h('h3', null, 'Mail received'), h('span', { class: 'note' }, `${data.items.length} of ${data.total} shown · newest first · times in UTC`)),
      mailRows(ctx, data.items, ui.openMails),
      data.items.length < data.total ? h('div', { class: 'empty' }, h('button', { class: 'pill', onclick: () => { f.limit += 100; ctx.rerender(); } }, `Show 100 more`)) : null));
  return { title: 'Inbox', sub: `${fmt.int(snap.mail.total)} mails · ${snap.mail.low_confidence} unsure`, node };
}

// --- system -------------------------------------------------------------------------------

export async function system(ctx) {
  const { snap } = ctx, sys = await ctx.api('/crm/api/system', false);
  const modes = { test: ['Test mode', 'Nothing is sent or booked. LLM calls use the evaluation key, shipments go to the test database.'],
    dry_run: ['Drafts only', 'Mails are composed and stored, nothing is sent and nothing is booked.'],
    sending: ['Sending and booking', 'Quotes are sent to customers and accepted quotes are booked for real.'] };
  const [modeName, modeText] = modes[sys.mode];
  const p = sys.poller, c = snap.mail.classification;
  const kv = rows => h('div', { class: 'tablewrap' }, h('table', null, h('tbody', null, rows.map(([k, v]) => h('tr', null, h('td', { class: 'soft' }, k), h('td', { class: 'free' }, v))))));
  const node = h('div', { class: 'grid' },
    card('c4', 'Mode', null, h('div', { style: { display: 'grid', gap: '8px' } },
      h('div', { class: 'hero', style: { fontSize: '26px' } }, modeName), h('p', { class: 'soft' }, modeText))),
    card('c4', 'Mailbox poller', null, h('div', { class: 'facts' },
      fact('State', p.running ? 'running' : p.enabled ? 'enabled, not running' : 'off'), fact('Interval', `${p.interval_seconds} s`),
      fact('Last poll', p.last_poll_at ? `${fmt.when(p.last_poll_at)} UTC` : 'none in this process'), fact('Last mail id read', fmt.int(p.cursor)),
      p.last_error ? fact('Last error', p.last_error) : null)),
    card('c4', 'Mail composed', snap.source === 'replay' ? 'dry run' : 'live', snap.outbound.total
      ? h('div', { style: { display: 'grid', gap: '12px' } },
          barList(snap.outbound.by_kind.map(k => ({ name: KINDS[k.kind] || words(k.kind), value: k.count })), { label: 150, solid: true }),
          h('p', { class: 'muted', style: { fontSize: '12.5px' } }, `${snap.outbound.sent} of ${snap.outbound.total} really sent · ${snap.outbound.templates_used} fell back to a template`))
      : h('div', { class: 'empty' }, 'Nothing composed yet.')),
    card('c6', 'Mail by type', `${fmt.int(snap.mail.total)} mails in the ${snap.source === 'replay' ? 'dry run' : 'live database'}`,
      snap.mail.total ? barList(snap.mail.by_category.map(k => ({ name: category(k.category), value: k.count })), { label: 170, solid: true }) : h('div', { class: 'empty' }, 'No mail stored.')),
    c ? card('c6', 'Classifier against hand labels', 'from the dry run',
          h('div', { style: { display: 'grid', gap: '10px' } }, h('div', { class: 'hero', style: { fontSize: '26px' } }, `${c.correct} of ${c.labelled}`, h('small', null, 'mails sorted as labelled')),
            c.wrong.length ? table(c.wrong, [{ key: 'id', label: 'Mail' }, { key: 'from', label: 'From' }, { key: 'category', label: 'Classified as', render: r => category(r.category) }, { key: 'expected', label: 'Label', render: r => category(r.expected) }])
              : h('p', { class: 'soft' }, 'No mail was sorted differently from its label.')))
      : card('c6', 'Classifier against hand labels', null, h('div', { class: 'empty' }, 'Labels exist for the recorded dry run only. Switch to "Dry run" to see them.')),
    h('section', { class: 'card glass c6 flush' }, h('header', null, h('h3', null, 'Configuration'), h('span', { class: 'note' }, 'no keys are shown')), kv(sys.settings.map(s => [s.name, s.value]))),
    h('section', { class: 'card glass c6 flush' }, h('header', null, h('h3', null, 'Data held')), kv(sys.inventory.map(i => [i.name, [h('span', { class: 'num' }, fmt.int(i.rows)), h('span', { class: 'sub' }, i.where)]]))),
    h('section', { class: 'card glass c12 flush' }, h('header', null, h('h3', null, 'Prompts'), h('span', { class: 'note' }, 'local files; the running router reads Langfuse first')),
      sys.prompts.map(pr => h('details', { class: 'prompt' }, h('summary', null, pr.name), h('pre', null, pr.text)))));
  return { title: 'System', sub: modeName, node };
}
