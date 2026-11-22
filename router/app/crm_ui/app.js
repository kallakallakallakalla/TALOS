// The shell: which data is shown, which page, and keeping it current.
// The page asks the server every few seconds for a change token and reloads the figures only
// when the token moves, so it follows the live database without flicker.

import { DEFAULT_VIEW } from './globe.js';
import { fmt, h, icon } from './lib.js';
import * as views from './views.js';

const POLL_MS = 3000;
const NAV = [['now', 'Now'], ['sessions', 'Sessions'], ['performance', 'Performance'], ['rounds', 'Rounds'], ['lookup', 'Lookup'], ['system', 'System']];
const PARENT = { shipment: 'rounds', account: 'lookup' };
const $ = id => document.getElementById(id);
const store = { get: k => { try { return localStorage.getItem(k); } catch { return null; } }, set: (k, v) => { try { localStorage.setItem(k, v); } catch {} } };

const state = {
  source: null, sources: null, snap: null, version: null, health: null, q: '', updatedAt: null, online: true,
  renders: 0, lastRoute: '', globes: [], leave: [], cache: {},
  ui: {
    sort: {}, rounds: { outcome: null, lane: null, all: false }, inbox: { category: '', role: '', limit: 100 },
    tariffs: { origin: '', destination: '', airline: '' }, openMails: new Set(), globe: { ...DEFAULT_VIEW }, coverage: false,
  },
};

async function api(path, withSource = true) {
  const url = withSource ? `${path}${path.includes('?') ? '&' : '?'}source=${state.source}` : path;
  const response = await fetch(url, { cache: 'no-store' });
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return response.json();
}

