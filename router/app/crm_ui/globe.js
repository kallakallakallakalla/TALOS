// A globe drawn on a canvas: orthographic projection of the sphere, so dragging turns a real
// 3D model. The sphere is see-through like glass: land and routes on the far side show faintly.
// Routes are true great circles between the airports, lifted a little off the surface.

import LAND from './land.js';
import { fmt, h, hideTip, showTip, tipRows } from './lib.js';

const RAD = Math.PI / 180;
const unit = (lon, lat) => [Math.cos(lat * RAD) * Math.cos(lon * RAD), Math.cos(lat * RAD) * Math.sin(lon * RAD), Math.sin(lat * RAD)];
const wrap = lon => ((((lon + 180) % 360) + 360) % 360) - 180;

const RINGS = LAND.map(flat => {
  const n = flat.length / 2, v = new Float32Array(n * 3);
  for (let i = 0; i < n; i++) v.set(unit(flat[2 * i], flat[2 * i + 1]), 3 * i);
  return { flat, v, n };
});

const GRATICULE = [];
for (let lon = -180; lon < 180; lon += 30) GRATICULE.push(Array.from({ length: 41 }, (_, i) => unit(lon, -80 + 4 * i)));
for (let lat = -60; lat <= 60; lat += 30) GRATICULE.push(Array.from({ length: 91 }, (_, i) => unit(-180 + 4 * i, lat)));

// Is the point inside the ring? Rings are cut at the antimeridian, so plain lon/lat works.
function inside(flat, lon, lat) {
  let hit = false;
  for (let i = 0, j = flat.length - 2; i < flat.length; j = i, i += 2) {
    const [xi, yi, xj, yj] = [flat[i], flat[i + 1], flat[j], flat[j + 1]];
    if ((yi > lat) !== (yj > lat) && lon < ((xj - xi) * (lat - yi)) / (yj - yi) + xi) hit = !hit;
  }
  return hit;
}

// Points along the great circle from a to b; longer routes rise higher above the surface.
function greatCircle(a, b, steps = 72) {
  const omega = Math.acos(Math.max(-1, Math.min(1, a[0] * b[0] + a[1] * b[1] + a[2] * b[2])));
  const so = Math.sin(omega) || 1e-9, peak = 0.03 + 0.12 * (omega / Math.PI);
  return Array.from({ length: steps + 1 }, (_, i) => {
    const t = i / steps, k1 = Math.sin((1 - t) * omega) / so, k2 = Math.sin(t * omega) / so;
    const lift = 1 + peak * Math.sin(Math.PI * t);
    return [(k1 * a[0] + k2 * b[0]) * lift, (k1 * a[1] + k2 * b[1]) * lift, (k1 * a[2] + k2 * b[2]) * lift];
  });
}

export const REGIONS = { 'Europe & Africa': [12, 30], Americas: [-80, 18], 'Asia-Pacific': [108, 18] };
export const DEFAULT_VIEW = { lon: 12, lat: 30, zoom: 1 };

// A view that looks straight at the middle of a route and is zoomed to hold both ends.
export function routeView(a, b) {
  const [u, v] = [unit(a.lon, a.lat), unit(b.lon, b.lat)];
  const m = [u[0] + v[0], u[1] + v[1], u[2] + v[2]], len = Math.hypot(...m) || 1;
  const half = Math.acos(Math.max(-1, Math.min(1, u[0] * v[0] + u[1] * v[1] + u[2] * v[2]))) / 2;
  return {
    lon: Math.atan2(m[1], m[0]) / RAD, lat: Math.asin(m[2] / len) / RAD,
    zoom: Math.max(1, Math.min(2.2, 0.8 / Math.sin(Math.min(Math.PI / 2, half + 0.12)))),
  };
}

/**
 * host: element to fill. options:
 *   airports {code: {city, lat, lon, origin, used}}, lanes (snapshot.lanes), deals (snapshot.deals),
 *   airlines (snapshot.airlines, for "served by"), view {lon, lat, zoom} (kept by the caller),
 *   height, coverage (show every served destination), compact (no controls), onLane(lane)
 */
