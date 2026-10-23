// Trips panel: every trip (out/trips.json from build_trips.py) grouped by year, with
// stats. Picking one flies the map to it, highlights its tracks and narrows the photos
// and filmstrip to its dates (app.js setTripRange); "All trips" undoes all three.
(function () {
  const CFG = window.MAP_CONFIG;
  const KM_MI = 0.621371, M_FT = 3.28084;
  let trips = [], selected = null, built = false;
  const FEATURED = 'featured';
  const open = new Set([FEATURED]);             // expanded years (and the folders); Featured starts open
  const AREAS = 'areas';

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const parts = (iso) => { const [y, m, d] = iso.split('-').map(Number); return { y, m, d }; };
  function dateRange(t) {
    const a = parts(t.start), b = parts(t.end);
    if (t.kind === 'local') return `${a.y}`;
    if (t.start === t.end) return `${MON[a.m - 1]} ${a.d}, ${a.y}`;
    if (a.y === b.y && a.m === b.m) return `${MON[a.m - 1]} ${a.d}–${b.d}, ${a.y}`;
    if (a.y === b.y) return `${MON[a.m - 1]} ${a.d} – ${MON[b.m - 1]} ${b.d}, ${a.y}`;
    return `${MON[a.m - 1]} ${a.d}, ${a.y} – ${MON[b.m - 1]} ${b.d}, ${b.y}`;
  }
  const mi = (km) => (km * KM_MI >= 10 ? Math.round(km * KM_MI) : (km * KM_MI).toFixed(1));
  const title = (t) => t.name || `Trip, ${dateRange(t)}`;
  const daysLabel = (t) => t.kind === 'local' ? `${t.days} days out` : t.days === 1 ? 'day trip' : `${t.days} days`;
  function statLine(t) {
    const bits = [daysLabel(t)];
    if (t.hiked_km >= 0.5) bits.push(`${mi(t.hiked_km)} mi on foot`);
    if (t.photos) bits.push(`${t.photos.toLocaleString()} photos`);
    return bits.join(' · ');
  }
  const thumb = (t) => {
    const id = t.hero || (t.photo_ids && t.photo_ids[0]);
    return id ? `<img loading="lazy" src="${CFG.photoUrl(id, 'thumb')}" alt="">` : '<div class="trip-noimg"></div>';
  };

  // ----- list ----------------------------------------------------------------
  function renderList() {
    const q = ($('trips-q').value || '').trim().toLowerCase();
    const shown = trips.filter((t) => !q || `${title(t)} ${t.summary || ''} ${t.start}`.toLowerCase().includes(q));
    const byYear = {};
    for (const t of shown) (byYear[t.start.slice(0, 4)] ||= []).push(t);
    const years = Object.keys(byYear).sort().reverse();
    // Styles that colour a trip's area (`tripArea`) get a folder of those trips on top.
    const S = window._am && window._am.style;
    const areas = S && S.tripArea ? shown.filter((t) => t.area) : [];
    if (areas.length) { byYear[AREAS] = areas; years.unshift(AREAS); }
    // Featured trips (build_trips.py `featured`: the owner's pick, in their order) on top of all
    const featured = shown.filter((t) => t.featured);
    if (featured.length) { byYear[FEATURED] = featured; years.unshift(FEATURED); }
    $('trips-count').textContent = `${shown.filter((t) => t.kind !== 'local').length} trips`;
    $('trips-list').innerHTML = years.map((y) => {
      const ts = byYear[y].sort(y === FEATURED ? (a, b) => a.featured - b.featured : (a, b) => (a.start < b.start ? 1 : -1));
      const away = ts.filter((t) => t.kind !== 'local');
      const days = away.reduce((n, t) => n + t.days, 0);
      const km = ts.reduce((n, t) => n + (t.hiked_km || 0), 0);
      const isOpen = (q && y !== AREAS) || open.has(y);
      const label = y === FEATURED ? '⭐ Featured trips' : y === AREAS ? '✨ Highlighted trips' : y;
      const sub = y === AREAS || y === FEATURED ? `${ts.length} trips` : `${away.length} trips · ${days} days · ${mi(km)} mi`;
      return `<div class="trip-year ${isOpen ? 'open' : ''}" data-year="${y}">
        <button class="trip-year-h" type="button"><span class="chev">${isOpen ? '▾' : '▸'}</span>
          <b>${label}</b><span>${sub}</span></button>
        <div class="trip-rows">${isOpen ? ts.map((t) => `
          <button class="trip-row" type="button" data-id="${esc(t.id)}">${thumb(t)}
            <span class="trip-txt"><span class="trip-name">${esc(title(t))}</span>
            <span class="trip-sub">${esc(t.kind === 'local' ? 'Local hikes' : dateRange(t))}</span>
            <span class="trip-sub">${esc(statLine(t))}</span></span></button>`).join('') : ''}</div></div>`;
    }).join('') || '<p class="trip-empty">No trips match.</p>';
  }

  // ----- detail --------------------------------------------------------------
  function renderDetail(t) {
    const hero = t.hero || (t.photo_ids && t.photo_ids[0]);
    const stat = (label, v) => (v ? `<div class="trip-stat"><span>${label}</span><b>${v}</b></div>` : '');
    // drives are listed (and drawn) only on multi-day trips
    const tracks = (t.tracks || []).filter((x) => x.type !== 'drive' || (t.has_drive && x.km > 1));
    $('trips-detail').innerHTML = `
      ${hero ? `<img class="trip-hero" src="${CFG.photoUrl(hero, 'web')}" alt="">` : ''}
      <div class="trip-d">
        <div class="trip-d-h"><button id="trips-back" type="button" aria-label="All trips">←</button>
          <b id="trip-title">${esc(title(t))}</b>
          <button id="trip-rename" type="button" title="Rename this trip" aria-label="Rename this trip">✏️</button></div>
        <div class="trip-sub">${esc(dateRange(t))}${t.place ? ' · ' + esc(t.place) : ''}</div>
        <div class="trip-stats">
          ${stat(t.kind === 'local' ? 'Days out' : 'Days', t.days)}
          ${stat('On foot', t.hiked_km >= 0.5 ? mi(t.hiked_km) + ' mi' : '')}
          ${stat('Driven', t.has_drive && t.drive_km >= 1 ? mi(t.drive_km) + ' mi' : '')}
          ${stat('High point', t.high_m ? Math.round(t.high_m * M_FT).toLocaleString() + ' ft' : '')}
          ${stat('Photos', t.photos ? t.photos.toLocaleString() : '')}
        </div>
        ${t.summary ? `<p class="trip-summary">${esc(t.summary)}</p>` : ''}
        ${tracks.length ? `<div class="trip-tracks-h">Tracks</div>${tracks.slice(0, 40).map((x, i) => `
          <button class="trip-track" type="button" data-i="${i}" ${x.bb ? '' : 'disabled'}>${x.type === 'drive' ? '🚙' : '🥾'} ${esc(x.name || (x.type === 'drive' ? 'Drive' : 'Hike'))} · ${mi(x.km)} mi</button>`).join('')}` : ''}
      </div>`;
    $('trips-back').onclick = clearSelection;
    $('trip-rename').onclick = () => startRename(t);
    $('trips-detail').querySelectorAll('.trip-track').forEach((b) => {
      b.onclick = () => focusTrack(tracks[+b.dataset.i], b);
    });
  }

  // ----- renaming --------------------------------------------------------------
  // Names you give a trip live in D1 (/api/trip-names) and win over the generated
  // ones in trips.json, which the nightly run rewrites. Local dev keeps them in this
  // browser instead. A rename is matched by trip id, or by start date if a rebuild
  // re-cut the trip and its id changed.
  const LOCAL_KEY = 'am-trip-names';
  let renames = {};
  async function loadRenames() {
    try {
      renames = CFG.isLocal ? JSON.parse(localStorage.getItem(LOCAL_KEY) || '{}')
        : !CFG.editing ? {}                             // read-only: the names in trips.json
        : ((await (await fetch('/api/trip-names', { cache: 'no-store' })).json()).names || {});
    } catch (e) { renames = {}; console.warn('trip names', e); }
    const byStart = {};
    for (const [id, r] of Object.entries(renames)) if (r.start && !trips.some((t) => t.id === id)) byStart[r.start] = r;
    for (const t of trips) {
      const r = renames[t.id] || (t.kind !== 'local' && byStart[t.start]);
      if (r) { t.autoName = t.name; t.name = r.name; }
    }
  }
  async function saveRename(t, name) {
    const reset = !name || name === t.autoName;
    if (CFG.isLocal) {
      if (reset) delete renames[t.id]; else renames[t.id] = { name, start: t.start, end: t.end };
      try { localStorage.setItem(LOCAL_KEY, JSON.stringify(renames)); } catch {}
      return true;
    }
    try {
      const r = await fetch('/api/trip-names', { method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify(reset ? { id: t.id, reset: true } : { id: t.id, name, start: t.start, end: t.end }) });
      return r.ok;
    } catch (e) { console.warn('rename', e); return false; }
  }
  // The title turns into a text box: Enter saves, Esc cancels, an empty box goes back
  // to the generated name.
  function startRename(t) {
    const b = $('trip-title');
    if (!b || b.tagName === 'INPUT') return;
    const input = document.createElement('input');
    input.id = 'trip-title'; input.className = 'trip-title-input'; input.maxLength = 120;
    input.value = title(t);
    input.placeholder = t.autoName || title(t);
    b.replaceWith(input);
    input.focus(); input.select();
    let done = false;
    const finish = async (save) => {
      if (done) return;
      done = true;
      const name = input.value.trim();
      if (save && name !== title(t)) {
        const before = t.name;
        if (!t.autoName) t.autoName = t.name;
        t.name = name || t.autoName;
        if (!(await saveRename(t, name))) { t.name = before; alert('Could not save the new name.'); }
      }
      if (selected === t) renderDetail(t);
      renderList();
    };
    input.onkeydown = (e) => { if (e.key === 'Enter') finish(true); else if (e.key === 'Escape') finish(false); };
    input.onblur = () => finish(true);
  }

  // A track in the trip's list: fly to it and mark it on top of the trip highlight.
  function focusTrack(x, btn) {
    if (!x || !x.bb) return;
    $('trips-detail').querySelectorAll('.trip-track.sel').forEach((b) => b.classList.remove('sel'));
    btn.classList.add('sel');
    focus(x);
    const [w, s, e, n] = x.bb;
    window._am.map.fitBounds([[w, s], [e, n]], { padding: openArea(), maxZoom: 14, duration: 1200 });
  }

  // ----- map -----------------------------------------------------------------
  const HL = [
    { id: 'trip-hl-activity', src: 'activities', layer: 'activities', key: 'activityId', pick: (t) => t.tracks.filter((x) => x.src === 'activity').map((x) => String(x.id)) },
    { id: 'trip-hl-hikes', src: 'hikes', layer: 'hikes_snapped', key: 'src_idx', pick: (t) => t.tracks.filter((x) => x.src === 'hike' && x.type !== 'drive').map((x) => x.id) },
  ];
  // Roads driven, multi-day trips only (a day hike's drive to the trailhead is noise):
  // the trip's own drive file (Google history matched to roads) plus any dated drive
  // lines from the hikes layer. Blue, under the hike highlight.
  const DRIVE = { color: '#2d6cdf', casing: '#ffffff' };
  // Track highlight layers sit BELOW every photo layer (the trip's photos lie along
  // its tracks, and a 7 px line on top buried them).
  function belowPhotos(map) {
    return ['trip-photos', 'photos'].find((id) => map.getLayer(id));
  }
  function ensureHL(map, h, filter) {
    if (map.getLayer(h.id)) return;
    const before = belowPhotos(map);
    const line = (id, paint) => map.addLayer({ id, type: 'line', source: h.src, 'source-layer': h.layer, filter,
      layout: { 'line-cap': 'round', 'line-join': 'round' }, paint }, before);
    line(h.id + '-case', { 'line-color': '#ffffff', 'line-width': 7, 'line-opacity': 0.9 });
    line(h.id, { 'line-color': '#e4572e', 'line-width': 3.5 });
    line(h.id + '-focus', { 'line-color': '#8e1b10', 'line-width': 6 });
  }

  async function showDrive(t) {
    const map = window._am.map;
    const empty = { type: 'FeatureCollection', features: [] };
    let data = empty;
    if (t && t.has_drive) {
      try { data = await (await fetch(CFG.tripDriveUrl(t.id))).json(); } catch (e) { console.warn('trip drive', e); }
    }
    if (selected !== t && t) return;                  // another trip was picked meanwhile
    // below the hike highlight (whose lowest layer is its casing), else below the photos
    const before = ['trip-hl-garmin-case', 'trip-hl-hikes-case'].find((id) => map.getLayer(id)) || belowPhotos(map);
    if (!map.getSource('trip-drive')) {
      map.addSource('trip-drive', { type: 'geojson', data });
      const line = (id, paint) => map.addLayer({ id, type: 'line', source: 'trip-drive',
        layout: { 'line-cap': 'round', 'line-join': 'round' }, paint }, before);
      line('trip-drive-case', { 'line-color': DRIVE.casing, 'line-width': 6, 'line-opacity': 0.85 });
      line('trip-drive', { 'line-color': DRIVE.color, 'line-width': 3 });
    } else {
      map.getSource('trip-drive').setData(data);
    }
    // legacy drive lines in the hikes tiles
    if (map.getSource('hikes')) {
      const ids = t && t.has_drive ? t.tracks.filter((x) => x.src === 'hike' && x.type === 'drive').map((x) => x.id) : [];
      const filter = ['in', ['get', 'src_idx'], ['literal', ids]];
      if (!map.getLayer('trip-hl-hikedrive')) {
        map.addLayer({ id: 'trip-hl-hikedrive', type: 'line', source: 'hikes', 'source-layer': 'hikes_snapped', filter,
          layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: { 'line-color': DRIVE.color, 'line-width': 3 } }, before);
      } else {
        map.setFilter('trip-hl-hikedrive', filter);
      }
    }
  }
  const noTrack = (h) => ['in', ['get', h.key], ['literal', []]];
  function highlight(t) {
    const map = window._am.map;
    for (const h of HL) {
      if (!map.getSource(h.src)) continue;
      const filter = ['in', ['get', h.key], ['literal', t ? h.pick(t) : []]];
      ensureHL(map, h, filter);
      map.setFilter(h.id, filter);
      map.setFilter(h.id + '-case', filter);
      map.setFilter(h.id + '-focus', noTrack(h));
    }
  }
  function focus(x) {
    const map = window._am.map;
    for (const h of HL) {
      if (!map.getLayer(h.id + '-focus')) continue;
      const mine = x && ((h.src === 'activities' && x.src === 'activity') || (h.src === 'hikes' && x.src === 'hike'));
      map.setFilter(h.id + '-focus', mine ? ['==', ['get', h.key], h.key === 'activityId' ? String(x.id) : x.id] : noTrack(h));
    }
  }

  // Styles that can colour a trip's area (`tripArea`) get the trip, or null to clear.
  function showArea(t) {
    const S = window._am.style;
    if (S && S.tripArea) S.tripArea(window._am.map, t && t.area ? t : null);
  }

  function select(id) {
    const t = trips.find((x) => x.id === id);
    if (!t) return;
    selected = t;
    $('trips').classList.add('detail');
    renderDetail(t);
    highlight(t);
    showDrive(t);
    window._am.setTripRange([t.start, t.end], CFG.tripPhotosUrl(t.id));
    // frame where the trip mostly happened (build_trips.py `focus`: not the drive or
    // flight there and back), or the coloured area when the style draws one
    let [w, s, e, n] = t.focus || t.bbox;
    const S = window._am.style;
    if (S && S.tripArea && t.area) {
      [w, s, e, n] = [180, 90, -180, -90];
      for (const r of t.area) for (const [x, y] of r) { w = Math.min(w, x); e = Math.max(e, x); s = Math.min(s, y); n = Math.max(n, y); }
    }
    window._am.map.fitBounds([[w, s], [e, n]], { padding: openArea(), maxZoom: 13, duration: 1600 });
    // after the flight has started, so the reveal waits for the camera to land
    showArea(t);
  }

  // fitBounds padding = the part of the map NOT covered by the trips panel, the layers
  // panel and the filmstrip, measured from the page (desktop: panel on the right;
  // phone: the panel shrinks to the top of the screen while a trip is open).
  function openArea() {
    const W = window.innerWidth, H = window.innerHeight, gap = 24;
    const rect = (id) => { const el = $(id); const r = el && el.getBoundingClientRect(); return r && r.width ? r : null; };
    const pad = { top: gap, bottom: gap, left: gap, right: gap };
    const film = rect('filmstrip');
    if (film) pad.bottom = Math.max(pad.bottom, H - film.top + gap);
    const tp = rect('trips');
    if (tp && tp.width < W * 0.6) pad.right = Math.max(pad.right, W - tp.left + gap);   // side panel
    else if (tp) pad.top = Math.max(pad.top, tp.bottom + gap);                          // top sheet (phone)
    const lp = rect('panel-body');
    if (lp && lp.width < W * 0.6 && lp.bottom > pad.top) pad.left = Math.max(pad.left, lp.right + gap);
    // never squeeze the map below a usable window
    const minW = 160, minH = 140;
    if (W - pad.left - pad.right < minW) { pad.left = gap; pad.right = Math.min(pad.right, W - minW - gap); }
    if (H - pad.top - pad.bottom < minH) { pad.top = Math.min(pad.top, H - pad.bottom - minH); }
    return pad;
  }

  function clearSelection() {
    selected = null;
    $('trips').classList.remove('detail');
    highlight(null);
    showDrive(null);
    showArea(null);
    window._am.setTripRange(null, null);
  }

  // ----- wiring --------------------------------------------------------------
  async function load() {
    try {
      const d = await (await fetch(CFG.tripsUrl)).json();
      trips = d.trips || d;
    } catch (e) { trips = []; console.warn('trips', e); }
    await loadRenames();
    const latest = trips.reduce((y, t) => (t.start.slice(0, 4) > y ? t.start.slice(0, 4) : y), '');
    if (latest) open.add(latest);
  }

  function build() {
    built = true;
    $('trips-q').addEventListener('input', renderList);
    $('trips-close').onclick = () => toggle(false);
    $('trips-list').addEventListener('click', (e) => {
      const yh = e.target.closest('.trip-year-h');
      if (yh) { const y = yh.parentElement.dataset.year; open.has(y) ? open.delete(y) : open.add(y); renderList(); return; }
      const row = e.target.closest('.trip-row');
      if (row) select(row.dataset.id);
    });
  }

  async function toggle(show) {
    const el = $('trips');
    const on = show ?? !el.classList.contains('on');
    el.classList.toggle('on', on);
    $('trips-btn').classList.toggle('active', on);
    if (on) {
      if (!built) { build(); await load(); }
      renderList();
    } else if (selected) {
      clearSelection();
    }
  }

  window.addEventListener('DOMContentLoaded', () => {
    const btn = $('trips-btn');
    if (btn) btn.onclick = () => toggle();
  });
})();
