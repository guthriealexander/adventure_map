/* POI add/edit/delete UI. Talks to /api/pois (D1-backed, behind Access); off when
   the site is read-only (site.js editing: false). */
window.initPOIeditor = (map) => {
  if (!window.MAP_CONFIG.editing) return;
  const TYPES = ['Camp', 'Car Camp', 'Backpacking Camp', 'Hut', 'Cabin', 'Hotel',
    'Peak', 'Trailhead', 'PCT', 'Water', 'Viewpoint', 'Other'];

  let placing = false;
  let tempMarker = null;

  // --- floating add button + status bar ---
  const btn = document.createElement('button');
  btn.id = 'add-poi-btn';
  btn.title = 'Add a POI';
  btn.textContent = '＋';
  document.body.appendChild(btn);

  const bar = document.createElement('div');
  bar.id = 'add-poi-bar';
  bar.innerHTML = `Tap the map to place a pin &nbsp;
    <button id="poi-here">Use my location</button>
    <button id="poi-cancel">Cancel</button>`;
  document.body.appendChild(bar);

  const startPlacing = () => {
    placing = true; bar.style.display = 'block'; btn.style.display = 'none';
    map.getCanvas().style.cursor = 'crosshair';
  };
  const stopPlacing = () => {
    placing = false; bar.style.display = 'none'; btn.style.display = 'block';
    map.getCanvas().style.cursor = '';
  };
  btn.onclick = startPlacing;
  bar.querySelector('#poi-cancel').onclick = stopPlacing;
  bar.querySelector('#poi-here').onclick = () => {
    if (!navigator.geolocation) return alert('No geolocation available');
    navigator.geolocation.getCurrentPosition((pos) => {
      stopPlacing();
      openForm({ lon: pos.coords.longitude, lat: pos.coords.latitude });
    }, () => alert('Could not get your location'));
  };

  map.on('click', (e) => {
    if (!placing) return;
    stopPlacing();
    openForm({ lon: e.lngLat.lng, lat: e.lngLat.lat });
  });

  // --- the form (create or edit) ---
  function openForm(poi) {
    const editing = poi.id != null;
    const opts = TYPES.map((t) =>
      `<option ${poi.type === t ? 'selected' : ''}>${t}</option>`).join('');
    const html = `<div class="poi-form">
      <h3>${editing ? 'Edit' : 'New'} POI</h3>
      <label>Name<input id="pf-name" value="${(poi.name || '').replace(/"/g, '&quot;')}"></label>
      <label>Type<select id="pf-type"><option value=""></option>${opts}</select></label>
      <label>Note<textarea id="pf-note" rows="2">${poi.note || ''}</textarea></label>
      <div class="poi-coords">${poi.lat.toFixed(5)}, ${poi.lon.toFixed(5)}</div>
      <div class="poi-actions">
        ${editing ? '<button id="pf-del" class="danger">Delete</button>' : ''}
        <span style="flex:1"></span>
        <button id="pf-cancel">Cancel</button>
        <button id="pf-save" class="primary">Save</button>
      </div></div>`;
    const popup = new maplibregl.Popup({ maxWidth: '300px', closeOnClick: false })
      .setLngLat([poi.lon, poi.lat]).setHTML(html).addTo(map);

    const $ = (id) => document.getElementById(id);
    $('pf-cancel').onclick = () => popup.remove();
    $('pf-save').onclick = async () => {
      const body = {
        name: $('pf-name').value.trim(),
        type: $('pf-type').value || null,
        note: $('pf-note').value.trim() || null,
        lon: poi.lon, lat: poi.lat,
      };
      if (!body.name) return $('pf-name').focus();
      $('pf-save').disabled = true;
      const url = editing ? `/api/pois/${poi.id}` : '/api/pois';
      const r = await fetch(url, { method: editing ? 'PUT' : 'POST',
        headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
      if (r.ok) { popup.remove(); await window._am.reloadPOIs(); }
      else { $('pf-save').disabled = false; alert('Save failed'); }
    };
    if (editing) $('pf-del').onclick = async () => {
      if (!confirm('Delete this POI?')) return;
      const r = await fetch(`/api/pois/${poi.id}`, { method: 'DELETE' });
      if (r.ok) { popup.remove(); await window._am.reloadPOIs(); }
      else alert('Delete failed');
    };
  }

  // --- click existing POI -> edit ---
  map.on('mouseenter', 'pois', () => map.getCanvas().style.cursor = 'pointer');
  map.on('mouseleave', 'pois', () => { if (!placing) map.getCanvas().style.cursor = ''; });
  map.on('click', 'pois', (e) => {
    if (placing) return;
    e.preventDefault();
    const p = e.features[0].properties;
    openForm({ id: p.id, name: p.name, type: p.type, note: p.note,
      lon: e.lngLat.lng, lat: e.lngLat.lat });
  });
};
