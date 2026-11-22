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
    onLane: lane => { ui.rounds.lane = lane.lane; ui.rounds.outcome = null; go('#/rounds'); },
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

function actionButtons(ctx, item) {
  return item.actions.map(name => {
    const spec = ACTIONS[name], blocked = name !== 'dismiss' && !ctx.snap.can_act;
    return h('button', {
      class: `pill small${name === 'dismiss' ? ' quiet' : ''}`, disabled: blocked || null,
      title: blocked ? 'Works on the router that reads the mailbox, not in this view' : null,
      onclick: e => {
        e.stopPropagation();
        ctx.act(name, { shipment_id: item.shipment_id, mail_id: item.mail_id, key: item.key }, spec.confirm?.(item, ctx.snap.sending));
      },
    }, spec.label);
  });
}

function queue(ctx, items) {
  return h('div', { class: 'list' }, items.map(item => {
    const open = item.shipment_id ? `#/shipment/${item.shipment_id}` : item.mail_id ? '#/lookup/mail' : null;
    return h('div', { class: `item ${item.level}${open ? ' link' : ''}`, onclick: open ? () => go(open) : null },
      icon('alert'),
      h('div', null, h('b', null, item.title), h('p', null, item.detail), h('div', { class: 'acts' }, actionButtons(ctx, item))),
      h('div', { class: 'meta' }, item.waiting_minutes != null && `waiting ${fmt.duration(item.waiting_minutes * 60)}`, item.lane && h('div', null, item.lane.replace('-', ' → '))));
  }));
}

// --- now: does anything need a person? ---------------------------------------------------------

export async function now(ctx) {
  const { snap } = ctx, k = snap.kpis, live = snap.source === 'live', p = snap.poller;
  const cases = snap.attention.filter(a => a.needs_person), notes = snap.attention.filter(a => !a.needs_person), n = cases.length;
  const decided = k.accepted + k.rejected;
  // one line on whether the router itself is alive, so an empty queue can be trusted
  const limited = !live ? 'Recorded dry run: timings and acceptance rates are not reported, bookings are simulated, and only Dismiss works here.'
    : !p?.enabled ? 'Read-only view: this process does not read the mailbox, so only Dismiss works here.' : null;
  const noLive = live && !snap.deals.length ? h('div', { class: 'notice glass c12' }, icon('info'), h('div', null,
    `The live database contains no priced shipments${snap.legacy.length ? ` (${snap.legacy.length} requests predate this router)` : ''}. `,
    ctx.sources.replay.available ? [`The recorded dry run contains ${ctx.sources.replay.shipments}. `, h('button', { class: 'linkbtn', onclick: () => ctx.pick('replay') }, 'Show dry run')] : null)) : null;
  const listed = cases.length || notes.length || snap.dismissed;
  const node = h('div', { class: 'grid' },
    noLive,
    h('section', { class: 'card glass c12 status' },
      h('h2', null, n ? `${n} ${n === 1 ? 'case needs' : 'cases need'} a decision` : 'Nothing needs you'),
      limited && h('p', null, limited),
      h('p', { class: 'next' },
        h('a', { href: '#/rounds' }, decided ? `${k.rejected} of ${decided} decided quotes declined` : `${k.requests} ${k.requests === 1 ? 'request' : 'requests'}, none decided yet`),
        h('a', { href: '#/performance' }, `${k.quoted} of ${k.requests} requests quoted`))),
    listed ? h('section', { class: 'card glass c6 flush' },
      cases.length ? queue(ctx, cases) : h('div', { class: 'empty' }, 'Nothing needs a decision.'),
      notes.length ? [h('div', { class: 'subhead' }, 'For information: nothing to do here'), queue(ctx, notes)] : null,
      snap.dismissed ? h('div', { class: 'subhead' }, `${snap.dismissed} dismissed · `, h('button', { class: 'linkbtn', onclick: () => ctx.act('restore', {}) }, 'restore')) : null) : null,
    globeCard(ctx, listed ? 'c6' : 'c12', { height: 400 }));
  return { title: 'Now', sub: '', node };
}

// --- sessions: one request as it happens -------------------------------------------------------

