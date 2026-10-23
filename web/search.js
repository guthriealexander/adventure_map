/* Adventure Map — place search (geocoding via OpenStreetMap Nominatim).
 * Type a place, pick a result, the map flies there and drops a pin. Self-wiring:
 * runs after the DOM + app.js, grabs the map from window._am at action time. */
(() => {
  const input = document.getElementById('search-input');
  const results = document.getElementById('search-results');
  if (!input || !results) return;

  const getMap = () => window._am && window._am.map;
  const esc = (s) => (s || '').replace(/[&<>"]/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  let timer, marker = null, lastQuery = '';

  async function run(q) {
    q = q.trim();
    if (q.length < 3) { hide(); return; }
    if (q === lastQuery) return;
    lastQuery = q;
    try {
      const url = `https://nominatim.openstreetmap.org/search?format=jsonv2&limit=6&q=${encodeURIComponent(q)}`;
      const data = await (await fetch(url, { headers: { 'Accept-Language': navigator.language || 'en' } })).json();
      if (input.value.trim() !== q) return;   // a newer keystroke won
      render(data);
    } catch {
      results.innerHTML = '<div class="search-empty">Search unavailable</div>';
      results.style.display = 'block';
    }
  }

  function render(data) {
    if (!data || !data.length) {
      results.innerHTML = '<div class="search-empty">No results</div>';
      results.style.display = 'block';
      return;
    }
    results._data = data;
    results.innerHTML = data.map((r, i) =>
      `<div class="search-item" data-i="${i}">${esc(r.display_name)}</div>`).join('');
    results.style.display = 'block';
  }

  function hide() { results.style.display = 'none'; }

  function go(r) {
    const map = getMap();
    if (!map) return;
    hide();
    input.value = (r.display_name || '').split(',')[0];
    const bb = r.boundingbox;   // [south, north, west, east] (strings)
    if (bb && bb.length === 4) {
      map.fitBounds([[+bb[2], +bb[0]], [+bb[3], +bb[1]]], { maxZoom: 15, padding: 60, duration: 1100 });
    } else {
      map.flyTo({ center: [+r.lon, +r.lat], zoom: 13, duration: 1100 });
    }
    if (marker) marker.remove();
    marker = new maplibregl.Marker({ color: '#e0414e' }).setLngLat([+r.lon, +r.lat]).addTo(map);
  }

  input.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(() => run(input.value), 350); });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { clearTimeout(timer); run(input.value); }
    if (e.key === 'Escape') { hide(); input.blur(); }
  });
  results.addEventListener('click', (e) => {
    const it = e.target.closest('.search-item');
    if (it && results._data) go(results._data[+it.dataset.i]);
  });
  document.addEventListener('click', (e) => { if (!e.target.closest('#search')) hide(); });
})();
