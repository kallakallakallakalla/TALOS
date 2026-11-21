// The pages. Each takes the context (snapshot, UI state, helpers) and returns
// {title, sub, node}; pages that need more than the snapshot fetch it themselves.

import { createGlobe, REGIONS, routeView } from './globe.js';
import { barList, card, category, exportButton, fmt, h, icon, KINDS, meter, ROLES, splitBar, stateBadge, STATES, table, tipRows, words } from './lib.js';

const go = hash => { location.hash = hash; };
const matches = (q, ...fields) => !q || fields.some(f => String(f ?? '').toLowerCase().includes(q));
const tile = (label, value, foot, unit, onclick) => h(onclick ? 'button' : 'div', { class: `tile glass${onclick ? ' link' : ''}`, onclick },
  h('div', { class: 'label' }, label), h('div', { class: 'value' }, value, unit && h('small', null, unit)), foot && h('div', { class: 'foot' }, foot));
const codes = list => h('span', { class: 'codes' }, list.map(c => h('span', { class: 'tag' }, c)));
const laneText = d => `${d.origin || '?'} → ${d.destination || '?'}`;
const state = d => STATES[d.state]?.[0] || d.state;
const SOURCES = { portal: 'portal', tariff: 'tariff', email: 'email offer' };
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

function globeCard(ctx, span, { height, title = 'Routes', lanes = ctx.snap.lanes, compact = false } = {}) {
  const { snap, ui } = ctx;
  const host = h('div');
  const routed = lanes.filter(l => l.origin);
  const globe = createGlobe(host, {
    airports: snap.airports, lanes, deals: snap.deals, airlines: snap.airlines, height, compact,
    view: compact ? ctx.focusView : ui.globe, coverage: ui.coverage,
    onLane: lane => { ui.pipeline.lane = lane.lane; ui.pipeline.state = null; go('#/lookup/shipments'); },
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

// What an operator can do with a queue item. `confirm` is the question asked first.
const ACTIONS = {
  retry_booking: { label: 'Retry booking', confirm: (item, sending) => (sending
    ? `Book shipment ${item.shipment_id} now?`
    : `Simulate the booking of shipment ${item.shipment_id}? Sending is off, so nothing is booked for real.`) },
  remind_customer: { label: 'Send reminder', confirm: (item, sending) => (sending
    ? `Write and send a reminder to ${item.company}? This uses one LLM call.`
    : `Write a reminder to ${item.company}? Sending is off, so it is stored as a draft. This uses one LLM call.`) },
  close_shipment: { label: 'Close', confirm: item => `Close shipment ${item.shipment_id}? No mail is sent and the request will not be quoted.` },
  retry_mail: { label: 'Process again', confirm: item => `Process mail ${item.mail_id} again? This uses LLM calls and may send a mail.` },
  dismiss: { label: 'Dismiss' },
};

function queue(ctx, items) {
  if (!items.length) return h('div', { class: 'empty' }, 'Nothing needs a decision.');
  return h('div', { class: 'list' }, items.map(item => {
    const open = item.shipment_id ? `#/shipment/${item.shipment_id}` : item.mail_id ? '#/lookup/mail' : null;
    const buttons = item.actions.map(name => {
      const spec = ACTIONS[name], blocked = name !== 'dismiss' && !ctx.snap.can_act;
      return h('button', {
        class: `pill small${name === 'dismiss' ? ' quiet' : ''}`, disabled: blocked || null,
        title: blocked ? 'Available on the router that processes the mailbox, not in this read-only view' : null,
        onclick: e => {
          e.stopPropagation();
          ctx.act(name, { shipment_id: item.shipment_id, mail_id: item.mail_id, key: item.key }, spec.confirm?.(item, ctx.snap.sending));
        },
      }, spec.label);
    });
    return h('div', { class: `item ${item.level}${open ? ' link' : ''}`, onclick: open ? () => go(open) : null },
      icon('alert'),
      h('div', null, h('b', null, item.title), h('p', null, item.detail), h('div', { class: 'acts' }, buttons)),
      h('div', { class: 'meta' }, item.company, item.lane && h('div', null, item.lane.replace('-', ' → '))));
  }));
}

// --- now: does anything need a person? ---------------------------------------------------------

export function now(ctx) {
  const { snap } = ctx, k = snap.kpis, live = snap.source === 'live', n = snap.attention.length;
  const line = [`${k.requests} requests`, `${k.quoted} quoted`, `${k.accepted} accepted`, k.rejected ? `${k.rejected} declined` : null,
    live && k.quotes_timed ? `${k.quotes_in_window} of ${k.quotes_timed} answered within 6 min` : null].filter(Boolean).join(' · ');
  const noLive = live && !snap.deals.length ? h('div', { class: 'notice glass c12' }, icon('info'), h('div', null,
    `The live database contains no priced shipments${snap.legacy.length ? ` (${snap.legacy.length} requests predate this router)` : ''}. `,
    ctx.sources.replay.available ? [`The recorded dry run contains ${ctx.sources.replay.shipments}. `, h('button', { class: 'linkbtn', onclick: () => ctx.pick('replay') }, 'Show dry run')] : null)) : null;
  const node = h('div', { class: 'grid' },
    noLive,
    h('section', { class: 'card glass c12 status' },
      h('h2', null, n ? `${n} ${n === 1 ? 'case needs' : 'cases need'} a decision` : 'Nothing needs you'),
      h('p', null, line, ' · ', h('a', { href: '#/rounds' }, 'see the rounds')),
      snap.can_act ? null : h('p', { class: 'muted' }, live
        ? 'Read-only view: the mailbox poller is not running in this process, so only Dismiss is available.'
        : 'Recorded dry run: timings and acceptance rates are not reported, bookings are simulated, and only Dismiss is available here.')),
    h('section', { class: 'card glass c6 flush' }, head('Action required', `${n} open`), queue(ctx, snap.attention)),
    globeCard(ctx, 'c6', { height: 400 }));
  return { title: 'Now', sub: n ? `${n} open` : 'all clear', node };
}

// --- rounds: one request, one outcome ----------------------------------------------------------

const OUTCOME = { accepted: ['good', 'check'], declined: ['neutral', 'cross'], 'awaiting decision': ['accent', 'dot'], 'in progress': ['accent', 'spin'], 'no quote': ['critical', 'alert'] };
const outcome = r => { const [tone, glyph] = OUTCOME[r.outcome] || ['neutral', 'dot']; return h('span', { class: `state ${tone}` }, icon(glyph), words(r.outcome)); };

export async function rounds(ctx, tab = 'ledger') {
  const data = await ctx.api('/crm/api/rounds'), live = data.source === 'live';
  const tabs = h('div', { class: 'tabs' }, h('div', { class: 'seg' },
    [['ledger', 'Ledger'], ['mails', 'Quote mails']].map(([key, label]) => h('button', { class: key === tab ? 'on' : '', onclick: () => go(`#/rounds/${key}`) }, label))));
  if (tab === 'mails') {
    // the jury reads the accepted mails, so they come first
    const sent = data.rounds.filter(r => r.quote_mail).sort((a, b) => (b.outcome === 'accepted') - (a.outcome === 'accepted') || b.id - a.id);
    const broken = data.quote_mails - data.mails_clean;
    return { title: 'Rounds', sub: `${sent.length} quote mails`, node: h('div', { class: 'grid' }, tabs,
      h('section', { class: 'card glass c7 status' },
        h('h2', null, !sent.length ? 'No quote mail yet' : broken ? `${broken} of ${data.quote_mails} quote mails break a style rule` : `All ${data.quote_mails} quote mails keep the style rules`),
        h('p', null, 'Each mail is checked against the rules of the compose prompt (talos-compose-mail). The jury reads the accepted mails without team names and picks up to three per round.'),
        h('p', { class: 'muted' }, live ? 'A breach here means the model ignored its prompt, or the prompt in Langfuse is not the one in the repository.'
          : 'A recorded run shows the prompt that was in use when it was recorded, not the current one.')),
      card('c5', 'Rules broken', 'mails per rule', data.mail_checks.length
        ? barList(data.mail_checks.map(c => ({ name: words(c.check), value: c.count })), { label: 190, solid: true })
        : h('div', { class: 'empty' }, 'None.')),
      sent.map(r => h('section', { class: 'card glass c6' },
        head(`Round ${r.id}`, r.company, outcome(r)),
        h('div', { class: 'flags' }, r.mail_flags.length ? r.mail_flags.map(f => h('span', { class: 'flag' }, f)) : h('span', { class: 'flag ok' }, 'keeps the style rules')),
        h('details', { class: 'ask' }, h('summary', null, 'Their request'), h('pre', null, r.request_mail || 'not stored')),
        h('pre', { class: 'letter' }, r.quote_mail)))) };
  }
  const q = ctx.q, rows = data.rounds.filter(r => matches(q, r.id, r.company, r.lane, r.outcome, r.cause, r.airline));
  const cols = [
    { key: 'id', label: 'Round', render: r => [h('span', { class: 'num' }, r.id), h('span', { class: 'sub' }, `${fmt.day(r.received_at)} ${fmt.time(r.received_at)}`)] },
    { key: 'company', label: 'Account', cls: 'clip' },
    { key: 'lane', label: 'Lane', render: r => (r.lane || '–').replace('-', ' → ') },
    { key: 'all_in_eur', label: 'Quote EUR', right: true, render: r => (r.all_in_eur == null ? '–' : fmt.eurExact(r.all_in_eur)) },
    live ? { key: 'minutes_to_quote', label: 'Answered after', right: true, render: r => (r.minutes_to_quote == null ? '–' : `${fmt.one(r.minutes_to_quote)} min`) } : null,
    { key: 'outcome', label: 'Outcome', render: outcome, csv: r => r.outcome },
    { key: 'booked', label: 'Booked', get: r => (r.booked ? 1 : 0), render: r => (r.booked ? 'yes' : r.dry_run_booking ? 'simulated' : '–'), csv: r => (r.booked ? 'yes' : 'no') },
    data.board_available ? { key: 'board', label: 'Board', get: r => r.board?.verdict, render: r => (r.board ? [r.board.verdict, r.board.reason && h('span', { class: 'sub' }, r.board.reason)] : '–'), csv: r => [r.board?.verdict, r.board?.reason].filter(Boolean).join(' / ') } : null,
    { key: 'cause', label: 'Loss', render: r => r.cause || h('span', { class: 'muted' }, '–') },
    { key: 'customer_said', label: 'Customer said', cls: 'clip wide', render: r => (r.customer_said ? h('span', { title: r.customer_said, class: 'soft' }, r.customer_said.split('\n')[0]) : h('span', { class: 'muted' }, '–')) },
  ].filter(Boolean);
  const node = h('div', { class: 'grid' }, tabs,
    h('section', { class: 'card glass c7 status' },
      h('h2', null, !live ? `${data.total} recorded rounds` : data.decided ? `${data.clean} of ${data.decided} decided rounds without a loss` : 'No decided rounds yet'),
      h('p', null, [`${data.total} requests`, `${data.quoted} quoted`, `${data.accepted} accepted`, `${data.declined} declined`, `${data.no_quote} without a quote`,
        live ? `${data.booked} booked` : null, data.timed ? `${data.in_window} of ${data.timed} within 6 min` : null].filter(Boolean).join(' · ')),
      h('p', { class: 'muted' }, data.board_available ? 'Verdicts and reasons include the platform board.' : live ? 'The platform board could not be read; outcomes come from our own records.' : 'Dry run: outcomes come from the recorded replies, which answered the previous process.')),
    card('c5', 'What the losses were', null, data.causes.length
      ? barList(data.causes.map(c => ({ name: c.cause, value: c.count })), { label: 230, solid: true })
      : h('div', { class: 'empty' }, 'No losses recorded.')),
    h('section', { class: 'card glass c12 flush' }, head('Ledger', `${rows.length} rounds`, exportButton('talos-rounds.csv', rows, cols)),
      table(rows, cols, { state: sortState(ctx, 'rounds', 'id'), onRow: r => go(`#/shipment/${r.id}`), empty: 'No round matches.' })));
  return { title: 'Rounds', sub: `${data.total} requests · ${data.accepted} accepted`, node };
}

// --- tuning: settings and the evidence for their value ------------------------------------------

const VERDICT = { change: ['warning', 'alert', 'Change suggested'], keep: ['good', 'check', 'Holds'], open: ['neutral', 'dot', 'Too little data'] };

export async function tuning(ctx) {
  const { snap } = ctx, list = await ctx.api('/crm/api/levers');
  const metrics = [
    { key: 'title', label: 'Metric', render: f => h('b', { style: { fontWeight: 600 } }, f.title) },
    { key: 'value', label: 'Value', right: true, sort: false, render: f => fmt.figure(f.value, f.unit, f.n), csv: f => f.value },
    { key: 'n', label: 'Cases', right: true }, { key: 'text', label: 'Definition', sort: false, cls: 'desc' },
  ];
  const changes = list.filter(l => l.verdict?.tone === 'change').length;
  const lever = l => {
    const [tone, glyph, label] = VERDICT[l.verdict?.tone] || [];
    return h('section', { class: 'card glass c6 lever' },
      head(l.title, `n = ${l.n}`, label && h('span', { class: `state ${tone}` }, icon(glyph), label)),
      h('div', { class: 'hero' }, l.figure, h('small', null, l.figure_label)),
      l.verdict && h('p', { class: 'verdict' }, l.verdict.text),
      h('p', null, l.text),
      l.key === 'llm_usage' && l.rows.length ? barList(l.rows.map(r => ({ name: words(r.name), value: r.tokens, note: `${r.calls} calls` })), { label: 190, solid: true }) : null,
      h('p', { class: 'setting' }, h('code', null, `${l.setting} = ${l.value}`)));
  };
  const node = h('div', { class: 'grid' },
    h('section', { class: 'card glass c12 status' },
      h('h2', null, changes ? `${changes} ${changes === 1 ? 'setting' : 'settings'} should change` : 'No setting needs to change'),
      h('p', null, 'One card per setting, with what the data says about its current value. Settings live in router/.env and take effect after a restart; this page does not change them.')),
    list.map(lever),
    h('section', { class: 'card glass c12 flush' },
      h('details', { class: 'prompt fold' }, h('summary', null, `Reference figures (${snap.findings.length})`),
        h('div', { class: 'foldbar' }, exportButton('talos-metrics.csv', snap.findings, metrics)),
        table(snap.findings, metrics, { empty: 'No priced shipments to analyse.' }))));
  return { title: 'Tuning', sub: changes ? `${changes} suggested` : 'nothing to change', node };
}

// --- lookup: everything else, behind tabs and the search box -------------------------------------

const LOOKUP = [['shipments', 'Shipments'], ['accounts', 'Accounts'], ['airlines', 'Airlines'], ['truckers', 'Truckers'], ['zones', 'Pickup zones'], ['tariffs', 'Tariffs'], ['mail', 'Mail']];

export async function lookup(ctx, tab = 'shipments') {
  const inner = tab === 'shipments' ? pipeline(ctx) : tab === 'accounts' ? accounts(ctx) : tab === 'mail' ? await inbox(ctx) : await carriers(ctx, tab);
  inner.node.prepend(h('div', { class: 'tabs' }, h('div', { class: 'seg' },
    LOOKUP.map(([key, label]) => h('button', { class: key === tab ? 'on' : '', onclick: () => go(`#/lookup/${key}`) }, label)))));
  return { title: 'Lookup', sub: inner.sub, node: inner.node };
}

// --- shipments -----------------------------------------------------------------------------

function pipeline(ctx) {
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
  const d = detail.deal;
  const trace = h('div', { class: 'trace' }, detail.trace.map(t => h('div', { class: `tstep ${t.tone}` },
    h('span', { class: 'tdot' }),
    h('div', null, h('b', null, t.label), t.at && h('span', { class: 'muted' }, ` · ${fmt.when(t.at)} UTC`), h('p', null, t.detail)))));
  const account = ctx.snap.accounts.find(a => a.email === d.account);

  const request = card('', 'Request', d.subject, h('div', { class: 'facts' },
    fact('Origin', d.pickup ? `${d.pickup_postal_code || ''} ${d.pickup_city || ''}`.trim() || '–' : `delivered to ${d.origin || 'the airport'}`),
    fact('Departure airport', d.origin ? `${d.origin}${d.zone ? ` (zone ${d.zone})` : ''}` : '–'),
    fact('Destination', d.destination ? `${d.destination} · ${ctx.snap.airports[d.destination]?.city || ''}` : '–'),
    fact('Ready date', fmt.day(d.ready_date)), fact('Pieces', fmt.int(d.pieces)), fact('Gross weight', fmt.kg(d.gross_kg)),
    fact('Volume', fmt.cbm(d.volume_cbm)), fact('Chargeable weight', `${fmt.kg(d.chargeable_kg)}${d.volumetric ? ' (volumetric)' : ''}`),
    fact('Commodity', d.commodity), fact('Priority', d.goal || 'not stated (priced as cheapest)'), fact('Contact', d.contact),
    d.deliver_by ? fact('Deliver by', fmt.day(d.deliver_by)) : null,
    d.dangerous_goods ? fact('Dangerous goods', `${d.dangerous_goods} · documents ${d.dg_documents_received ? 'received' : 'outstanding'}`) : null,
    d.special_cargo ? fact('Special cargo', words(d.special_cargo)) : null,
    d.preferred_airline || d.preferred_trucker ? fact('Preferred carriers', [d.preferred_airline, d.preferred_trucker].filter(Boolean).join(', ')) : null,
    d.excluded_carriers?.length ? fact('Excluded carriers', d.excluded_carriers.join(', ')) : null,
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
        fact('Airline', d.airline), fact('Transit', fmt.days(d.transit_days)), fact('Rate source', SOURCES[d.air_source] || d.air_source),
        fact('Billed weight', `${fmt.kg(d.billed_kg)}${d.weight_break ? ' (next weight break)' : ''}`), fact('Air rate', d.air_eur_per_kg ? `${fmt.two(d.air_eur_per_kg)} €/kg` : '–'),
        fact('Pickup', fmt.day(d.estimated_pickup)), fact('Departure', fmt.day(d.estimated_departure)), fact('Arrival', fmt.day(d.estimated_arrival)),
        d.booking_ref ? fact('Booking reference', d.dry_run_booking ? 'none (dry run)' : d.booking_ref) : null)));

  const top = Math.max(...detail.options.map(o => o.total_eur), 0);
  const flights = detail.options.length ? card('', 'Air options', `${detail.options.length} priced · selected option highlighted`,
    barList(detail.options.map(o => ({
      name: o.airline, value: o.total_eur, em: o.chosen, text: fmt.eurExact(o.total_eur),
      note: `${fmt.days(o.transit_days)}${o.cheapest ? ' · lowest' : ''}${o.fastest ? ' · fastest' : ''}`,
      tip: () => tipRows(o.airline, [['Total', fmt.eurExact(o.total_eur)], ['Transit', fmt.days(o.transit_days)], ['Billed weight', fmt.kg(o.billed_kg)],
        ['Rate', `${fmt.two(o.eur_per_kg)} €/kg`], ['Source', SOURCES[o.source] || o.source], o.chosen && ['Selected', 'yes']]),
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
    h('div', { class: 'crumbs' }, h('a', { href: '#/rounds' }, 'Rounds'), '/', h('span', null, `Shipment ${d.id}`)),
    h('div', { class: 'title' }, h('h2', null, d.company), stateBadge(d), h('span', { class: 'muted' }, laneText(d))),
    h('div', { class: 'stack c8' }, card('', 'What happened', `${detail.trace.length} steps`, trace), request, offer, flights, trucks, h('section', { class: 'card glass flush' }, h('details', { class: 'prompt fold' }, h('summary', null, `Correspondence (${detail.timeline.length} mails)`), h('div', { class: 'foldbody' }, correspondence(detail))))),
    side);
  return { title: `Shipment ${d.id}`, sub: `${d.company} · ${laneText(d)}`, node };
}

// --- accounts -----------------------------------------------------------------------------

const priorities = goals => Object.entries(goals).map(([goal, n]) => `${n} ${goal}`).join(', ');

function accounts(ctx) {
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
    h('div', { class: 'crumbs' }, h('a', { href: '#/lookup/accounts' }, 'Accounts'), '/', h('span', null, a.company)),
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

// --- carriers -----------------------------------------------------------------------------

async function carriers(ctx, tab = 'airlines') {
  const { snap, q, ui } = ctx, live = snap.source === 'live';
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
      { key: 'source', label: 'Rate source', render: a => SOURCES[a.source] || h('span', { class: 'muted' }, 'not yet priced') },
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
  return { title: 'Carriers', sub, node: h('div', { class: 'grid' }, body) };
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

async function inbox(ctx) {
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
    h('section', { class: 'card glass c12 flush' }, head('Details', 'credentials are not displayed', h('button', { class: 'pill small', onclick: () => ctx.act('restore', {}) }, 'Restore dismissed items')),
      h('details', { class: 'prompt' }, h('summary', null, 'Configuration'), kv(sys.settings.map(x => [x.name, x.value]))),
      h('details', { class: 'prompt' }, h('summary', null, 'Data inventory'), kv(sys.inventory.map(x => [x.name, [h('span', { class: 'num' }, fmt.int(x.rows)), h('span', { class: 'sub' }, x.where)]]))),
      sys.prompts.map(pr => h('details', { class: 'prompt' }, h('summary', null, `Prompt: ${pr.name}`), h('pre', null, pr.text)))));
  return { title: 'System', sub: modeName, node };
}
