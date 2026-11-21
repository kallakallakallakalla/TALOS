// The shell: which data is shown, which page, and keeping it current.
// The page asks the server every few seconds for a change token and reloads the figures only
// when the token moves, so it follows the live database without flicker.

import { DEFAULT_VIEW } from './globe.js';
import { fmt, h, icon } from './lib.js';
import * as views from './views.js';

const POLL_MS = 3000;
const NAV = [['now', 'Now'], ['rounds', 'Rounds'], ['tuning', 'Tuning'], ['lookup', 'Lookup'], ['system', 'System']];
const PARENT = { shipment: 'rounds', account: 'lookup' };
const $ = id => document.getElementById(id);
const store = { get: k => { try { return localStorage.getItem(k); } catch { return null; } }, set: (k, v) => { try { localStorage.setItem(k, v); } catch {} } };

const state = {
  source: null, sources: null, snap: null, version: null, health: null, q: '', updatedAt: null, online: true,
  renders: 0, lastRoute: '', globes: [], cache: {},
  ui: {
    sort: {}, pipeline: { state: null, lane: null, account: null, goal: '', truck: '' }, inbox: { category: '', role: '', limit: 100 },
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
  const ctx = { snap: state.snap, sources: state.sources, ui: state.ui, q: state.q.trim().toLowerCase(), api, act, pick, rerender: render, cache: state.cache, globes: [], focusView: null };
  let page;
  try { page = await views[now.name](ctx, now.arg); } catch (error) {
    page = { title: 'Something went wrong', sub: '', node: h('div', { class: 'grid' }, h('div', { class: 'empty glass c12' }, error.message)) };
  }
  if (turn !== state.renders) { ctx.globes.forEach(g => g.destroy()); return; }
  const key = `${now.name}/${now.arg ?? ''}`, scroll = key === state.lastRoute ? view.scrollTop : 0;
  state.globes.forEach(g => g.destroy());
  state.globes = ctx.globes; state.lastRoute = key;
  view.replaceChildren(page.node);
  view.scrollTop = scroll;
  view.classList.remove('stale');
  $('title').textContent = page.title; $('sub').textContent = page.sub;
  document.title = `${page.title} · TALOS`;
  frame(now.name);
}

function frame(active) {
  const snap = state.snap, urgent = snap.attention.length;
  const counts = { now: urgent || '' };
  document.title = `${urgent ? `(${urgent}) ` : ''}${document.title.replace(/^\(\d+\) /, '')}`;
  $('nav').replaceChildren(...NAV.map(([key, label]) => h('a', {
    href: `#/${key}`, class: (PARENT[active] || active) === key ? 'on' : '', 'aria-current': (PARENT[active] || active) === key ? 'page' : null,
  }, icon(key), label, counts[key] !== undefined && h('span', { class: 'count', title: 'cases that need a decision' }, counts[key]))));
  $('source').replaceChildren(
    h('button', { class: state.source === 'live' ? 'on' : '', onclick: () => pick('live') }, 'Live'),
    h('button', { class: state.source === 'replay' ? 'on' : '', disabled: !state.sources.replay.available || null, onclick: () => pick('replay'),
      title: state.sources.replay.available ? 'The recorded mailbox, replayed through the pipeline' : 'No dry run recorded' }, 'Dry run'));
  status();
  $('source-line').textContent = state.source === 'live' ? 'Live database' : 'Recorded dry run';
  $('mode-line').textContent = state.health?.sending === 'on' ? 'Mode: sending and booking' : 'Mode: drafts only';
}

function status() {
  const live = state.source === 'live';
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

async function load() {
  const [snap, health] = await Promise.all([api('/crm/api/snapshot'), fetch('/health').then(r => r.json()).catch(() => null)]);
  announce(snap);
  Object.assign(state, { snap, version: snap.version, health, updatedAt: new Date(), online: true });
  await render();
}

// A new case in the queue is announced, so nobody has to watch the page.
function announce(snap) {
  const known = state.seen, keys = new Set(snap.attention.map(a => a.key));
  state.seen = keys;
  if (!known || snap.source !== 'live' || !('Notification' in window) || Notification.permission !== 'granted') return;
  const fresh = snap.attention.filter(a => !known.has(a.key));
  if (fresh.length) new Notification(fresh.length === 1 ? fresh[0].title : `${fresh.length} new cases need a decision`,
    { body: fresh.length === 1 ? `${fresh[0].company || ''} ${fresh[0].detail}`.trim() : fresh.map(a => a.title).join('\n'), tag: 'talos-queue' });
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
    const response = await fetch(`/crm/api/actions/${name}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(params) });
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
    const { version } = await api('/crm/api/version');
    if (!state.online) { state.online = true; status(); }
    if (version !== state.version) await load();
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
  setInterval(() => { $('clock').textContent = `${fmt.clock(new Date())} UTC`; }, 1000);
  $('clock').textContent = `${fmt.clock(new Date())} UTC`;

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
