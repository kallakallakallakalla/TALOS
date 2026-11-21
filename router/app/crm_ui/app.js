// The shell: which data is shown, which page, and keeping it current.
// The page asks the server every few seconds for a change token and reloads the figures only
// when the token moves, so it follows the live database without flicker.

import { DEFAULT_VIEW } from './globe.js';
import { fmt, h, icon } from './lib.js';
import * as views from './views.js';

const POLL_MS = 3000;
const NAV = [['desk', 'Desk'], ['pipeline', 'Pipeline'], ['accounts', 'Accounts'], ['network', 'Network'], ['carriers', 'Carriers'], ['inbox', 'Inbox'], ['system', 'System']];
const PARENT = { shipment: 'pipeline', account: 'accounts' };
const $ = id => document.getElementById(id);
const store = { get: k => { try { return localStorage.getItem(k); } catch { return null; } }, set: (k, v) => { try { localStorage.setItem(k, v); } catch {} } };

const state = {
  source: null, sources: null, snap: null, version: null, health: null, q: '', updatedAt: null, online: true,
  renders: 0, lastRoute: '', globes: [], cache: {},
  ui: {
    sort: {}, pipeline: { state: null, lane: null, account: null }, inbox: { category: '', role: '', limit: 100 },
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
  return { name: views[name] ? name : 'desk', arg: rest.length ? decodeURIComponent(rest.join('/')) : undefined };
}

async function render() {
  const turn = ++state.renders, view = $('view'), now = route();
  view.classList.add('stale');  // hold the old frame while the new one is prepared
  const ctx = { snap: state.snap, sources: state.sources, ui: state.ui, q: state.q.trim().toLowerCase(), api, pick, rerender: render, cache: state.cache, globes: [], focusView: null };
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
  const snap = state.snap, urgent = snap.attention.filter(a => a.level !== 'info').length;
  const counts = { desk: urgent || '', pipeline: snap.deals.length, accounts: snap.accounts.length, inbox: snap.mail.total };
  $('nav').replaceChildren(...NAV.map(([key, label]) => h('a', {
    href: `#/${key}`, class: (PARENT[active] || active) === key ? 'on' : '', 'aria-current': (PARENT[active] || active) === key ? 'page' : null,
  }, icon(key), label, counts[key] !== undefined && h('span', { class: 'count', title: key === 'desk' ? 'items that need attention' : null }, counts[key]))));
  $('source').replaceChildren(
    h('button', { class: state.source === 'live' ? 'on' : '', onclick: () => pick('live') }, 'Live'),
    h('button', { class: state.source === 'replay' ? 'on' : '', disabled: !state.sources.replay.available || null, onclick: () => pick('replay'),
      title: state.sources.replay.available ? 'The recorded mailbox, replayed through the pipeline' : 'No dry run recorded' }, 'Dry run'));
  const sending = state.health?.sending === 'on';
  $('mode').replaceChildren(h('span', { class: `dot ${sending ? 'warning' : ''}` }), sending ? 'Sending and booking' : 'Drafts only, nothing is sent');
  status();
  $('source-line').textContent = state.source === 'live' ? 'Live database' : 'Recorded dry run';
}

function status() {
  const live = state.source === 'live';
  $('live').replaceChildren(
    h('span', { class: `dot ${!state.online ? 'critical' : live ? 'live' : ''}` }),
    !state.online ? 'Server not reachable, retrying' : live ? `Live · updated ${fmt.clock(state.updatedAt)}` : `Recorded · loaded ${fmt.clock(state.updatedAt)}`);
}

async function load() {
  const [snap, health] = await Promise.all([api('/crm/api/snapshot'), fetch('/health').then(r => r.json()).catch(() => null)]);
  Object.assign(state, { snap, version: snap.version, health, updatedAt: new Date(), online: true });
  await render();
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
  let typing;
  $('search').addEventListener('input', e => { state.q = e.target.value; clearTimeout(typing); typing = setTimeout(render, 180); });
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