const ACTOR = { talos: 'TALOS', customer: 'Customer', trucker: 'Trucker', airline: 'Airline' };
const laneArrow = lane => (lane || '–').replace('-', ' → ');

function eventNode(e) {
  return h('div', { class: `sevent ${e.actor === 'talos' ? 'own' : 'theirs'} ${e.tone}` },
    h('span', { class: 'swho' }, ACTOR[e.actor] || 'Mail'),
    h('div', null,
      h('b', null, e.title), e.at && h('span', { class: 'muted' }, ` · ${fmt.time(e.at)} UTC`),
      e.detail && h('p', null, e.detail),
      e.body && h('details', { class: 'ask' }, h('summary', null, 'Mail text'), h('pre', null, e.body))));
}

// The list of requests on the left, the chosen one on the right as a stream of events. On live
// data the server pushes each new event as the router stores it (server-sent events); "Replay"
// has the server send a session's events again, one by one.
export async function sessions(ctx, id) {
  const list = await ctx.api('/crm/api/sessions'), live = ctx.snap.source === 'live';
  const current = list.find(s => String(s.id) === String(id)) || list[0];
  if (!current) {
    if (live) {  // wait for the first request and show it the moment it arrives
      const first = new EventSource('/crm/api/stream?source=live');
      first.addEventListener('step', () => ctx.rerender());
      ctx.onLeave(() => first.close());
    }
    return { title: 'Sessions', sub: '', node: h('div', { class: 'grid' }, h('div', { class: 'empty glass c12' }, 'No request yet. A session opens with the first mail of a request.')) };
  }
  const { events } = await ctx.api(`/crm/api/sessions/${current.id}`);
  const shown = new Map(), stream = h('div', { class: 'stream', 'aria-live': 'polite' });
  const add = (e, fresh) => {
    const node = eventNode(e), known = shown.get(e.id);
    shown.set(e.id, node);
    if (known) { known.replaceWith(node); return; }  // the same event, now saying more
    stream.append(node);
    if (fresh) { node.classList.add('fresh'); node.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); }
  };
  events.forEach(e => add(e, false));
  const rows = new Map(list.map(s => [s.id, h('a', { class: `srow${s.id === current.id ? ' on' : ''}`, href: `#/sessions/${s.id}` },
    h('span', { class: `dot ${s.open ? 'accent' : ''}`, title: s.open ? 'open' : 'closed' }),
    h('span', { class: 'sname' }, h('b', null, s.company), h('small', null, `${laneArrow(s.lane)} · ${STATES[s.state]?.[0] || s.state}`)),
    h('span', { class: 'stime' }, s.last_at ? fmt.time(s.last_at) : '–', h('small', null, `${s.mails} mails`)))]));

  const state = h('span', { class: 'chip' });
  const say = (tone, text) => state.replaceChildren(h('span', { class: `dot ${tone}` }), text);
  const idle = () => (live ? say('live', 'Live') : say('', 'Recorded run'));
  let playing = null, connected = false;
  if (live) {
    const feed = new EventSource('/crm/api/stream?source=live');
    feed.addEventListener('ready', async () => {
      if (connected) (await ctx.api(`/crm/api/sessions/${current.id}`)).events.forEach(e => add(e, true));  // after a reconnect: what was missed
      connected = true;
      if (!playing) idle();
    });
    feed.addEventListener('step', m => {
      const e = JSON.parse(m.data), row = rows.get(e.session);
      if (!row) { ctx.rerender(); return; }  // a new request: the list has to grow
      row.classList.remove('pulse'); void row.offsetWidth; row.classList.add('pulse');
      if (e.session === current.id && !playing) add(e, true);
    });
    feed.onerror = () => { if (!playing) say('warning', 'Reconnecting'); };
    ctx.onLeave(() => feed.close());
  }
  const replay = () => {
    playing?.close();
    shown.clear(); stream.replaceChildren();
    say('accent', 'Replaying');
    playing = new EventSource(`/crm/api/stream?source=${ctx.snap.source}&session=${current.id}&replay=1`);
    playing.addEventListener('step', m => add(JSON.parse(m.data), true));
    const end = () => { playing?.close(); playing = null; idle(); };
    playing.addEventListener('done', end);
    playing.onerror = end;
  };
  ctx.onLeave(() => playing?.close());
  idle();
  const node = h('div', { class: 'grid' },
    h('section', { class: 'card glass c4 flush' }, head('Sessions', `${list.length} ${list.length === 1 ? 'request' : 'requests'}`), h('div', { class: 'slist' }, [...rows.values()])),
    h('section', { class: 'card glass c8' },
      head(current.company, `${laneArrow(current.lane)} · session ${current.id}`, [state,
        h('button', { class: 'pill small', onclick: replay }, 'Replay'),
        h('a', { class: 'pill small', href: `#/shipment/${current.id}` }, 'Open shipment')]),
      stream));
  return { title: 'Sessions', sub: '', node };
}

