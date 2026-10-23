/* Adventure Map — bottom "filmstrip" photo timeline + geotagging.
 *
 * A collapsible strip at the bottom of the screen shows photos in time order:
 *   - geo-referenced photos currently visible on the map (queryRenderedFeatures),
 *   - PLUS un-referenced photos from the same time range, badged "needs location".
 * Hover a located photo -> a leader line is drawn from its thumbnail to its spot
 * on the map. All geotagging lives here: select un-referenced photos and drag them
 * onto the map (or "Place on map" -> tap), or "Skip". Saves to /api/geotags (D1).
 */
(() => {
  let map, API = '/api/geotags';
  const $ = (id) => document.getElementById(id);
  const photoThumb = (id) => window.MAP_CONFIG.photoUrl(id, 'thumb');

  let untagged = [];          // [{id, ts, is_360, suggestion?}] un-referenced, not yet placed/skipped, sorted by ts
  let suggestions = {};       // id -> {lon,lat,delta_min,source,method} -- nearest-GPS-time matches
  let tagged = {};            // id -> {status}
  const sel = new Set();      // selected un-referenced ids
  let placeMode = null;       // {ids} awaiting a map tap
  let hover = null;           // {el, coord} currently-hovered located photo
  let lastSelIdx = -1;        // anchor index for shift-click range select
  let focusedId = null;       // photo to keep centered in the strip after the next refresh
  let curItems = [];          // currently rendered records
  let programmaticMove = false; // true while a filmstrip-click map move is in flight
                                // (its moveend must NOT rebuild/reshuffle the strip)

  // keep a photo in view across re-renders / thumbnail resizes (horizontal)
  function centerVisibleId() {
    const track = $('film-track'); if (!track) return null;
    const cx = track.scrollLeft + track.clientWidth / 2;
    let best = null, bd = Infinity;
    track.querySelectorAll('.film-item').forEach((el) => {
      const d = Math.abs((el.offsetLeft + el.offsetWidth / 2) - cx);
      if (d < bd) { bd = d; best = el.dataset.id; }
    });
    return best;
  }
  function scrollToId(id, center) {
    const track = $('film-track');
    const el = track && track.querySelector(`.film-item[data-id="${id}"]`);
    if (!el) return;
    if (center) track.scrollLeft = Math.max(0, el.offsetLeft + el.offsetWidth / 2 - track.clientWidth / 2);
    else el.scrollIntoView({ inline: 'start', block: 'nearest' });
  }
  const newFeats = [];        // session "just placed" markers
  const isAi = (s) => !!(s && s.source === 'claude');   // geotag_ai.py suggestion, not a GPS match

  window.initFilmstrip = async (m) => {
    map = m;
    ensureMarkerLayer();
    wireControls();
    initSize();
    wireMapGeotag();
    await loadUntagged();
    const deb = debounce(() => {
      // a map move from clicking a filmstrip photo must not reshuffle the strip
      // (you'd lose the photo you just clicked); only USER pans/zooms rebuild it.
      if (programmaticMove) { programmaticMove = false; return; }
      refresh(true);
    }, 220);
    map.on('moveend', deb);
    map.on('move', updateHoverLine);
    window.filmstripRefresh = refresh;     // app.js calls this after hiding a photo
    refresh();
    // The refresh above usually runs before the photo tiles have rendered, so it
    // found "0 on map" and the strip stayed that way until you moved the map. Once
    // the map first settles with tiles drawn, rebuild so on-screen photos show.
    // (triggerRepaint guarantees a fresh 'idle' even if the map already went idle
    // while the catalog was downloading.)
    map.once('idle', () => refresh(true));
    map.triggerRepaint();
  };

  // ----- data --------------------------------------------------------------
  // Untagged catalog as [{id, ts, is_360}]. Prefers the slim rows file
  // ({fields, rows}); falls back to the full GeoJSON if it's missing.
  async function fetchUntaggedRecords() {
    const cfg = window.MAP_CONFIG;
    try {
      const r = await fetch(cfg.untaggedUrl);
      if (r.ok) {
        const d = await r.json();
        if (Array.isArray(d.rows)) return d.rows.map(([id, ts, s]) => ({ id, ts: ts || '', is_360: !!s }));
      }
    } catch {}
    const fc = await (await fetch(cfg.untaggedFullUrl)).json();
    return (fc.features || []).map((f) => ({
      id: f.properties.id, ts: f.properties.timestamp || '', is_360: !!f.properties.is_360 }));
  }

  async function loadUntagged() {
    const json = (url) => fetch(url).then((r) => r.json());
    try {
      // fetched in parallel — these used to wait on each other in sequence
      const [records, tg, sg, ai] = await Promise.all([
        fetchUntaggedRecords(),
        json(API).catch(() => ({})),
        // Offline-computed nearest-GPS-time matches (suggest_geotags.py). The file
        // may not exist yet -- that's fine, suggestions are an optional enhancement.
        json(window.MAP_CONFIG.geotagSuggestionsUrl).catch(() => ({})),
        // Places recognised in the picture itself (geotag_ai.py, source "claude"):
        // used only where there's no GPS match, and never bulk-accepted.
        json(window.MAP_CONFIG.geotagAiUrl).catch(() => ({})),
      ]);
      tagged = tg || {};
      suggestions = sg || {};
      const aiSugg = ai || {};
      untagged = records
        .filter((u) => !tagged[u.id])
        .map((u) => ({ ...u, suggestion: suggestions[u.id] || aiSugg[u.id] || null }))
        .sort((a, b) => (a.ts < b.ts ? -1 : a.ts > b.ts ? 1 : 0));
    } catch { untagged = []; }
  }

  function refresh(trackMap) {
    // Scroll target after the rebuild. On a user pan/zoom (trackMap) the strip is
    // rebuilt from what's now in view, so it starts at the far LEFT — the strip
    // then reads as "the photos on screen, from the beginning". Otherwise (hide a
    // photo, resize, tag) keep the focused/centered photo where it was, so those
    // in-place edits don't jump your position.
    const preserved = trackMap ? null : (focusedId || centerVisibleId());
    focusedId = null;
    // located photos currently rendered on the map
    let feats = [];
    // the tiled dots, plus an open trip's own photo layer (trips.js / app.js)
    const photoLayers = ['photos', 'trip-photos'].filter((id) => map.getLayer(id));
    if (photoLayers.length) {
      try { feats = map.queryRenderedFeatures({ layers: photoLayers }); } catch {}
    }
    const isHidden = (window._am && window._am.isPhotoHidden) || (() => false);
    const seen = new Set(), located = [];
    for (const f of feats) {
      const p = f.properties; if (!p || seen.has(p.id) || isHidden(p.id)) continue; seen.add(p.id);
      const c = f.geometry && f.geometry.coordinates;
      located.push({ id: p.id, ts: p.timestamp || '', referenced: true, coord: c,
        is_360: p.is_360 === true || p.is_360 === 'true', filename: p.filename, pano_type: p.pano_type, props: p });
    }
    located.sort((a, b) => (a.ts < b.ts ? -1 : 1));
    const cappedLoc = located.slice(0, 500);

    // Un-referenced photos worth showing = ones taken AROUND THE SAME TIME as
    // something on screen (same trip), not merely inside the overall span. The old
    // [min,max] test degenerated to "the whole archive" whenever the view mixed
    // eras; the 300-cap then took the 300 EARLIEST, which clumped into one dense
    // block and pushed the later on-map photos off to the far right of the strip.
    const locTimes = cappedLoc.map((r) => Date.parse(r.ts))
      .filter((t) => !isNaN(t)).sort((a, b) => a - b);
    let unref;
    if (locTimes.length) {
      unref = untagged.filter((u) => {
        const t = Date.parse(u.ts);
        return !isNaN(t) && nearestDiffMs(locTimes, t) <= NEAR_MS;
      });
    } else {
      unref = untagged;   // no located photos in view -> show un-referenced so you can still tag
    }
    // If still over the cap, thin EVENLY across time rather than taking the
    // earliest N — slicing would clump them again and rebuild the same wall.
    if (unref.length > UNREF_CAP) {
      const step = unref.length / UNREF_CAP, thinned = [];
      for (let i = 0; i < UNREF_CAP; i++) thinned.push(unref[Math.floor(i * step)]);
      unref = thinned;
    }
    unref = unref.map((u) => ({ ...u, referenced: false }));

    // Photos on the map always come FIRST (in time order), then the ones to place
    // (also in time order). Interleaving them by time put "to place" photos ahead
    // of what you're actually looking at whenever they happened to be older.
    // (both lists are already time-sorted: cappedLoc above, unref via `untagged`)
    curItems = [...cappedLoc, ...unref];
    renderTrack(located.length, unref.length, located.length > cappedLoc.length);
    if (preserved) scrollToId(preserved, true);   // restore position (non-map refresh)
    else $('film-track').scrollLeft = 0;          // map moved -> show the strip from the start
  }

  // How close (in time) an un-referenced photo must be to something on the map to
  // count as "same trip" context worth offering to place. Widen if you want more.
  const NEAR_MS = 24 * 60 * 60 * 1000;   // 1 day
  const UNREF_CAP = 300;                 // max un-referenced photos shown at once

  // Smallest |t - x| over a sorted numeric array (binary search).
  function nearestDiffMs(sorted, t) {
    let lo = 0, hi = sorted.length;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (sorted[mid] < t) lo = mid + 1; else hi = mid; }
    let best = Infinity;
    if (lo < sorted.length) best = Math.min(best, Math.abs(sorted[lo] - t));
    if (lo > 0) best = Math.min(best, Math.abs(sorted[lo - 1] - t));
    return best;
  }

  // ----- render ------------------------------------------------------------
  function renderTrack(locCount, unrefCount, truncated) {
    const track = $('film-track');
    track.innerHTML = '';
    if (!curItems.length) {
      track.innerHTML = '<div class="film-empty">No photos here yet — pan/zoom the map, or scroll to a time with photos.</div>';
    }
    lastSelIdx = -1;                       // reset the shift-select anchor on re-render
    const frag = document.createDocumentFragment();
    let lastDay = '';
    curItems.forEach((r, idx) => {
      // divider where the "to place" group starts (after the on-map photos)
      if (!r.referenced && idx > 0 && curItems[idx - 1].referenced) {
        const sep = document.createElement('div');
        sep.className = 'film-sep';
        sep.textContent = 'To place';
        frag.appendChild(sep);
        lastDay = '';                        // restart date labels for this group
      }
      const day = (r.ts || '').slice(0, 10);
      if (day && day !== lastDay) { frag.appendChild(dayLabel(r.ts)); lastDay = day; }
      frag.appendChild(itemEl(r, idx));
    });
    track.appendChild(frag);
    $('film-info').textContent =
      `${locCount.toLocaleString()} on map${truncated ? '+ (zoom in)' : ''} · ${unrefCount.toLocaleString()} to place`;
    updateToolbar();
  }

  function dayLabel(ts) {
    const d = document.createElement('div');
    d.className = 'film-day';
    d.textContent = fmtDay(ts);
    return d;
  }

  function itemEl(r, idx) {
    const el = document.createElement('div');
    el.className = 'film-item' + (r.referenced ? ' ref' : '') + (sel.has(r.id) ? ' sel' : '');
    el.dataset.id = r.id;
    el.dataset.idx = idx;
    // GPS-match suggestion (suggest_geotags.py): a green badge with the time gap,
    // plus a one-tap accept button so placing a confidently-matched photo doesn't
    // need the drag/tap-to-place flow at all.
    const ai = isAi(r.suggestion);
    const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
    const badge = r.referenced ? '' : (!r.suggestion ? '<span class="film-badge">place</span>'
      : ai ? `<span class="film-badge ai" title="Recognised in the photo: ${esc(r.suggestion.place)} (${esc(r.suggestion.confidence)} confidence)">📍 ${esc(r.suggestion.place.split(',')[0])}</span>`
        : `<span class="film-badge gps" title="Nearest GPS match: ${r.suggestion.source} (±${r.suggestion.delta_min} min)">GPS ±${r.suggestion.delta_min}min</span>`);
    const acceptBtn = (!r.referenced && r.suggestion)
      ? `<button class="film-accept-one" title="${ai ? 'Accept suggested place' : 'Accept GPS match'}">✓</button>` : '';
    el.innerHTML =
      `<div class="film-thumb-wrap">${badge}${acceptBtn}` +
      `<img class="film-thumb" loading="lazy" src="${photoThumb(r.id)}" alt="" onerror="this.style.opacity=.2"></div>` +
      `<div class="film-date">${fmtTime(r.ts)}</div>`;

    if (r.referenced) {
      // hover -> leader line to its map spot; click -> open
      el.addEventListener('mouseenter', () => { if (r.coord) { hover = { el, coord: r.coord }; updateHoverLine(); } });
      el.addEventListener('mouseleave', () => { hover = null; clearLine(); });
      el.addEventListener('click', () => {
        if (!r.coord) return;
        programmaticMove = true;                           // don't reshuffle the strip on this move
        scrollToId(r.id, true);                            // center the clicked photo in the strip
        map.easeTo({ center: r.coord, duration: 600 });    // center the map on this photo
        if (window._am && window._am.openPhoto) window._am.openPhoto(r.props, { lng: r.coord[0], lat: r.coord[1] });
      });
    } else {
      // un-referenced -> selectable (shift-click = range) + draggable for geotagging
      el.draggable = true;
      el.addEventListener('click', (e) => onUnrefClick(r, idx, e, el));
      el.addEventListener('dragstart', (e) => {
        const ids = sel.has(r.id) ? [...sel] : [r.id];
        e.dataTransfer.setData('text/plain', JSON.stringify(ids));
        e.dataTransfer.effectAllowed = 'copy';
      });
      if (r.suggestion) {
        el.querySelector('.film-accept-one').addEventListener('click', (e) => {
          e.stopPropagation();   // don't also trigger the select-toggle click
          acceptSuggestion(r);
        });
      }
    }
    return el;
  }

  // click an un-referenced tile: toggle, or shift-click to select the whole range
  function onUnrefClick(r, idx, e, el) {
    if (e.shiftKey && lastSelIdx >= 0) {
      const a = Math.min(lastSelIdx, idx), b = Math.max(lastSelIdx, idx);
      for (let i = a; i <= b; i++) {
        const it = curItems[i];
        if (it && !it.referenced) {
          sel.add(it.id);
          const node = $('film-track').querySelector(`.film-item[data-idx="${i}"]`);
          if (node) node.classList.add('sel');
        }
      }
    } else {
      sel.has(r.id) ? sel.delete(r.id) : sel.add(r.id);
      el.classList.toggle('sel');
      lastSelIdx = idx;
      // if this photo has a suggested GPS match, fly to it + ring the spot so you
      // can verify where it would land before hitting the ✓ accept. Flag the move
      // as programmatic so its moveend doesn't rebuild the strip out from under you.
      if (r.suggestion) {
        programmaticMove = true;
        const coord = [r.suggestion.lon, r.suggestion.lat];
        map.easeTo({ center: coord, zoom: Math.max(map.getZoom(), 13), duration: 600 });
        showPreview(coord);
      }
    }
    updateToolbar();
  }

  // ----- leader line (hovered photo -> map) --------------------------------
  function updateHoverLine() {
    if (!hover) return;
    const svg = $('film-overlay');
    const r = hover.el.getBoundingClientRect();
    const p = map.project(hover.coord);
    const fx = r.left + r.width / 2, fy = r.top;
    svg.innerHTML =
      `<line x1="${fx}" y1="${fy}" x2="${p.x}" y2="${p.y}" stroke="#e91e63" stroke-width="2"/>` +
      `<circle cx="${p.x}" cy="${p.y}" r="6" fill="#e91e63" stroke="#fff" stroke-width="2"/>`;
    svg.style.display = 'block';
  }
  function clearLine() { const s = $('film-overlay'); s.innerHTML = ''; s.style.display = 'none'; }

  // ----- geotagging controls ----------------------------------------------
  // thumbnail size stepper (persisted); --film-h keeps the floating buttons clear
  let thumbSize = 96;
  function initSize() {
    thumbSize = parseInt(localStorage.getItem('filmThumb') || '96', 10) || 96;
    applySize();
    $('film-smaller').onclick = () => { thumbSize -= 16; applySize(); };
    $('film-bigger').onclick = () => { thumbSize += 16; applySize(); };
  }
  function applySize() {
    const anchor = centerVisibleId();      // keep the centered photo centered after resizing
    thumbSize = Math.max(64, Math.min(176, thumbSize));
    document.documentElement.style.setProperty('--film-thumb', thumbSize + 'px');
    setFilmHeight();
    localStorage.setItem('filmThumb', thumbSize);
    if (anchor) scrollToId(anchor, true);
  }
  function setFilmHeight() {
    const collapsed = $('filmstrip').classList.contains('collapsed');
    document.documentElement.style.setProperty('--film-h', (collapsed ? 34 : thumbSize + 92) + 'px');
  }

  function wireControls() {
    $('film-toggle').onclick = () => {
      const f = $('filmstrip'); f.classList.toggle('collapsed');
      $('film-toggle').textContent = f.classList.contains('collapsed') ? '▴' : '▾';
      setFilmHeight();
    };
    $('film-place').onclick = startPlaceMode;
    $('film-skip').onclick = () => commitSkip([...sel]);
    $('film-clear').onclick = clearSel;
    $('film-accept-matches').onclick = acceptAllSuggested;
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') { if (placeMode) { placeMode = null; $('film-banner').style.display = 'none'; } } });
  }
  function wireMapGeotag() {
    const mapDiv = $('map');
    mapDiv.addEventListener('dragover', (e) => e.preventDefault());
    mapDiv.addEventListener('drop', (e) => {
      e.preventDefault();
      let ids = []; try { ids = JSON.parse(e.dataTransfer.getData('text/plain')); } catch {}
      if (!ids.length) return;
      const rect = mapDiv.getBoundingClientRect();
      commitPlace(ids, map.unproject([e.clientX - rect.left, e.clientY - rect.top]));
    });
    map.on('click', (e) => {
      if (!placeMode) return;
      const ids = placeMode.ids; placeMode = null; $('film-banner').style.display = 'none';
      commitPlace(ids, e.lngLat);
    });
  }
  function updateToolbar() {
    const n = sel.size;
    $('film-sel').textContent = n ? `${n} selected` : '';
    $('film-place').disabled = $('film-skip').disabled = n === 0;
    $('film-clear').style.display = n ? '' : 'none';
    const matched = curItems.filter((r) => !r.referenced && r.suggestion && !isAi(r.suggestion)).length;
    const am = $('film-accept-matches');
    am.style.display = matched ? '' : 'none';
    am.textContent = `Accept ${matched} GPS match${matched === 1 ? '' : 'es'}`;
  }
  function clearSel() {
    sel.clear();
    document.querySelectorAll('.film-item.sel').forEach((t) => t.classList.remove('sel'));
    clearPreview();
    updateToolbar();
  }
  function startPlaceMode() {
    if (!sel.size) return;
    placeMode = { ids: [...sel] };
    const b = $('film-banner');
    b.textContent = `Tap the map to place ${placeMode.ids.length} photo(s)  ·  Esc to cancel`;
    b.style.display = 'block';
  }

  async function commitPlace(ids, lngLat) {
    const lon = +lngLat.lng.toFixed(6), lat = +lngLat.lat.toFixed(6);
    try {
      await fetch(API, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ ids, lon, lat }) });
    } catch { toast('Save failed — try again'); return; }
    afterTag(ids);
    addNewMarkers(ids, [lon, lat]);
    reverseGeocode(lon, lat, ids.length);
  }
  // accept one photo's suggested GPS match (the per-thumbnail ✓ button)
  async function acceptSuggestion(r) {
    const { lon, lat } = r.suggestion;
    try {
      await fetch(API, { method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ id: r.id, lon, lat }) });
    } catch { toast('Save failed — try again'); return; }
    afterTag([r.id]);
    addNewMarkers([r.id], [lon, lat]);
    toast(isAi(r.suggestion) ? `Placed at ${r.suggestion.place}` : `Placed via GPS match (±${r.suggestion.delta_min} min)`);
  }

  // accept every GPS-matched suggestion currently shown in the strip (each photo
  // keeps its own matched coordinate, unlike commitPlace's "drop a group at one
  // spot" -- so this fires one POST per photo, sequentially to be gentle on D1).
  async function acceptAllSuggested() {
    // GPS matches only: a place recognised in a picture is a guess to confirm one at a time
    const matched = curItems.filter((r) => !r.referenced && r.suggestion && !isAi(r.suggestion));
    if (!matched.length) return;
    const ok = [];
    for (const r of matched) {
      try {
        await fetch(API, { method: 'POST', headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ id: r.id, lon: r.suggestion.lon, lat: r.suggestion.lat }) });
        ok.push(r);
      } catch { /* leave it for the next pass / manual placement */ }
    }
    if (!ok.length) { toast('Save failed — try again'); return; }
    afterTag(ok.map((r) => r.id));
    ok.forEach((r) => addNewMarkers([r.id], [r.suggestion.lon, r.suggestion.lat]));
    toast(`Placed ${ok.length} photo${ok.length > 1 ? 's' : ''} via GPS match`);
  }

  async function commitSkip(ids) {
    if (!ids.length) return;
    try {
      await fetch(API, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ ids, status: 'skipped' }) });
    } catch { toast('Save failed — try again'); return; }
    afterTag(ids);
    toast(`Skipped ${ids.length}`);
  }
  function afterTag(ids) {
    const set = new Set(ids);
    untagged = untagged.filter((u) => !set.has(u.id));
    ids.forEach((id) => sel.delete(id));
    clearPreview();
    refresh();
  }

  // ----- map feedback ------------------------------------------------------
  const emptyFC = () => ({ type: 'FeatureCollection', features: [] });
  function ensureMarkerLayer() {
    if (map.getSource('geotagged-new')) return;
    map.addSource('geotagged-new', { type: 'geojson', data: emptyFC() });
    map.addLayer({ id: 'geotagged-new', type: 'circle', source: 'geotagged-new',
      paint: { 'circle-radius': 6, 'circle-color': '#2ecc71', 'circle-stroke-color': '#fff', 'circle-stroke-width': 2 } });
    // "candidate" ring: where a clicked GPS-match suggestion WOULD land, shown so
    // you can verify the spot before accepting it. Hollow, to read as not-yet-placed.
    map.addSource('geotag-preview', { type: 'geojson', data: emptyFC() });
    map.addLayer({ id: 'geotag-preview', type: 'circle', source: 'geotag-preview',
      paint: { 'circle-radius': 11, 'circle-color': '#2ecc71', 'circle-opacity': 0.22,
        'circle-stroke-color': '#2ecc71', 'circle-stroke-width': 2.5 } });
  }
  function showPreview(coord) {
    const s = map.getSource('geotag-preview');
    if (s) s.setData({ type: 'FeatureCollection',
      features: [{ type: 'Feature', geometry: { type: 'Point', coordinates: coord }, properties: {} }] });
  }
  function clearPreview() {
    const s = map.getSource('geotag-preview'); if (s) s.setData(emptyFC());
  }
  function addNewMarkers(ids, coord) {
    newFeats.push({ type: 'Feature', geometry: { type: 'Point', coordinates: coord }, properties: { n: ids.length } });
    const s = map.getSource('geotagged-new'); if (s) s.setData({ type: 'FeatureCollection', features: newFeats });
  }
  async function reverseGeocode(lon, lat, n) {
    let where = '';
    try {
      const r = await (await fetch(`https://nominatim.openstreetmap.org/reverse?format=jsonv2&lat=${lat}&lon=${lon}&zoom=12`)).json();
      where = r.name || (r.display_name || '').split(',').slice(0, 2).join(',');
    } catch {}
    toast(`Placed ${n} photo${n > 1 ? 's' : ''}${where ? ' · ' + where : ''}`);
  }

  // ----- helpers -----------------------------------------------------------
  let toastT;
  function toast(msg) {
    const t = $('app-toast'); if (!t) return;
    t.textContent = msg; t.style.display = 'block';
    clearTimeout(toastT); toastT = setTimeout(() => { t.style.display = 'none'; }, 3000);
  }
  function debounce(fn, ms) { let h; return (...a) => { clearTimeout(h); h = setTimeout(() => fn(...a), ms); }; }
  function fmtDay(ts) {
    const d = new Date(ts); if (isNaN(d)) return (ts || '').slice(0, 10);
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
  }
  function fmtTime(ts) {
    if (!ts) return '';
    const d = new Date(ts); if (isNaN(d)) return ts.slice(0, 10);
    return (d.getHours() || d.getMinutes())
      ? d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })
      : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
  }
})();
