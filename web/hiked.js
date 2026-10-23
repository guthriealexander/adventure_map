/* Adventure Map — mark OSM trail segments as "hiked".
 * Toggle the tool, tap trail segments (from the Trails overlay) to select them
 * (highlighted yellow), then "Add to hiked trails" saves their geometry to D1
 * (/api/hiked) and they render as the orange "Hiked trails" layer. Tap an
 * existing hiked segment (tool off) to remove it. */
(() => {
  let map, selecting = false;
  const sel = new Map();           // key -> { osm_id, name, geometry }
  const $ = (id) => document.getElementById(id);

  window.initHiked = (m) => {
    map = m;
    const btn = $('hiked-btn');
    if (!btn || !window.MAP_CONFIG.editing) return;     // stored by /api: editing only
    ensureSelectLayer();
    btn.onclick = () => (selecting ? exit() : enter());
    $('hiked-add').onclick = commit;
    $('hiked-done').onclick = exit;

    // select trail segments while the tool is on
    map.on('click', 'trails', (e) => { if (selecting && e.features[0]) toggleSeg(e.features[0]); });
    // remove an existing hiked segment (tool off)
    map.on('click', 'hiked', (e) => { if (!selecting && e.features[0]) deletePopup(e); });
    map.on('mouseenter', 'hiked', () => { if (!selecting) map.getCanvas().style.cursor = 'pointer'; });
    map.on('mouseleave', 'hiked', () => { if (!selecting) map.getCanvas().style.cursor = ''; });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && selecting) exit(); });
  };

  function ensureSelectLayer() {
    if (map.getSource('hiked-select')) return;
    map.addSource('hiked-select', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
    map.addLayer({ id: 'hiked-select', type: 'line', source: 'hiked-select',
      layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: { 'line-color': '#ffeb3b', 'line-width': 7, 'line-opacity': 0.9 } });
  }

  function enter() {
    selecting = true;
    window.__hikedSelecting = true;   // app.js wireLine checks this to suppress the trail popup
    if (map.getLayer('trails')) map.setLayoutProperty('trails', 'visibility', 'visible');
    const cb = document.querySelector('#overlays [data-ov="trails"]');
    if (cb) cb.checked = true;
    map.getCanvas().style.cursor = 'crosshair';
    $('hiked-btn').classList.add('active');
    $('hiked-bar').style.display = 'flex';
    update();
  }

  function exit() {
    selecting = false;
    window.__hikedSelecting = false;
    sel.clear(); render();
    map.getCanvas().style.cursor = '';
    $('hiked-btn').classList.remove('active');
    $('hiked-bar').style.display = 'none';
  }

  function segKey(f) {
    const c = JSON.stringify(f.geometry.coordinates);
    return `${f.properties.OSM_ID || '?'}|${f.geometry.type}|${c.length}|${c.slice(0, 36)}`;
  }

  function toggleSeg(f) {
    if (!f.geometry) return;
    const k = segKey(f);
    if (sel.has(k)) sel.delete(k);
    else sel.set(k, { osm_id: f.properties.OSM_ID, name: f.properties.name || null, geometry: f.geometry });
    render(); update();
  }

  function render() {
    const fc = { type: 'FeatureCollection',
      features: [...sel.values()].map((s) => ({ type: 'Feature', geometry: s.geometry, properties: {} })) };
    const src = map.getSource('hiked-select');
    if (src) src.setData(fc);
  }

  function update() {
    const n = sel.size;
    $('hiked-count').textContent = n ? `${n} segment${n > 1 ? 's' : ''} selected` : 'Tap trail segments to select';
    $('hiked-add').disabled = n === 0;
  }

  async function commit() {
    if (!sel.size) return;
    const features = [...sel.values()];
    try {
      await fetch('/api/hiked', { method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ features }) });
    } catch { toast('Save failed — try again'); return; }
    sel.clear(); render(); update();
    if (window._am && window._am.reloadHiked) window._am.reloadHiked();
    toast(`Added ${features.length} segment${features.length > 1 ? 's' : ''} to hiked trails`);
  }

  function deletePopup(e) {
    const p = e.features[0].properties || {};
    const name = (p.name || 'Hiked segment').toString().replace(/</g, '&lt;');
    const popup = new maplibregl.Popup({ maxWidth: '240px' })
      .setLngLat(e.lngLat)
      .setHTML(`<b>${name}</b><br><button id="hk-del" class="hk-del">Remove from hiked</button>`)
      .addTo(map);
    popup.getElement().addEventListener('click', async (ev) => {
      if (ev.target.id !== 'hk-del') return;
      try { await fetch('/api/hiked/' + p.id, { method: 'DELETE' }); } catch {}
      popup.remove();
      if (window._am && window._am.reloadHiked) window._am.reloadHiked();
    });
  }

  let toastT;
  function toast(msg) {
    const t = $('app-toast');   // reuse the shared toast element
    if (!t) return;
    t.textContent = msg; t.style.display = 'block';
    clearTimeout(toastT); toastT = setTimeout(() => { t.style.display = 'none'; }, 3000);
  }
})();
