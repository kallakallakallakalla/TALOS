// The shell: navigation, the global search, and keeping every page current.
// The page asks the server every 2 s for a change token and reloads only when it moved,
// so it follows the desk as it works, without flicker. Nothing on these pages changes anything.

import { DEFAULT_VIEW } from './globe.js';
import { fmt, h, icon, statusBadge } from './lib.js';
import * as views from './views.js';

const POLL_MS = 2000;
const NAV = [['today', 'Today'], ['shipments', 'Shipments'], ['customers', 'Customers'], ['carriers', 'Carriers'], ['rates', 'Rates'], ['reports', 'Reports']];
const PARENT = { shipment: 'shipments', customer: 'customers' };
const $ = id => document.getElementById(id);
const store = { get: k => { try { return localStorage.getItem(k); } catch { return null; } }, set: (k, v) => { try { localStorage.setItem(k, v); } catch {} } };

const state = {
  snap: null, version: null, desk: null, online: true, renders: 0, lastRoute: '', globes: [],
  ui: { sort: {}, view: store.get('talos.shipments.view') || 'table', status: '', filter: '', nextFilter: '', carriers: 'airlines', rates: { origin: '', destination: '', airline: '' }, open: new Set(), globe: { ...DEFAULT_VIEW }, coverage: false },
};

async function api(path) {
  const response = await fetch(path, { cache: 'no-store' });
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return response.json();
}