function route() {
  const [name, ...rest] = location.hash.replace(/^#\/?/, '').split('/');
  return { name: views[name] ? name : 'now', arg: rest.length ? decodeURIComponent(rest.join('/')) : undefined };
}

async function render() {
  const turn = ++state.renders, view = $('view'), now = route();
  view.classList.add('stale');  // hold the old frame while the new one is prepared
  const ctx = { snap: state.snap, sources: state.sources, ui: state.ui, q: state.q.trim().toLowerCase(), api, act, pick, rerender: render, cache: state.cache, globes: [], focusView: null, leave: [] };
  ctx.onLeave = undo => ctx.leave.push(undo);  // a page that opens a stream closes it when the page goes
  let page;
  try { page = await views[now.name](ctx, now.arg); } catch (error) {
    page = { title: 'Something went wrong', sub: '', node: h('div', { class: 'grid' }, h('div', { class: 'empty glass c12' }, error.message)) };
  }
  if (turn !== state.renders) { ctx.globes.forEach(g => g.destroy()); ctx.leave.forEach(undo => undo()); return; }
  const key = `${now.name}/${now.arg ?? ''}`, scroll = key === state.lastRoute ? view.scrollTop : 0;
  state.globes.forEach(g => g.destroy()); state.leave.forEach(undo => undo());
  state.globes = ctx.globes; state.leave = ctx.leave; state.lastRoute = key;
  view.replaceChildren(page.node);
  view.scrollTop = scroll;
  view.classList.remove('stale');
  $('title').textContent = page.title; $('sub').textContent = page.sub;
  $('health').hidden = !['now', 'system'].includes(now.name);  // the status bar belongs to the two pages about the system
  // the search box only exists where it filters something
  $('search').closest('label').hidden = !(now.name === 'lookup' || (now.name === 'rounds' && now.arg !== 'mails'));
  document.title = `${page.title} · TALOS`;
  frame(now.name);
}

function frame(active) {
  const snap = state.snap, urgent = snap.attention.filter(a => a.needs_person).length;
  const counts = { now: urgent || '' };
  document.title = `${urgent ? `(${urgent}) ` : ''}${document.title.replace(/^\(\d+\) /, '')}`;
  $('nav').replaceChildren(...NAV.map(([key, label]) => h('a', {
    href: `#/${key}`, class: `${(PARENT[active] || active) === key ? 'on' : ''}${key === 'system' ? ' rule setup' : ''}`, 'aria-current': (PARENT[active] || active) === key ? 'page' : null,
  }, icon(key), label, counts[key] !== undefined && h('span', { class: 'count', title: 'cases that need a decision' }, counts[key]))));
  $('source').replaceChildren(
    h('button', { class: state.source === 'live' ? 'on' : '', onclick: () => pick('live') }, 'Live'),
    h('button', { class: state.source === 'replay' ? 'on' : '', disabled: !state.sources.replay.available || null, onclick: () => pick('replay'),
      title: state.sources.replay.available ? 'The recorded mailbox, replayed through the pipeline' : 'No dry run recorded' }, 'Dry run'));
  status();
}

// The status bar: this process and what it depends on. One glance says whether the system is up.
const uptime = s => (s < 90 ? `${s} s` : s < 5400 ? `${Math.round(s / 60)} min` : s < 172800 ? `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min` : `${Math.floor(s / 86400)} d ${Math.floor((s % 86400) / 3600)} h`);

// The status bar: an overall state, then one strip of ticks per part of the system. It is
// rebuilt only when something changed, so a tick under the pointer stays put.
function paintHealth() {
  const x = state.online ? state.health : null, bar = $('health');
  const signature = JSON.stringify(x ? [x.state, x.label, x.mode, x.strips] : 'offline');
  if (signature !== bar.dataset.signature) {
    bar.dataset.signature = signature;
    const tone = { operational: '', degraded: 'warning', down: 'critical', viewer: '' }[x?.state] ?? 'critical';
    const strips = (x?.strips || []).map(strip => h('span', { class: `hstrip ${strip.state}` }, h('b', null, strip.name),
      h('span', { class: 'ticks' }, strip.ticks.map(t => h('i', {
        class: `tick ${t.tone}${t.href ? ' link' : ''}`, title: t.tip || null, onclick: t.href ? () => { location.hash = t.href; } : null,
      }))),
      strip.text && h('small', null, strip.text)));
    bar.replaceChildren(
      h('span', { class: `hstate ${tone}`, title: x?.detail || null }, x ? x.label : 'Router not reachable', h('small', { id: 'uptime' })),
      h('span', { class: 'hstrips' }, strips),  // strips that do not fit are left out, never wrapped
      h('span', { class: `hmode${x?.mode === 'sending' ? ' hot' : ''}` }, !x ? '' : x.mode === 'sending' ? 'Sending on' : x.mode === 'test' ? 'Test mode' : 'Drafts only'),
      h('b', { id: 'clock', class: 'hclock' }, `${fmt.clock(new Date())} UTC`));
  }
  if (x) { $('uptime').textContent = `up ${uptime(x.uptime_s)}`; $('build').textContent = `Build ${x.build}`; }
}

function status() {
  paintHealth();
  const live = state.source === 'live';
  $('live').hidden = state.online && !live;  // the switch beside it already says "Dry run"
  $('live').replaceChildren(
    h('span', { class: `dot ${!state.online ? 'critical' : live ? 'live' : ''}` }),
    !state.online ? 'Server not reachable' : live ? `Live · updated ${fmt.clock(state.updatedAt)}` : 'Dry run data');
  // say what to do, not only that something is wrong
  const view = $('view'), note = document.getElementById('offline');
  if (state.online) note?.remove();
  else if (!note) view.prepend(h('div', { class: 'notice glass offline', id: 'offline' }, icon('alert'), h('div', null,
    'The application on this computer is not running, so the figures below are no longer updated. Start it again in a terminal: ',
    h('code', null, 'bash router/crm.sh'), '. This page reconnects by itself.')));
}

async function load(quiet = false) {
  const [snap, health] = await Promise.all([api('/crm/api/snapshot'), api('/crm/api/version').then(v => v.health).catch(() => null)]);
  announce(snap);
  Object.assign(state, { snap, version: snap.version, health, updatedAt: new Date(), online: true });
  // The Sessions page is fed by its own stream; rebuilding it on every change would cut that off.
  if (quiet && route().name === 'sessions') { frame('sessions'); return; }
  await render();
}

// A new case in the queue is announced, so nobody has to watch the page.
function announce(snap) {
  const cases = snap.attention.filter(a => a.needs_person);
  const known = state.seen, keys = new Set(cases.map(a => a.key));
  state.seen = keys;
  if (!known || snap.source !== 'live' || !('Notification' in window) || Notification.permission !== 'granted') return;
  const fresh = cases.filter(a => !known.has(a.key));
  if (fresh.length) new Notification(fresh.length === 1 ? fresh[0].title : `${fresh.length} new cases need a decision`,
    { body: fresh.length === 1 ? fresh[0].detail : fresh.map(a => a.title).join('\n'), tag: 'talos-queue' });
}

function toast(text, bad = false) {
  const el = $('toast');
  el.textContent = text; el.className = `on${bad ? ' bad' : ''}`;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => { el.className = ''; }, 6000);
}