// --- performance: what the router did, and with whom ---------------------------------------------

export async function performance(ctx) {
  const c = await ctx.api('/crm/api/scorecard');
  const share = (part, whole) => (whole ? fmt.pct(100 * part / whole) : '–');
  const used = c.airlines.filter(a => a.quoted), unused = c.airlines.length - used.length;
  const chosen = c.truckers.filter(t => t.selected);
  const top = Math.max(...c.airlines.map(a => a.quoted), 1), topTruck = Math.max(...c.truckers.map(t => t.asked), 1);
  const airlineCols = [
    { key: 'name', label: 'Airline' },
    { key: 'quoted', label: 'Quoted', right: true, render: a => meter(100 * a.quoted / top, fmt.int(a.quoted)) },
    { key: 'share', label: 'Share of quotes', right: true, get: a => a.quoted, render: a => share(a.quoted, c.quoted), csv: a => (c.quoted ? Math.round(1000 * a.quoted / c.quoted) / 10 : '') },
  ];
  const truckerCols = [
    { key: 'name', label: 'Trucker' },
    { key: 'selected', label: 'Used in a quote', right: true },
    { key: 'asked', label: 'Asked', right: true, render: t => meter(100 * t.asked / topTruck, fmt.int(t.asked)) },
    { key: 'answered', label: 'Offers', right: true },
    { key: 'rate', label: 'Answered', right: true, get: t => (t.asked ? t.answered / t.asked : 0), render: t => share(t.answered, t.asked), csv: t => (t.asked ? Math.round(100 * t.answered / t.asked) : '') },
  ];
  const askCols = [
    { key: 'name', label: 'In the request' }, { key: 'count', label: 'Requests', right: true, render: a => meter(c.requests ? 100 * a.count / c.requests : 0, fmt.int(a.count)) },
    { key: 'share', label: 'Share', right: true, get: a => a.count, render: a => share(a.count, c.requests), csv: a => (c.requests ? Math.round(100 * a.count / c.requests) : '') },
  ];
  const node = h('div', { class: 'grid' },
    h('div', { class: 'stack c7' },
      h('section', { class: 'card glass flush' },
        head('Airlines used', used.length ? `${used.length} airlines in ${c.quoted} quotes · most used ${fmt.pct(c.top_airline_share_pct)}` : 'no quote yet', exportButton('talos-airlines-used.csv', c.airlines, airlineCols)),
        table(used, airlineCols, { state: sortState(ctx, 'used', 'quoted'), empty: 'No quote has gone out.' }),
        unused ? h('div', { class: 'subhead' }, `${unused} more were priced and not chosen · `, h('a', { href: '#/lookup/airlines' }, 'all airlines')) : null),
      h('section', { class: 'card glass flush' },
        head('Truckers used', c.pickups ? `${chosen.length} used in a quote · ${c.truckers.length} asked for ${c.pickups} pickups` : 'no pickup yet', exportButton('talos-truckers-used.csv', c.truckers, truckerCols)),
        table(c.truckers.slice(0, 8), truckerCols, { state: sortState(ctx, 'trucks', 'selected'), empty: 'No trucker has been asked.' }),
        c.truckers.length > 8 ? h('div', { class: 'subhead' }, `${c.truckers.length - 8} more asked · `, h('a', { href: '#/lookup/truckers' }, 'all truckers')) : null)),
    h('section', { class: 'card glass c5 flush' },
      head('In the requests', `${c.requests} requests`, exportButton('talos-requests-content.csv', c.asks, askCols)),
      table(c.asks, askCols, { empty: 'No request yet.' }),
      h('div', { class: 'subhead' }, 'Insurance is counted by a word search in the request mail; the other rows come from the extracted request.')));
  return { title: 'Performance', sub: '', node };
}