function route() {
  const [name, ...rest] = location.hash.replace(/^#\/?/, '').split('/');
  return { name: views[name] ? name : 'today', arg: rest.length ? decodeURIComponent(rest.join('/')) : undefined };
}

async function render() {
  const turn = ++state.renders, view = $('view'), now = route();
  view.classList.add('stale');
  const ctx = { snap: state.snap, ui: state.ui, api, rerender: render, globes: [], focusView: null, setView: v => { state.ui.view = v; store.set('talos.shipments.view', v); render(); } };
  let page;
  try { page = await views[now.name](ctx, now.arg); } catch (error) {
    page = { title: 'Something went wrong', sub: '', node: h('div', { class: 'grid' }, h('div', { class: 'empty glass c12' }, error.message)) };
  }
  if (turn !== state.renders) { ctx.globes.forEach(g => g.destroy()); return; }
  state.globes.forEach(g => g.destroy()); state.globes = ctx.globes;
  const key = `${now.name}/${now.arg ?? ''}`, scroll = key === state.lastRoute ? view.scrollTop : 0;
  state.lastRoute = key;
  view.replaceChildren(page.node);
  view.scrollTop = scroll;
  view.classList.remove('stale');
  $('title').textContent = page.title; $('sub').textContent = page.sub || '';
  document.title = `${page.title} · TALOS`;
  frame(now.name);
}

function frame(active) {
  const urgent = state.snap.today.needs_you.length;
  document.title = `${urgent ? `(${urgent}) ` : ''}${document.title.replace(/^\(\d+\) /, '')}`;
  const link = ([key, label], extra = '') => h('a', {
    href: `#/${key}`, class: `${(PARENT[active] || active) === key ? 'on' : ''}${extra}`, 'aria-current': (PARENT[active] || active) === key ? 'page' : null,
  }, icon(key), label, key === 'today' && urgent ? h('span', { class: 'count', title: 'shipments that need a person' }, urgent) : null);
  $('nav').replaceChildren(...NAV.map(item => link(item)), link(['settings', 'Settings'], ' rule setup'));
  paintDesk();
}

// The desk's state in one chip: working, idle, or quiet since when. Offline when this server is gone.
function paintDesk() {
  const d = state.online ? state.desk : null;
  const tone = !d ? 'critical' : d.state === 'working' ? 'live' : d.state === 'idle' ? 'good' : 'quiet';
  $('deskstate').replaceChildren(h('span', { class: `dot ${tone}` }), !d ? 'CRM server not reachable' : d.label);
}

async function load() {
  const snap = await api('/crm/api/snapshot');
  Object.assign(state, { snap, version: snap.version, desk: snap.desk, online: true });
  await render();
}

async function poll() {
  if (document.hidden || !state.snap) return;
  try {
    const { version, desk, build } = await api('/crm/api/version');
    state.desk = desk; $('build').textContent = `Build ${build}`;
    if (!state.online) { state.online = true; document.getElementById('offline')?.remove(); }
    paintDesk();
    if (version !== state.version) await load();
  } catch {
    state.online = false; paintDesk();
    if (!document.getElementById('offline')) $('view').prepend(h('div', { class: 'notice glass offline', id: 'offline' }, icon('alert'), h('div', null,
      'The CRM server on this computer is not running, so this page is no longer updated. Start it again: ', h('code', null, 'bash router/crm.sh'), '. The page reconnects by itself.')));
  }
}

// --- global search: shipments and customers from the page's data, mails from the server -------

let searchTurn = 0;
async function search(text) {
  const box = $('results'), q = text.trim().toLowerCase(), turn = ++searchTurn;
  if (!q) { box.hidden = true; return; }
  const has = (...fields) => fields.some(f => String(f ?? '').toLowerCase().includes(q));
  const ships = state.snap.shipments.filter(s => has(s.id, s.company, s.customer, s.booking, s.lane, s.subject, s.airline)).slice(0, 6);
  const custs = state.snap.customers.filter(c => has(c.company, c.email)).slice(0, 4);
  let mails = [];
  if (q.length >= 3) { try { mails = (await api(`/crm/api/search?q=${encodeURIComponent(q)}`)).mails.slice(0, 6); } catch {} }
  if (turn !== searchTurn) return;
  const go = href => () => { location.hash = href; box.hidden = true; $('search').blur(); };
  const group = (title, rows) => rows.length ? [h('div', { class: 'rhead' }, title), rows] : null;
  const groups = [
    group('Shipments', ships.map(s => h('button', { class: 'ritem', onclick: go(`#/shipment/${s.id}`) },
      h('b', null, `${s.id} · ${s.company}`), h('span', null, [s.lane, s.booking].filter(Boolean).join(' · ')), statusBadge(s)))),
    group('Customers', custs.map(c => h('button', { class: 'ritem', onclick: go(`#/customer/${encodeURIComponent(c.email)}`) },
      h('b', null, c.company), h('span', null, `${c.email} · ${c.shipments} shipments`)))),
    group('Mails', mails.map(m => h('button', { class: 'ritem', onclick: m.shipment ? go(`#/shipment/${m.shipment}`) : null, disabled: !m.shipment || null,
      title: m.shipment ? null : 'This mail belongs to no shipment' },
      h('b', null, m.subject || '(no subject)'), h('span', null, `${m.from} · ${fmt.when(m.at)}${m.shipment ? ` · shipment ${m.shipment}` : ''}`)))),
  ].filter(Boolean);
  box.replaceChildren(...(groups.length ? groups.flat(Infinity) : [h('div', { class: 'empty' }, `Nothing found for “${text.trim()}”.`)]));
  box.hidden = false;
}

function start() {
  $('search-icon').replaceWith(icon('search'));
  const themed = () => document.documentElement.dataset.theme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  const paintTheme = () => $('theme').replaceChildren(icon(themed() === 'dark' ? 'sun' : 'moon'));
  $('theme').addEventListener('click', () => {
    const next = themed() === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next; store.set('talos.theme', next); paintTheme(); document.dispatchEvent(new Event('themechange'));
  });
  paintTheme();
  let typing;
  $('search').addEventListener('input', e => { clearTimeout(typing); typing = setTimeout(() => search(e.target.value), 180); });
  $('search').addEventListener('focus', e => { if (e.target.value.trim()) search(e.target.value); });
  addEventListener('click', e => { if (!$('gsearch').contains(e.target)) $('results').hidden = true; });
  addEventListener('keydown', e => {
    const typingElsewhere = /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement?.tagName || '');
    if (e.key === '/' && !typingElsewhere) { e.preventDefault(); $('search').focus(); }
    else if (e.key === 'Escape' && document.activeElement === $('search')) { $('search').value = ''; $('results').hidden = true; $('search').blur(); }
  });
  addEventListener('hashchange', () => { $('results').hidden = true; state.ui.filter = state.ui.nextFilter; state.ui.nextFilter = ''; render(); });
  document.addEventListener('visibilitychange', poll);
  load().then(poll).catch(error => {
    $('view').replaceChildren(h('div', { class: 'grid' }, h('div', { class: 'empty glass c12' }, `Could not load the data: ${error.message}`)));
  });
  setInterval(poll, POLL_MS);
}

start();