export function createGlobe(host, options) {
  const { airports, lanes, deals = [], airlines = [], view, height = 420, compact = false, onLane } = options;
  let coverage = !!options.coverage, hover = null, drag = null, frame = 0, alive = true;
  const canvas = h('canvas', { tabindex: 0, role: 'img', style: { height: `${height}px` },
    'aria-label': `Globe with ${lanes.filter(l => l.origin).length} routes. Drag to turn, arrow keys to turn, plus and minus to zoom. The same figures are in the lanes table.` });
  const ctx = canvas.getContext('2d');
  const controls = compact ? null : h('div', { class: 'ctl' },
    h('button', { title: 'Zoom in', onclick: () => zoom(1.25) }, '+'),
    h('button', { title: 'Zoom out', onclick: () => zoom(0.8) }, '−'),
    h('button', { title: 'Reset the view', onclick: () => { Object.assign(view, DEFAULT_VIEW); draw(); } }, '⟲'));
  host.classList.add('globe');
  host.replaceChildren(canvas, ...[controls].filter(Boolean));

  const dealById = new Map(deals.map(d => [d.id, d]));
  const servedBy = code => airlines.filter(a => a.serves.includes(code)).length;
  const routes = lanes.filter(l => l.origin && airports[l.origin] && airports[l.destination]).map(l => {
    const a = airports[l.origin], b = airports[l.destination];
    const group = l.in_transit.length ? 'transit' : l.accepted ? 'booked' : l.open ? 'open' : 'declined';
    return { lane: l, group, points: greatCircle(unit(a.lon, a.lat), unit(b.lon, b.lat)) };
  });
  const requestsTo = {}, unknownOrigin = new Set(), onRoute = new Set();
  for (const l of lanes) {
    requestsTo[l.destination] = (requestsTo[l.destination] || 0) + l.requests;
    if (l.origin) { onRoute.add(l.origin); onRoute.add(l.destination); } else unknownOrigin.add(l.destination);
  }
  // only the airports of the lanes shown count as "used", so a single-route globe stays clean
  const places = Object.entries(airports).map(([code, a]) => ({ code, ...a, used: onRoute.has(code), v: unit(a.lon, a.lat), requests: requestsTo[code] || 0 }));
  const usedOrigins = places.filter(p => p.origin && p.used);
  let hits = [], dots = [];

  // Share of the planned transit that has passed, by date: we have no aircraft positions.
  function progress(id) {
    const d = dealById.get(id);
    if (!d?.estimated_departure || !d?.estimated_arrival) return null;
    const [from, to] = [Date.parse(d.estimated_departure), Date.parse(d.estimated_arrival) + 864e5];
    return Math.max(0.04, Math.min(0.96, (Date.now() - from) / (to - from)));
  }

  function zoom(factor) { view.zoom = Math.max(0.8, Math.min(3.2, view.zoom * factor)); draw(); }

  function draw() {
    if (!alive) return;
    const css = getComputedStyle(host), color = name => css.getPropertyValue(name).trim();
    const dpr = window.devicePixelRatio || 1, w = canvas.clientWidth, hgt = canvas.clientHeight;
    if (!w || !hgt) return;
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(hgt * dpr)) {
      canvas.width = Math.round(w * dpr); canvas.height = Math.round(hgt * dpr);
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, hgt);
    const R = (Math.min(w, hgt) / 2) * 0.86 * view.zoom, cx = w / 2, cy = hgt / 2;
    const cl = Math.cos(view.lon * RAD), sl = Math.sin(view.lon * RAD), cp = Math.cos(view.lat * RAD), sp = Math.sin(view.lat * RAD);
    // world vector -> [right, up, towards the viewer]
    const P = (x, y, z) => { const x1 = x * cl + y * sl; return [-x * sl + y * cl, cp * z - sp * x1, sp * z + cp * x1]; };
    const sx = x => cx + R * x, sy = y => cy - R * y;
    const ink = color('--ink'), ink3 = color('--ink-3'), accent = color('--series-1'), off = color('--globe-off'), sea = color('--globe-sea-a');

    // the sphere: a matte ball, lit softly from the upper left
    const shade = ctx.createRadialGradient(cx - R * 0.38, cy - R * 0.42, R * 0.05, cx, cy, R);
    shade.addColorStop(0, sea); shade.addColorStop(1, color('--globe-sea-b'));
    ctx.beginPath(); ctx.arc(cx, cy, R, 0, 2 * Math.PI); ctx.fillStyle = shade; ctx.fill();

    ctx.save();
    ctx.beginPath(); ctx.arc(cx, cy, R, 0, 2 * Math.PI); ctx.clip();

    // Land of one hemisphere. Points on the other side are pushed out to the rim, which
    // closes each outline along the horizon. When the point opposite the hemisphere's
    // centre lies inside a ring, that push runs around the wrong way; the extra full
    // circle with the even-odd rule turns the fill right side out again.
    const land = front => {
      const [tlon, tlat] = front ? [wrap(view.lon + 180), -view.lat] : [wrap(view.lon), view.lat];
      ctx.fillStyle = color(front ? '--globe-land' : '--globe-land-back');
      for (const ring of RINGS) {
        let seen = false;
        ctx.beginPath();
        for (let i = 0; i < ring.n; i++) {
          let [x, y, z] = P(ring.v[3 * i], ring.v[3 * i + 1], ring.v[3 * i + 2]);
          if (front ? z >= 0 : z < 0) seen = true;
          else { const m = Math.hypot(x, y) || 1e-9; x /= m; y /= m; }
          if (i) ctx.lineTo(sx(x), sy(y)); else ctx.moveTo(sx(x), sy(y));
        }
        if (!seen) continue;
        ctx.closePath();
        if (inside(ring.flat, tlon, tlat)) { ctx.moveTo(cx + R, cy); ctx.arc(cx, cy, R, 0, 2 * Math.PI); }
        ctx.fill('evenodd');
      }
    };
    land(false);

    ctx.strokeStyle = color('--globe-line'); ctx.lineWidth = 1; ctx.beginPath();
    for (const line of GRATICULE) {
      let pen = false;
      for (const v of line) {
        const [x, y, z] = P(...v);
        if (z < 0) { pen = false; continue; }
        if (pen) ctx.lineTo(sx(x), sy(y)); else ctx.moveTo(sx(x), sy(y));
        pen = true;
      }
    }
    ctx.stroke();
    land(true);
    ctx.restore();

    const rim = ctx.createLinearGradient(cx - R, cy - R, cx + R, cy + R);
    rim.addColorStop(0, color('--globe-rim')); rim.addColorStop(1, 'rgba(128, 128, 128, 0.08)');
    ctx.beginPath(); ctx.arc(cx, cy, R, 0, 2 * Math.PI); ctx.strokeStyle = rim; ctx.lineWidth = 1.25; ctx.stroke();

    // routes: far side first and faint, then the near side
    hits = [];
    const strokeRoute = (route, front) => {
      const on = hover?.lane === route.lane;
      ctx.beginPath();
      let pen = false;
      route.points.forEach((v, i) => {
        const [x, y, z] = P(...v);
        const hidden = z < 0 && Math.hypot(x, y) < 1;
        if (hidden === front) { pen = false; return; }
        if (pen) ctx.lineTo(sx(x), sy(y)); else ctx.moveTo(sx(x), sy(y));
        pen = true;
        if (front) hits.push({ x: sx(x), y: sy(y), route, i });
      });
      ctx.strokeStyle = route.group === 'declined' ? off : accent;
      ctx.lineWidth = (route.group === 'declined' ? 1.3 : 1.6 + Math.min(2.4, 0.7 * (route.lane.requests - 1))) + (on ? 1.2 : 0);
      ctx.setLineDash(route.group === 'open' ? [5, 4] : []);
      ctx.globalAlpha = front ? (hover && !on ? 0.45 : 1) : 0.22;
      ctx.lineCap = 'round'; ctx.lineJoin = 'round';
      ctx.stroke();
      ctx.setLineDash([]); ctx.globalAlpha = 1;
    };
    routes.forEach(r => strokeRoute(r, false));
    routes.forEach(r => strokeRoute(r, true));

    // airports
    dots = [];
    const labels = [];
    ctx.font = '600 11px system-ui, -apple-system, "Segoe UI", sans-serif';
    for (const p of places.sort((a, b) => a.requests - b.requests)) {
      const active = p.used || unknownOrigin.has(p.code);
      if (!active && !coverage) continue;
      const [x, y, z] = P(...p.v), px = sx(x), py = sy(y);
      ctx.globalAlpha = z < 0 ? 0.25 : 1;
      if (!active) {
        ctx.beginPath(); ctx.arc(px, py, 1.8, 0, 2 * Math.PI); ctx.fillStyle = ink3; ctx.fill();
      } else if (p.origin) {
        ctx.beginPath(); ctx.arc(px, py, 3.6, 0, 2 * Math.PI); ctx.fillStyle = sea; ctx.fill();
        ctx.strokeStyle = ink; ctx.lineWidth = 1.6; ctx.stroke();
      } else {
        const r = 3 + Math.min(3, p.requests - 1);
        ctx.beginPath(); ctx.arc(px, py, r + 2, 0, 2 * Math.PI); ctx.fillStyle = sea; ctx.fill();  // surface ring
        ctx.beginPath(); ctx.arc(px, py, r, 0, 2 * Math.PI);
        if (p.used) { ctx.fillStyle = ink; ctx.fill(); }
        else { ctx.strokeStyle = ink; ctx.lineWidth = 1.4; ctx.setLineDash([2, 2]); ctx.stroke(); ctx.setLineDash([]); }
      }
      ctx.globalAlpha = 1;
      if (z >= 0) {
        dots.push({ x: px, y: py, place: p });
        if (active && !(p.origin && usedOrigins.length > 1 && view.zoom < 1.9)) {
          // a label only where it does not run into one already placed
          const tw = ctx.measureText(p.code).width;
          const spot = [[px + 8, py + 4], [px - 8 - tw, py + 4], [px - tw / 2, py - 9], [px - tw / 2, py + 17]]
            .find(([lx, ly]) => !labels.some(b => lx < b.x + b.w + 3 && lx + tw + 3 > b.x && ly - 11 < b.y + 2 && ly + 2 > b.y - 11));
          if (spot) { labels.push({ x: spot[0], y: spot[1], w: tw }); ctx.fillStyle = ink; ctx.fillText(p.code, spot[0], spot[1]); }
        }
      }
    }

    if (usedOrigins.length > 1 && view.zoom < 1.9) {
      const c = usedOrigins.reduce((m, p) => [m[0] + p.v[0], m[1] + p.v[1], m[2] + p.v[2]], [0, 0, 0]);
      const [x, y, z] = P(...c.map(k => k / Math.hypot(...c)));
      if (z >= 0) {
        const text = `Germany · ${usedOrigins.length} airports`, tw = ctx.measureText(text).width;
        ctx.fillStyle = ink; ctx.fillText(text, sx(x) - tw / 2, sy(y) - 12);
      }
    }

    // shipments in transit, placed by their planned dates
    let moving = false;
    for (const route of routes) {
      for (const id of route.lane.in_transit) {
        const share = progress(id);
        if (share == null) continue;
        const [x, y, z] = P(...route.points[Math.round(share * (route.points.length - 1))]);
        if (z < 0 && Math.hypot(x, y) < 1) continue;
        moving = true;
        const beat = matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : (performance.now() % 2400) / 2400;
        ctx.beginPath(); ctx.arc(sx(x), sy(y), 5 + 9 * beat, 0, 2 * Math.PI);
        ctx.strokeStyle = accent; ctx.globalAlpha = 0.5 * (1 - beat); ctx.lineWidth = 1.5; ctx.stroke(); ctx.globalAlpha = 1;
        ctx.beginPath(); ctx.arc(sx(x), sy(y), 6.5, 0, 2 * Math.PI); ctx.fillStyle = sea; ctx.fill();
        ctx.beginPath(); ctx.arc(sx(x), sy(y), 4.5, 0, 2 * Math.PI); ctx.fillStyle = accent; ctx.fill();
        dots.push({ x: sx(x), y: sy(y), transit: { id, share, route } });
      }
    }
    cancelAnimationFrame(frame);
    if (moving && !matchMedia('(prefers-reduced-motion: reduce)').matches && !document.hidden) frame = requestAnimationFrame(draw);
  }

  function pick(event) {
    const box = canvas.getBoundingClientRect(), x = event.clientX - box.left, y = event.clientY - box.top;
    const near = (list, limit) => list.reduce((best, p) => { const d = Math.hypot(p.x - x, p.y - y); return d < limit && (!best || d < best.d) ? { ...p, d } : best; }, null);
    const dot = near(dots, 11);
    if (dot) return dot;
    const hit = near(hits, 9);
    return hit ? { lane: hit.route.lane } : null;
  }

  function describe(target) {
    if (target.transit) {
      const d = dealById.get(target.transit.id), l = target.transit.route.lane;
      return tipRows(`In transit: ${d.company}`, [['Route', `${l.origin} → ${l.destination}`], ['Airline', d.airline],
        ['Planned', `${fmt.day(d.estimated_departure)} to ${fmt.day(d.estimated_arrival)}`],
        ['Position', `${Math.round(100 * target.transit.share)}% of the planned time`]]);
    }
    if (target.place) {
      const p = target.place;
      return tipRows(`${p.code} · ${p.city}`, p.origin
        ? [['Role', 'Departure airport'], ['Routes from here', fmt.int(lanes.filter(l => l.origin === p.code).length)]]
        : [['Requests', fmt.int(p.requests)], ['Served by', `${servedBy(p.code)} airlines`],
           unknownOrigin.has(p.code) && ['Note', 'a request without a known origin']]);
    }
    const l = target.lane;
    return tipRows(`${l.origin} → ${l.destination}`, [
      ['Route', `${airports[l.origin].city} to ${airports[l.destination].city}`], ['Distance', fmt.km(l.distance_km)],
      ['Requests', fmt.int(l.requests)], ['Accepted', fmt.int(l.accepted)], ['Declined', fmt.int(l.rejected)], ['Open', fmt.int(l.open)],
      l.avg_all_in_eur != null && ['Average all-in', fmt.eur(l.avg_all_in_eur)],
      l.best_airline && ['Lowest rate', `${l.best_airline}, ${fmt.two(l.best_eur_per_kg)} €/kg`],
      l.fastest_days != null && ['Fastest', fmt.days(l.fastest_days)]]);
  }

  canvas.addEventListener('pointerdown', e => {
    drag = { x: e.clientX, y: e.clientY, lon: view.lon, lat: view.lat, moved: false };
    canvas.setPointerCapture(e.pointerId); hideTip();
  });
  canvas.addEventListener('pointermove', e => {
    if (drag) {
      const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
      if (Math.abs(dx) + Math.abs(dy) > 4) drag.moved = true;
      const R = (Math.min(canvas.clientWidth, canvas.clientHeight) / 2) * 0.86 * view.zoom;
      view.lon = wrap(drag.lon - (dx / R) / RAD);
      view.lat = Math.max(-85, Math.min(85, drag.lat + (dy / R) / RAD));
      draw();
      return;
    }
    const target = pick(e), was = hover;
    hover = target?.lane ? target : null;
    canvas.style.cursor = target ? 'pointer' : '';
    if (target) showTip(describe(target), e.clientX, e.clientY); else hideTip();
    if (hover?.lane !== was?.lane) draw();
  });
  canvas.addEventListener('pointerup', e => {
    const moved = drag?.moved;
    drag = null;
    const target = moved ? null : pick(e);
    if (target?.lane && onLane) onLane(target.lane);
  });
  canvas.addEventListener('pointerleave', () => { if (hover) { hover = null; draw(); } hideTip(); });
  canvas.addEventListener('wheel', e => {
    if (!e.ctrlKey && !e.metaKey) return;  // plain scrolling stays with the page
    e.preventDefault(); zoom(e.deltaY < 0 ? 1.12 : 0.89);
  }, { passive: false });
  canvas.addEventListener('keydown', e => {
    const step = { ArrowLeft: [-10, 0], ArrowRight: [10, 0], ArrowUp: [0, 10], ArrowDown: [0, -10] }[e.key];
    if (step) { view.lon = wrap(view.lon + step[0]); view.lat = Math.max(-85, Math.min(85, view.lat + step[1])); draw(); e.preventDefault(); }
    else if (e.key === '+' || e.key === '=') zoom(1.25);
    else if (e.key === '-') zoom(0.8);
  });

  const resize = new ResizeObserver(draw);
  resize.observe(canvas);
  const onTheme = () => draw();
  document.addEventListener('themechange', onTheme);
  draw();
  return {
    draw,
    lookAt(lon, lat) { view.lon = lon; view.lat = lat; draw(); },
    setCoverage(value) { coverage = value; draw(); },
    destroy() { alive = false; cancelAnimationFrame(frame); resize.disconnect(); document.removeEventListener('themechange', onTheme); },
  };
}