// --- rounds: one request, one outcome ----------------------------------------------------------

const OUTCOME = { accepted: ['good', 'check'], declined: ['neutral', 'cross'], 'awaiting decision': ['accent', 'dot'], 'in progress': ['accent', 'spin'],
  'waiting for customer': ['warning', 'clock'], 'no quote': ['critical', 'alert'] };
const outcome = (r, live) => {
  const unbooked = live && r.outcome === 'accepted' && !r.booked;  // the exception worth a word
  const [tone, glyph] = unbooked ? ['warning', 'alert'] : OUTCOME[r.outcome] || ['neutral', 'dot'];
  return h('span', { class: `state ${tone}` }, icon(glyph), unbooked ? 'Accepted, not booked' : words(r.outcome));
};

export async function rounds(ctx, tab = 'ledger') {
  const data = await ctx.api('/crm/api/rounds'), live = data.source === 'live', f = ctx.ui.rounds;
  const pill = (label, count, on, set) => h('button', { class: `pill${on ? ' on' : ''}`, onclick: () => { set(); ctx.rerender(); } }, label, count != null && h('b', null, count));
  const tabs = h('div', { class: 'tabs' }, h('div', { class: 'seg' },
    [['ledger', 'Ledger'], ['mails', 'Quote mails']].map(([key, label]) => h('button', { class: key === tab ? 'on' : '', onclick: () => go(`#/rounds/${key}`) }, label))));
  if (tab === 'mails') {
    // the jury reads the accepted mails: they are the default
    const sent = data.rounds.filter(r => r.quote_mail), accepted = sent.filter(r => r.outcome === 'accepted');
    const shown = f.all || !accepted.length ? sent : accepted, broken = data.quote_mails - data.mails_clean;
    return { title: 'Rounds', sub: '', node: h('div', { class: 'grid' }, tabs,
      h('section', { class: 'card glass c7 status' },
        h('h2', null, !sent.length ? 'No quote mail yet' : broken ? `${broken} of ${data.quote_mails} quote mails break a style rule` : `All ${data.quote_mails} quote mails keep the style rules`),
        h('p', null, 'Checked against the rules of the compose prompt. The jury reads the accepted mails without team names and picks up to three per round.'),
        h('p', null, live ? 'A breach means the model ignored its prompt, or the prompt in Langfuse is not router/prompts/talos-compose-mail.md.'
          : 'A recorded run shows the prompt that was in use when it was recorded, not the current one.')),
      card('c5', 'Rules broken', 'mails per rule', data.mail_checks.length
        ? barList(data.mail_checks.map(c => ({ name: words(c.check), value: c.count })), { label: 190, solid: true })
        : h('div', { class: 'empty' }, 'None.')),
      sent.length ? h('div', { class: 'filters' },
        pill('Accepted', accepted.length, !f.all, () => { f.all = false; }), pill('All', sent.length, f.all, () => { f.all = true; })) : null,
      shown.map(r => h('section', { class: 'card glass c6' },
        head(`Round ${r.id}`, r.company, outcome(r, live)),
        h('div', { class: 'flags' }, r.mail_flags.length ? r.mail_flags.map(flag => h('span', { class: 'flag' }, flag)) : h('span', { class: 'flag ok' }, 'keeps the style rules')),
        h('details', { class: 'ask' }, h('summary', null, 'Their request'), h('pre', null, r.request_mail || 'not stored')),
        h('pre', { class: 'letter' }, r.quote_mail)))) };
  }
  const outcomes = Object.keys(OUTCOME).map(name => [name, data.rounds.filter(r => r.outcome === name).length]).filter(([, count]) => count);
  const rows = data.rounds.filter(r => (!f.outcome || r.outcome === f.outcome) && (!f.lane || r.lane === f.lane)
    && matches(ctx.q, r.id, r.company, r.lane, r.outcome, r.cause, r.airline));
  const cols = [
    { key: 'id', label: 'Round', render: r => [h('span', { class: 'num' }, r.id), h('span', { class: 'sub' }, `${fmt.day(r.received_at)} ${fmt.time(r.received_at)}`)] },
    { key: 'company', label: 'Account', cls: 'clip' },
    { key: 'lane', label: 'Lane', render: r => (r.lane || '–').replace('-', ' → ') },
    { key: 'airline', label: 'Airline', cls: 'clip', render: r => r.airline || h('span', { class: 'muted' }, '–') },
    { key: 'all_in_eur', label: 'Quote EUR', right: true, render: r => (r.all_in_eur == null ? '–' : fmt.eurExact(r.all_in_eur)) },
    live ? { key: 'minutes_to_quote', label: 'Answered after', right: true, render: r => (r.minutes_to_quote == null ? '–' : `${fmt.one(r.minutes_to_quote)} min`) } : null,
    { key: 'outcome', label: 'Outcome', render: r => outcome(r, live), csv: r => r.outcome },
    { key: 'booked', label: 'Booked', hidden: true, csv: r => (r.booked ? 'yes' : 'no') },
    data.board_available ? { key: 'board', label: 'Board', get: r => r.board?.verdict, render: r => (r.board ? [r.board.verdict, r.board.reason && h('span', { class: 'sub' }, r.board.reason)] : '–'), csv: r => [r.board?.verdict, r.board?.reason].filter(Boolean).join(' / ') } : null,
    data.board_available ? { key: 'points', label: 'Points', right: true, get: r => r.board?.points, csv: r => r.board?.points ?? '',
      render: r => (typeof r.board?.points === 'number' ? h('span', { title: Object.entries(r.board.kinds || {}).map(([kind, value]) => `${kind} ${value}`).join(' · ') || null }, fmt.one(r.board.points)) : '–') } : null,
    { key: 'cause', label: 'Why it was lost', cls: 'wrap reason', render: r => r.cause || h('span', { class: 'muted' }, '–') },
    { key: 'customer_said', label: 'Customer said', hidden: true },
  ].filter(Boolean);
  const note = data.board_note || (data.board_available ? 'Verdicts, reasons and points come from the platform board.'
    : live ? 'The platform board could not be read; outcomes come from our own records.'
    : 'Dry run: the recorded replies answered the earlier process\'s quotes, not the ones shown here.');
  const node = h('div', { class: 'grid' }, tabs,
    h('section', { class: 'card glass c12' }, head('Why rounds were lost', `${data.lost} of ${data.decided} decided`),
      data.causes.length
        ? h('div', { class: 'reasons' }, data.causes.map(c => h('div', null, h('b', null, `${c.count} ×`), h('span', null, c.cause))))
        : h('div', { class: 'muted' }, data.decided ? 'No round was lost.' : 'No round has been decided yet.'),
      h('p', { class: 'muted cardnote' }, note)),
    h('div', { class: 'filters' },
      pill('All', data.total, !f.outcome, () => { f.outcome = null; }),
      outcomes.map(([name, count]) => pill(words(name), count, f.outcome === name, () => { f.outcome = f.outcome === name ? null : name; })),
      f.lane ? pill(`${f.lane.replace('-', ' → ')} ×`, null, true, () => { f.lane = null; }) : null),
    h('section', { class: 'card glass c12 flush' }, head('Ledger', `${rows.length} of ${data.total} rounds`, exportButton('talos-rounds.csv', rows, cols)),
      table(rows, cols.filter(c => !c.hidden), { state: sortState(ctx, 'rounds', 'id'), onRow: r => go(`#/shipment/${r.id}`), empty: 'No round matches.' })));
  return { title: 'Rounds', sub: '', node };
}

