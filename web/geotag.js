/* Adventure Map — geotag old (un-geotagged) photos.
 * A slide-out gallery of photos with no GPS, grouped by date. Select one, a
 * group, or many; then DRAG them onto the map or tap "Place on map" and tap a
 * spot. Saves {id,lon,lat} to /api/geotags (D1). A desktop script later writes
 * the EXIF back into the originals.
 */
(() => {
  let map, API = '/api/geotags';
  let groups = [];            // [{key,label,ids:[],items:[{id,filename}]}]
  const sel = new Set();      // selected photo ids
  let placeMode = null;       // {ids} when waiting for a map tap
  const els = {};

  const $ = (id) => document.getElementById(id);
  const photoThumb = (pid) => window.MAP_CONFIG.photoUrl(pid, 'thumb');

  window.initGeotag = (m) => {
    map = m;
    cacheEls();
    if (!els.btn) return;
    els.btn.onclick = openPanel;
    els.close.onclick = closePanel;
    initSize();
    els.place.onclick = startPlaceMode;
    els.skip.onclick = () => commitSkip([...sel]);
    els.clear.onclick = clearSel;
    if (els.jump) els.jump.onchange = () => jumpToGroup(els.jump.value);

    // drop target = the map
    const mapDiv = $('map');
    mapDiv.addEventListener('dragover', (e) => { e.preventDefault(); });
    mapDiv.addEventListener('drop', (e) => {
      e.preventDefault();
      let ids = [];
      try { ids = JSON.parse(e.dataTransfer.getData('text/plain')); } catch {}
      if (!ids.length) return;
      const rect = mapDiv.getBoundingClientRect();
      const ll = map.unproject([e.clientX - rect.left, e.clientY - rect.top]);
      commitPlace(ids, ll);
    });

    // tap-to-place: next map click drops the pending selection
    map.on('click', (e) => {
      if (!placeMode) return;
      const ids = placeMode.ids; placeMode = null;
      els.banner.style.display = 'none';
      commitPlace(ids, e.lngLat);
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && placeMode) { placeMode = null; els.banner.style.display = 'none'; }
    });

    ensureMarkerLayer();
  };

  function cacheEls() {
    for (const k of ['btn', 'panel', 'close', 'list', 'count', 'selcount', 'place', 'skip', 'clear', 'banner', 'toast', 'smaller', 'bigger', 'jump']) {
      els[k] = $('geotag-' + k);
    }
  }

  // thumbnail size stepper (persisted)
  function initSize() {
    let size = parseInt(localStorage.getItem('geotagSize') || '96', 10) || 96;
    const apply = () => {
      size = Math.max(56, Math.min(184, size));
      els.list.style.setProperty('--gt-tile', size + 'px');
      localStorage.setItem('geotagSize', size);
    };
    apply();
    if (els.smaller) els.smaller.onclick = () => { size -= 16; apply(); };
    if (els.bigger) els.bigger.onclick = () => { size += 16; apply(); };
  }

  // ----- load + group ------------------------------------------------------
  let loaded = false;
  async function openPanel() {
    els.panel.classList.add('open');
    if (!loaded) { loaded = true; await load(); }
  }
  function closePanel() { els.panel.classList.remove('open'); }

  async function load() {
    els.list.innerHTML = '<div class="geotag-empty">Loading…</div>';
    let feats = [], tagged = {};
    try {
      const fc = await (await fetch(window.MAP_CONFIG.untaggedUrl)).json();
      feats = fc.features || [];
    } catch (e) { els.list.innerHTML = '<div class="geotag-empty">No untagged catalog yet. Run <code>photos_ingest.py untagged</code>.</div>'; return; }
    try { tagged = await (await fetch(API)).json(); } catch {}

    // candidates = untagged photos not already placed/skipped/applied
    const byKey = new Map();
    for (const f of feats) {
      const p = f.properties; if (tagged[p.id]) continue;
      const ts = p.timestamp || '';
      const key = ts ? ts.slice(0, 7) : 'undated';        // YYYY-MM
      if (!byKey.has(key)) byKey.set(key, []);
      byKey.get(key).push({ id: p.id, filename: p.filename || '' });
    }
    groups = [...byKey.entries()]
      .sort((a, b) => {                                   // newest first, 'undated' last
        if (a[0] === 'undated') return 1;
        if (b[0] === 'undated') return -1;
        return a[0] < b[0] ? 1 : -1;
      })
      .map(([key, items]) => ({
        key, items, ids: items.map((x) => x.id),
        label: key === 'undated' ? 'Undated' : prettyMonth(key),
      }));
    sel.clear();
    render();
    updateToolbar();
  }

  function prettyMonth(k) {
    const [y, m] = k.split('-');
    return new Date(y, m - 1, 1).toLocaleString(undefined, { month: 'long', year: 'numeric' });
  }

  // ----- chunked rendering (handles thousands) -----------------------------
  let flat = [], cursor = 0, io = null, sentinel = null;
  function rebuildFlat() {
    flat = [];
    for (const g of groups) { flat.push({ t: 'h', g }); for (const it of g.items) flat.push({ t: 'i', it }); }
  }
  function updateCount() {
    const total = groups.reduce((n, g) => n + g.items.length, 0);
    els.count.textContent = total ? `(${total.toLocaleString()} left)` : '(all done 🎉)';
  }
  function render() {
    els.list.innerHTML = '';
    updateCount();
    populateJump();
    rebuildFlat();
    cursor = 0;
    sentinel = document.createElement('div'); sentinel.id = 'geotag-sentinel';
    els.list.appendChild(sentinel);
    if (io) io.disconnect();
    io = new IntersectionObserver((es) => { if (es[0].isIntersecting) chunk(); }, { root: els.list });
    io.observe(sentinel);
    chunk();
  }
  function chunk() {
    const end = Math.min(cursor + 120, flat.length);
    const frag = document.createDocumentFragment();
    for (; cursor < end; cursor++) {
      const e = flat[cursor];
      frag.appendChild(e.t === 'h' ? headerEl(e.g) : itemEl(e.it));
    }
    els.list.insertBefore(frag, sentinel);
    if (cursor >= flat.length && io) io.disconnect();
  }

  // jump to a date group: render forward until its header exists, then scroll to it
  function jumpToGroup(key) {
    if (!key) return;
    let hdr = els.list.querySelector(`.geotag-group[data-gkey="${key}"]`), guard = 0;
    while (!hdr && cursor < flat.length && guard++ < 3000) { chunk(); hdr = els.list.querySelector(`.geotag-group[data-gkey="${key}"]`); }
    if (hdr) hdr.scrollIntoView({ block: 'start' });
  }
  function populateJump() {
    const jp = els.jump; if (!jp) return;
    jp.innerHTML = '<option value="">Jump to date…</option>' +
      groups.map((g) => `<option value="${g.key}">${g.label} (${g.items.length})</option>`).join('');
    jp.value = '';
  }

  function headerEl(g) {
    const h = document.createElement('div');
    h.className = 'geotag-group';
    h.dataset.gkey = g.key;
    h.innerHTML = `<span>${g.label} · ${g.items.length}</span><button class="geotag-selall">select all</button>`;
    h.querySelector('button').onclick = () => {
      const allSel = g.ids.every((id) => sel.has(id));
      g.ids.forEach((id) => allSel ? sel.delete(id) : sel.add(id));
      // reflect on any rendered tiles
      g.ids.forEach((id) => { const t = els.list.querySelector(`[data-id="${id}"]`); if (t) t.classList.toggle('sel', sel.has(id)); });
      updateToolbar();
    };
    return h;
  }
  function itemEl(it) {
    const d = document.createElement('div');
    d.className = 'geotag-tile' + (sel.has(it.id) ? ' sel' : '');
    d.dataset.id = it.id;
    d.draggable = true;
    d.innerHTML = `<img loading="lazy" src="${photoThumb(it.id)}" alt="" onerror="this.style.opacity=.2">`;
    d.onclick = () => { sel.has(it.id) ? sel.delete(it.id) : sel.add(it.id); d.classList.toggle('sel'); updateToolbar(); };
    d.ondragstart = (e) => {
      const ids = sel.has(it.id) ? [...sel] : [it.id];
      e.dataTransfer.setData('text/plain', JSON.stringify(ids));
      e.dataTransfer.effectAllowed = 'copy';
    };
    return d;
  }

  // ----- selection toolbar -------------------------------------------------
  function updateToolbar() {
    const n = sel.size;
    els.selcount.textContent = `${n} selected`;
    els.place.disabled = els.skip.disabled = n === 0;
  }
  function clearSel() {
    sel.clear();
    els.list.querySelectorAll('.geotag-tile.sel').forEach((t) => t.classList.remove('sel'));
    updateToolbar();
  }

  function startPlaceMode() {
    if (!sel.size) return;
    placeMode = { ids: [...sel] };
    els.banner.textContent = `Tap the map to place ${placeMode.ids.length} photo(s)  ·  Esc to cancel`;
    els.banner.style.display = 'block';
  }

  // ----- commit ------------------------------------------------------------
  async function commitPlace(ids, lngLat) {
    const lon = +lngLat.lng.toFixed(6), lat = +lngLat.lat.toFixed(6);
    try {
      await fetch(API, { method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ ids, lon, lat }) });
    } catch (e) { toast('Save failed — try again'); return; }
    removeFromGallery(ids);
    addNewMarkers(ids, [lon, lat]);
    reverseGeocode(lon, lat, ids.length);
  }
  async function commitSkip(ids) {
    if (!ids.length) return;
    try {
      await fetch(API, { method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ ids, status: 'skipped' }) });
    } catch (e) { toast('Save failed — try again'); return; }
    removeFromGallery(ids);
    toast(`Skipped ${ids.length}`);
  }

  // Remove placed/skipped photos WITHOUT a full re-render, so the scroll position
  // is preserved (re-rendering jumped the list back to the top).
  function removeFromGallery(ids) {
    const set = new Set(ids);
    for (const id of ids) { const t = els.list.querySelector(`[data-id="${id}"]`); if (t) t.remove(); }
    for (const g of groups) { g.items = g.items.filter((x) => !set.has(x.id)); g.ids = g.items.map((x) => x.id); }
    for (const g of groups) {                                  // update/remove headers
      const h = els.list.querySelector(`.geotag-group[data-gkey="${g.key}"]`);
      if (!g.items.length) { if (h) h.remove(); }
      else if (h) { const s = h.querySelector('span'); if (s) s.textContent = `${g.label} · ${g.items.length}`; }
    }
    groups = groups.filter((g) => g.items.length);
    rebuildFlat();
    // DOM still holds the first N remaining entries (a prefix of flat); resume there
    cursor = Math.min(Math.max(0, els.list.children.length - 1), flat.length);  // -1 for the sentinel
    ids.forEach((id) => sel.delete(id));
    updateCount();
    populateJump();
    updateToolbar();
    // if the list shrank so the sentinel scrolled into view, re-fire the observer
    if (io && sentinel && cursor < flat.length) { io.unobserve(sentinel); io.observe(sentinel); }
  }

  // ----- map feedback ------------------------------------------------------
  function ensureMarkerLayer() {
    if (map.getSource('geotagged-new')) return;
    map.addSource('geotagged-new', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
    map.addLayer({ id: 'geotagged-new', type: 'circle', source: 'geotagged-new',
      paint: { 'circle-radius': 6, 'circle-color': '#2ecc71',
        'circle-stroke-color': '#fff', 'circle-stroke-width': 2 } });
  }
  const newFeats = [];
  function addNewMarkers(ids, coord) {
    newFeats.push({ type: 'Feature', geometry: { type: 'Point', coordinates: coord },
      properties: { n: ids.length } });
    const s = map.getSource('geotagged-new');
    if (s) s.setData({ type: 'FeatureCollection', features: newFeats });
  }

  async function reverseGeocode(lon, lat, n) {
    let where = '';
    try {
      const r = await (await fetch(`https://nominatim.openstreetmap.org/reverse?format=jsonv2&lat=${lat}&lon=${lon}&zoom=12`)).json();
      where = r.name || (r.display_name || '').split(',').slice(0, 2).join(',');
    } catch {}
    toast(`Placed ${n} photo${n > 1 ? 's' : ''}${where ? ' · ' + where : ''}`);
  }

  let toastT;
  function toast(msg) {
    els.toast.textContent = msg;
    els.toast.style.display = 'block';
    clearTimeout(toastT);
    toastT = setTimeout(() => { els.toast.style.display = 'none'; }, 3200);
  }
})();