// Carry out an operator action: ask first when it changes something, then show what happened.
async function act(name, params, question) {
  if (question && !confirm(question)) return;
  try {
    const response = await fetch(`/crm/api/actions/${name}?source=${state.source}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(params) });
    const answer = await response.json();
    if (!response.ok) throw new Error(answer.detail || response.statusText);
    toast(answer.message);
  } catch (error) { toast(error.message, true); }
  await load();
}

async function pick(source) {
  if (source === state.source) return;
  state.source = source; store.set('talos.source', source);
  $('view').classList.add('stale');
  await load();
}

async function poll() {
  if (document.hidden || !state.snap) return;
  try {
    const { version, health } = await api('/crm/api/version');
    state.health = health; paintHealth();
    if (!state.online) { state.online = true; status(); }
    if (version !== state.version) await load(true);
  } catch { state.online = false; status(); }
}

async function start() {
  $('search-icon').replaceWith(icon('search'));
  const themed = () => document.documentElement.dataset.theme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  const paintTheme = () => $('theme').replaceChildren(icon(themed() === 'dark' ? 'sun' : 'moon'));
  $('theme').addEventListener('click', () => {
    const next = themed() === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next; store.set('talos.theme', next);
    paintTheme(); document.dispatchEvent(new Event('themechange'));
  });
  paintTheme();
  const paintBell = () => {
    const state = 'Notification' in window ? Notification.permission : 'unsupported';
    $('bell').replaceChildren(icon(state === 'granted' ? 'bell' : 'bell-off'));
    $('bell').title = state === 'granted' ? 'New cases are announced by a notification' : state === 'denied' ? 'Notifications are blocked in the browser settings' : 'Announce new cases by a notification';
  };
  $('bell').addEventListener('click', async () => { if ('Notification' in window && Notification.permission === 'default') await Notification.requestPermission(); paintBell(); });
  paintBell();
  let typing;
  $('search').addEventListener('input', e => { state.q = e.target.value; clearTimeout(typing); typing = setTimeout(render, 180); });
  // "/" jumps to the search box, Escape clears it
  addEventListener('keydown', e => {
    const typingElsewhere = /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement?.tagName || '');
    if (e.key === '/' && !typingElsewhere) { e.preventDefault(); $('search').focus(); }
    else if (e.key === 'Escape' && document.activeElement === $('search')) { $('search').value = state.q = ''; $('search').blur(); render(); }
  });
  setInterval(() => { const clock = $('clock'); if (clock) clock.textContent = `${fmt.clock(new Date())} UTC`; }, 1000);

  try {
    state.sources = await api('/crm/api/sources', false);
    const stored = store.get('talos.source');
    state.source = stored === 'live' || (stored === 'replay' && state.sources.replay.available) ? stored : state.sources.default;
    await load();
  } catch (error) {
    $('view').replaceChildren(h('div', { class: 'grid' }, h('div', { class: 'empty glass c12' }, `Could not load the data: ${error.message}`)));
    return;
  }
  addEventListener('hashchange', () => { $('search').value = state.q = ''; render(); });
  document.addEventListener('visibilitychange', poll);
  setInterval(poll, POLL_MS);
}

start();