// --- lookup: everything else, behind tabs and the search box -------------------------------------

const LOOKUP = [['accounts', 'Accounts'], ['airlines', 'Airlines'], ['truckers', 'Truckers'], ['tariffs', 'Tariffs'], ['mail', 'Mail']];

export async function lookup(ctx, tab = 'accounts') {
  const inner = tab === 'mail' ? await inbox(ctx) : ['airlines', 'truckers', 'tariffs'].includes(tab) ? await carriers(ctx, tab) : accounts(ctx);
  inner.node.prepend(h('div', { class: 'tabs' }, h('div', { class: 'seg' },
    LOOKUP.map(([key, label]) => h('button', { class: key === tab || (key === 'accounts' && !LOOKUP.some(([k]) => k === tab)) ? 'on' : '', onclick: () => go(`#/lookup/${key}`) }, label)))));
  return { title: 'Lookup', sub: inner.sub, node: inner.node };
}

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

  const request = h('div', null, h('h4', null, 'Request'), h('div', { class: 'facts' },
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

  const offer = d.all_in_eur == null ? null : h('div', null, h('h4', null, 'Offer', d.valid_until && h('span', { class: 'muted' }, ` · valid until ${d.valid_until}`)),
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
  const trucks = detail.truckers.length ? card('', 'Trucking offers', `${answered} of ${detail.truckers.length} received${d.truck_mode === 'estimate' && answered ? ', after the quote had gone out' : ''}`,
    barList(detail.truckers.map(t => ({
      name: t.name, value: t.price_eur || 0, em: t.chosen, text: t.price_eur == null ? 'no offer' : fmt.eurExact(t.price_eur),
      note: t.price_eur == null ? null : `${t.pickup_hours != null ? `pickup within ${fmt.one(t.pickup_hours)} h` : ''}${t.cheapest ? ' · lowest' : ''}`,
      tip: () => tipRows(t.name, [['Offer', t.price_eur == null ? 'none' : fmt.eurExact(t.price_eur)], t.pickup_hours != null && ['Pickup within', `${fmt.one(t.pickup_hours)} h`],
        t.ref && ['Reference', t.ref], t.response_s != null && ['Response time', fmt.duration(t.response_s)], t.chosen && ['Selected', 'yes']]),
    })), { label: 190 })) : null;

  const lane = ctx.snap.lanes.filter(l => l.shipments.includes(d.id));
  const here = ctx.snap.airports[d.destination], from = ctx.snap.airports[d.origin];
  ctx.focusView = here && from ? routeView(from, here) : { lon: 12, lat: 30, zoom: 1 };
  const side = h('div', { class: 'stack c4' },
    lane.some(l => l.origin) ? globeCard(ctx, '', { height: 260, title: `${laneText(d)}${d.distance_km ? ` · ${fmt.km(d.distance_km)}` : ''}`, lanes: lane, compact: true }) : null,
    d.error ? card('', 'Error', null, h('p', { class: 'soft' }, d.error)) : null);

  const node = h('div', { class: 'grid' },
    h('div', { class: 'crumbs' }, h('a', { href: '#/rounds' }, 'Rounds'), '/', h('span', null, `Shipment ${d.id}`)),
    h('div', { class: 'title' }, h('h2', null, account ? h('a', { href: `#/account/${encodeURIComponent(account.email)}` }, d.company) : d.company), stateBadge(d), h('span', { class: 'muted' }, laneText(d)),
      h('span', { class: 'acts right' }, ctx.snap.attention.filter(item => item.shipment_id === d.id).flatMap(item => actionButtons(ctx, item)))),
    h('div', { class: `stack ${side.childElementCount ? 'c8' : 'c12'}` }, card('', 'What happened', `${detail.trace.length} steps`, trace), flights, trucks,
      h('section', { class: 'card glass flush' }, h('details', { class: 'prompt fold' }, h('summary', null, 'Request and offer in full'), h('div', { class: 'foldbody stack' }, request, offer))), h('section', { class: 'card glass flush' }, h('details', { class: 'prompt fold' }, h('summary', null, `Correspondence (${detail.timeline.length} mails)`), h('div', { class: 'foldbody' }, correspondence(detail))))),
    side.childElementCount ? side : null);
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
    { key: 'quoted', label: 'Quoted', right: true, hidden: true }, { key: 'accepted', label: 'Accepted', right: true },
    { key: 'rejected', label: 'Declined', right: true }, { key: 'open', label: 'Open', right: true },
    { key: 'quoted_eur', label: 'Quoted EUR', right: true, hidden: true },
    { key: 'accepted_eur', label: 'Accepted EUR', right: true, render: a => fmt.eur(a.accepted_eur) },
    { key: 'avg_gross_kg', label: 'Avg. kg', right: true, hidden: true },
    { key: 'goals', label: 'Priority', hidden: true, csv: a => priorities(a.goals) },
    { key: 'destinations', label: 'Destinations', sort: false, render: a => codes(a.destinations), csv: a => a.destinations.join(' ') },
    { key: 'mails_in', label: 'Mails', right: true, hidden: true },
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
      { key: 'airports', label: 'Airports', hidden: true, csv: t => t.airports.join(' ') },
      { key: 'zones', label: 'Postal zones', sort: false, render: t => h('span', { class: 'soft num' }, t.zones.join(' ')), cls: 'wrap', csv: t => t.zones.join(' ') },
      { key: 'asked', label: 'Requests', right: true }, { key: 'answered', label: 'Offers', right: true },
      { key: 'answer_rate_pct', label: 'Response rate %', right: true, render: t => (t.answer_rate_pct == null ? '–' : meter(t.answer_rate_pct, fmt.pct(t.answer_rate_pct))) },
      { key: 'cheapest_in', label: 'Lowest offer', right: true, hidden: true }, { key: 'chosen', label: 'Selected', right: true },
      { key: 'avg_price_eur', label: 'Avg. offer EUR', right: true, render: t => fmt.eur(t.avg_price_eur) },
      { key: 'avg_eur_per_100kg', label: '€ per 100 kg', right: true, hidden: true },
      { key: 'median_pickup_hours', label: 'Pickup within h', right: true, render: t => fmt.one(t.median_pickup_hours) },
      live ? { key: 'median_response_s', label: 'Response time', right: true, render: t => fmt.duration(t.median_response_s) } : null,
    ].filter(Boolean);
    sub = `${snap.truckers.length} truckers`;
    body = h('section', { class: 'card glass c12 flush' }, head('Truckers', `${rows.filter(t => t.asked).length} contacted`, exportButton('talos-truckers.csv', rows, cols)),
      table(rows, cols.filter(c => !c.hidden), { state: sortState(ctx, 'truckers', 'asked') }));
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
    card('c6', 'Router', modeName, h('div', { style: { display: 'grid', gap: '12px' } }, h('p', { class: 'soft' }, modeText), h('div', { class: 'facts' },
      fact('Mailbox poller', p.running ? 'running' : p.enabled ? 'enabled, not running' : 'off in this process'), fact('Interval', `${p.interval_seconds} s`),
      fact('Last poll', p.last_poll_at ? `${fmt.when(p.last_poll_at)} UTC` : 'none in this process'), fact('Last mail id read', fmt.int(p.cursor)),
      p.last_error ? fact('Last error', p.last_error) : null))),
    c ? card('c6', 'Classifier accuracy', 'against hand labels, dry run',
          h('div', { style: { display: 'grid', gap: '10px' } }, h('div', { class: 'hero', style: { fontSize: '24px' } }, `${c.correct} of ${c.labelled}`, h('small', null, 'classified as labelled')),
            c.wrong.length ? table(c.wrong, [{ key: 'id', label: 'Mail' }, { key: 'from', label: 'Sender' }, { key: 'category', label: 'Classified as', render: r => category(r.category) }, { key: 'expected', label: 'Label', render: r => category(r.expected) }])
              : h('p', { class: 'soft' }, 'No deviations.')))
      : card('c6', 'Classifier accuracy', null, h('div', { class: 'empty' }, 'Hand labels exist for the recorded dry run only.')),
    h('section', { class: 'card glass c12 flush' }, head('Details', 'credentials are not displayed'),
      h('details', { class: 'prompt' }, h('summary', null, 'Configuration'), kv(sys.settings.map(x => [x.name, x.value]))),
      h('details', { class: 'prompt' }, h('summary', null, 'Data inventory'), kv(sys.inventory.map(x => [x.name, [h('span', { class: 'num' }, fmt.int(x.rows)), h('span', { class: 'sub' }, x.where)]]))),
      sys.prompts.map(pr => h('details', { class: 'prompt' }, h('summary', null, `Prompt: ${pr.name}`), h('pre', null, pr.text)))));
  return { title: 'System', sub: modeName, node };
}
