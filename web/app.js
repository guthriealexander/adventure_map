/* Adventure Map — main app. Driven by window.MAP_CONFIG (config.js). */
(() => {
  const CFG = window.MAP_CONFIG;

  if (window.pmtiles) {
    const protocol = new pmtiles.Protocol();
    maplibregl.addProtocol('pmtiles', protocol.tile);
  }

  // Pick the basemap style: a one-shot fallback (set by the error handler below
  // after a failed style) wins, then the user's saved choice, then the default.
  // The one-shot is in sessionStorage and consumed immediately, so it never
  // clobbers the saved preference — the real choice is retried on the next reload.
  const STYLES = CFG.styles || [];
  const FALLBACK_STYLE = 'https://basemaps.cartocdn.com/gl/positron-gl-style/style.json';
  const styleById = (id) => STYLES.find((s) => s.id === id);
  let oneShotFallback = null;
  try { oneShotFallback = sessionStorage.getItem('am-style-fallback'); sessionStorage.removeItem('am-style-fallback'); } catch {}
  const savedStyleId = (() => { try { return localStorage.getItem('am-style'); } catch { return null; } })();
  const activeStyle = styleById(oneShotFallback) || styleById(savedStyleId)
    || styleById(CFG.defaultStyleId) || STYLES[0];
  // A style can recolour data layers for itself (`layerColors` in config.js).
  // Applied before the panel/layers are built, so the swatch shows it too.
  for (const [id, color] of Object.entries((activeStyle && activeStyle.layerColors) || {})) {
    const L = CFG.layers.find((x) => x.id === id);
    if (L) L.color = color;
  }

  const map = new maplibregl.Map({
    container: 'map',
    // a style entry is either a URL (vector style.json), an inline style object
    // (raster basemaps like USGS Topo, which ship tiles, not a style), or a `build()`
    // that returns one (a style written entirely in code).
    style: activeStyle ? (activeStyle.build ? activeStyle.build() : (activeStyle.style || activeStyle.url))
      : FALLBACK_STYLE,
    center: [-105.5, 39.5], zoom: 4, hash: true,
    // Styles whose data isn't described by a TileJSON (e.g. a terrain source added in
    // code) name their required credits in config; MapLibre shows the rest.
    ...(activeStyle && activeStyle.attribution
      ? { attributionControl: { compact: true, customAttribution: activeStyle.attribution } }
      : {}),
  });

  // Your POIs always draw on top of everything, in every style - above the basemap
  // labels, the styles' screen passes, the data layers and anything added later
  // (lazily loaded layers, trip highlights, filmstrip markers). After every addLayer,
  // the POI layer is moved back to the top of the stack.
  const TOP_LAYERS = ['pois'];
  const addLayerRaw = map.addLayer.bind(map);
  map.addLayer = (layer, beforeId) => {
    const r = addLayerRaw(layer, beforeId);
    for (const id of TOP_LAYERS) if (map.getLayer(id)) map.moveLayer(id);
    return r;
  };

  // Resilience: if the basemap STYLE fails to load (e.g. MapTiler 429/quota, a
  // network blip, or a 403), the whole app would otherwise brick — `load` never
  // fires, so no data/photos. Catch that one case and reload onto a key-free
  // style. Guarded to fire at most once (sessionStorage one-shot), so it can't
  // loop, and only before the style is up (later tile errors are unrelated).
  let _triedStyleFallback = false;
  map.on('error', (e) => {
    if (_triedStyleFallback || map.isStyleLoaded()) return;
    const err = e && e.error; const url = String((err && err.url) || ''); const status = err && err.status;
    const styleBase = String((activeStyle && activeStyle.url) || '').split('?')[0];
    // An inline style object (raster basemap) has no URL to fail — its tiles may
    // 403/404 individually, but that's not a style failure, so never fall back on it.
    if (!styleBase) return;
    const styleFailed = status === 429 || status === 403
      || url.split('?')[0] === styleBase;   // the active style URL itself failed
    if (!styleFailed) return;
    const fb = styleById(activeStyle && activeStyle.id === 'liberty' ? 'positron' : 'liberty') || styleById('positron');
    if (!fb || (activeStyle && fb.id === activeStyle.id)) return; // already on a key-free style
    _triedStyleFallback = true;
    console.warn(`Basemap style failed (status ${status}, ${url}) — falling back to ${fb.id}`);
    try { sessionStorage.setItem('am-style-fallback', fb.id); } catch {}
    location.reload();
  });
  // Symbols can ask for a style's runtime images before `load`; supply them on demand.
  if (activeStyle && activeStyle.onLoad) {
    map.on('styleimagemissing', () => { try { activeStyle.onLoad(map); } catch (e) { console.warn('style onLoad', e); } });
  }
  map.addControl(new maplibregl.NavigationControl(), 'top-right');
  // Globe / flat (Web Mercator) toggle, remembered between visits
  const PROJ_KEY = 'am-projection';
  const savedProjection = () => { try { return localStorage.getItem(PROJ_KEY) === 'mercator' ? 'mercator' : 'globe'; } catch { return 'globe'; } };
  const globeCtl = new maplibregl.GlobeControl();
  map.addControl(globeCtl, 'top-right');
  if (globeCtl._container) globeCtl._container.addEventListener('click', () => setTimeout(() => {
    const type = (map.getProjection() || {}).type === 'mercator' ? 'mercator' : 'globe';
    try { localStorage.setItem(PROJ_KEY, type); } catch {}
  }));
  map.addControl(new maplibregl.GeolocateControl({
    positionOptions: { enableHighAccuracy: true }, trackUserLocation: true,
  }), 'top-right');
  map.addControl(new maplibregl.ScaleControl(), 'bottom-left');

  const panel = document.getElementById('layers');

  // ----- POI type icons ----------------------------------------------------
  // "Fieldnotes POI" set: 32x32 map markers (forest-green glyph on a white round
  // backplate, centre anchor) in icons/poi/. Each SVG is rasterized at 3x and
  // registered as a map image; icon-image picks by POI type. PCT sub-types reuse
  // the base glyph (a PCT camp is a camp); the rest of the PCT stops get the
  // evergreen PCT badge.
  const POI_ICONS = {
    'Camp': 'camp', 'Car Camp': 'car-camp', 'Backpacking Camp': 'backpacking-camp',
    'Hut': 'hut', 'Cabin': 'cabin', 'Hotel': 'hotel', 'Peak': 'peak',
    'Trailhead': 'trailhead', 'Water': 'water', 'Viewpoint': 'viewpoint', 'Other': 'other',
    'PCT': 'pct', 'PCT Package': 'pct', 'PCT Trail Angel': 'pct',
    'PCT Camp': 'camp', 'PCT Camp 1': 'camp', 'PCT Camp 2': 'camp', 'PCT Hotel': 'hotel',
  };
  const DEFAULT_ICON = 'other';
  const POI_ICON_PX = 32, POI_ICON_SCALE = 3;   // display size, raster density

  // Register every icon: a transparent placeholder first (so the symbol layer can
  // be added straight away without "missing image" warnings), then swap in the
  // rasterized SVG once it has decoded.
  function registerPoiIcons() {
    const px = POI_ICON_PX * POI_ICON_SCALE;
    for (const name of new Set([...Object.values(POI_ICONS), DEFAULT_ICON])) {
      const key = 'poi-' + name;
      if (map.hasImage(key)) continue;
      map.addImage(key, { width: px, height: px, data: new Uint8Array(px * px * 4) },
        { pixelRatio: POI_ICON_SCALE });
      const img = new Image(px, px);
      img.onload = () => {
        const c = document.createElement('canvas');
        c.width = c.height = px;
        const ctx = c.getContext('2d');
        ctx.drawImage(img, 0, 0, px, px);   // SVG draws as vector at this size
        if (map.hasImage(key)) map.updateImage(key, ctx.getImageData(0, 0, px, px));
      };
      img.onerror = () => console.warn('POI icon failed to load:', name);
      img.src = `icons/poi/${name}.svg`;
    }
  }

  function poiIconExpression() {
    const expr = ['match', ['get', 'type']];
    for (const [type, name] of Object.entries(POI_ICONS)) expr.push(type, 'poi-' + name);
    expr.push('poi-' + DEFAULT_ICON); // fallback: untyped / unknown
    return expr;
  }

  // The basemap layer to insert terrain/overlays BELOW: its first road
  // (source-layer "transportation") line, else its first label (symbol), else the
  // very first layer. Anchoring on the road keeps roads + labels drawn on TOP of
  // the hillshade and the trails overlay, while they sit above the landcover/water
  // fills — works across every OpenMapTiles-schema style (Outdoor/Liberty/Positron),
  // some of which draw landcover after their first generic line layer.
  function basemapUnderlayBeforeId() {
    const ls = map.getStyle().layers || [];
    const road = ls.find((l) => l.type === 'line' && l['source-layer'] === 'transportation');
    if (road) return road.id;
    const sym = ls.find((l) => l.type === 'symbol');
    if (sym) return sym.id;
    // Raster basemap (USGS Topo): nothing to slot *under* — its roads/labels are
    // baked into the image. Returning undefined puts terrain/overlays ON TOP of it
    // (below the data layers, which are added later), instead of hidden beneath.
    return undefined;
  }

  // true when the basemap is a plain raster image (no vector fills/lines/labels)
  function isRasterBasemap() {
    const ls = map.getStyle().layers || [];
    return !ls.some((l) => l.type === 'fill' || l.type === 'line' || l.type === 'symbol');
  }

  // ----- raster base layers (hillshade / imagery) --------------------------
  // Drawn BELOW everything: we insert them under the vector basemap's first
  // layer and make that opaque background transparent so terrain/imagery show
  // through, with the basemap's water/roads/labels and all data drawn on top.
  function setupBasemap() {
    const B = CFG.basemap || {};
    const styleLayers = map.getStyle().layers || [];
    const rasterBase = isRasterBasemap();
    // On a raster basemap the base image is opaque, so "below the first layer"
    // would hide these — put them on top of it instead (undefined = on top).
    const firstId = rasterBase
      ? undefined
      : (styleLayers.length ? styleLayers[0].id : undefined);   // 'background'
    if (map.getLayer('background')) map.setPaintProperty('background', 'background-opacity', 0);

    const hillBefore = basemapUnderlayBeforeId();
    // Styles that already bake in shaded relief + contours (USGS Topo) start with
    // our hillshade off — doubling it up just muddies the topo.
    const styleHasRelief = !!(activeStyle && activeStyle.hasRelief);
    const hillOn = !!(B.hillshade && B.hillshade.defaultOn) && !styleHasRelief;

    // A style that labels summits with a 'peak' icon gets this small triangle from
    // the app; without it MapLibre warns every frame.
    if (map.getStyle().layers.some((l) => l.id === 'peak-label') && !map.hasImage('peak')) {
      const px = 36, c = document.createElement('canvas');
      c.width = c.height = px;
      const ctx = c.getContext('2d');
      ctx.strokeStyle = '#25303a'; ctx.lineWidth = 3; ctx.lineJoin = 'round';
      ctx.beginPath(); ctx.moveTo(4, px - 5); ctx.lineTo(px / 2, 5); ctx.lineTo(px - 4, px - 5);
      ctx.closePath(); ctx.stroke();
      map.addImage('peak', ctx.getImageData(0, 0, px, px), { pixelRatio: 3 });
    }
    // A style's screen pass works on the frame drawn beneath it; inserting it here
    // (before any of our data layers are added) keeps photos, tracks and POIs sharp on top.
    // A style can bring runtime images it references (`onLoad`).
    if (activeStyle && activeStyle.onLoad) {
      try { activeStyle.onLoad(map); } catch (e) { console.warn('style onLoad', e); }
    }
    // `paintLayer` lets a style supply its own screen pass: a custom WebGL layer that
    // reads the frame drawn so far and post-processes it.
    const makePass = activeStyle && activeStyle.paintLayer;
    const hasPass = map.getStyle().layers.some((l) => l.type === 'custom');
    if (makePass && !hasPass) {
      try {
        const paint = makePass();
        map.addLayer(paint, activeStyle.dataBefore);
        // The pass repaints the whole screen every frame. A style can ask for a
        // lighter pass while the map moves (paintWhileMoving: false -> `fast`: the
        // shader skips its costliest part) and the full one once you stop. Switching
        // the pass off while moving made the shading snap plain/painted on every pan.
        if (activeStyle.paintWhileMoving === false) {
          map.on('movestart', () => { paint.fast = 1; });
          map.on('moveend', () => { paint.fast = 0; map.triggerRepaint(); });
        }
      } catch (e) { console.warn('screen pass layer', e); }
    }
    // 3D terrain is the most expensive thing on the map and does almost nothing
    // looking straight down, so switch it on only when the view is pitched.
    if (activeStyle && activeStyle.terrain3d && map.getSource('dem-terrain')) {
      const TERRAIN = { source: 'dem-terrain', exaggeration: activeStyle.exaggeration ?? 1.3 };
      const syncTerrain = () => {
        if (window._am && window._am.terrainLocked) return;   // clips.js holds its own terrain while in a clip
        const want = map.getPitch() > 15;
        const has = !!map.getTerrain();
        if (want && !has) map.setTerrain(TERRAIN);
        else if (!want && has) map.setTerrain(null);
      };
      syncTerrain();
      map.on('pitchend', syncTerrain);
      map.on('moveend', syncTerrain);
    }
    // A style can bring its own relief (`hasRelief`) — don't add a second one.
    if (B.hillshade && !map.getLayer('hillshade')) {
      map.addSource('hillshade', {
        type: 'raster', tiles: [B.hillshade.url], tileSize: 256,
        minzoom: B.hillshade.minzoom, maxzoom: B.hillshade.maxzoom,
        attribution: B.hillshade.attribution,
      });
      map.addLayer({ id: 'hillshade', type: 'raster', source: 'hillshade',
        paint: { 'raster-opacity': B.hillshade.defaultOpacity ?? 0.6 } }, hillBefore);
      if (!hillOn) map.setLayoutProperty('hillshade', 'visibility', 'none');
    }
    if (B.imagery) {
      map.addSource('imagery', {
        type: 'raster', tiles: [B.imagery.url], tileSize: 256,
        minzoom: B.imagery.minzoom ?? 0, maxzoom: B.imagery.maxzoom ?? 19,
        attribution: B.imagery.attribution,
      });
      // beforeId=firstId again -> lands just below background, above hillshade
      map.addLayer({ id: 'imagery', type: 'raster', source: 'imagery',
        paint: { 'raster-opacity': 1 } }, firstId);
      map.setLayoutProperty('imagery', 'visibility', B.imagery.defaultOn ? 'visible' : 'none');
    }
    wireBasemapUI();
    wireStyleSelector();
  }

  // Basemap style picker: persist the choice and reload (map position is kept via
  // the URL hash). Reloading re-runs the full init so every feature + the editor
  // modules' own layers come back cleanly under the new style.
  function wireStyleSelector() {
    const sel = document.getElementById('basemap-style');
    if (!sel || !STYLES.length) return;
    sel.innerHTML = STYLES.map((s) => `<option value="${s.id}">${s.label}</option>`).join('');
    sel.value = (activeStyle && activeStyle.id) || STYLES[0].id;
    sel.onchange = () => {
      try { localStorage.setItem('am-style', sel.value); } catch {}
      location.reload();
    };
  }

  function wireBasemapUI() {
    const B = CFG.basemap || {};
    const hs = document.getElementById('bm-hillshade');
    const op = document.getElementById('bm-hillshade-opacity');
    const im = document.getElementById('bm-imagery');
    if (hs) {
      // mirror the actual layer state (styles with baked-in relief start it off)
      hs.checked = !!(map.getLayer('hillshade')
        ? (map.getLayoutProperty('hillshade', 'visibility') || 'visible') === 'visible'
        : (B.hillshade && B.hillshade.defaultOn));
      hs.onchange = () => map.getLayer('hillshade') &&
        map.setLayoutProperty('hillshade', 'visibility', hs.checked ? 'visible' : 'none');
    }
    if (op) {
      op.value = Math.round((B.hillshade?.defaultOpacity ?? 0.6) * 100);
      op.oninput = () => map.getLayer('hillshade') &&
        map.setPaintProperty('hillshade', 'raster-opacity', op.value / 100);
    }
    // Contour lines + their elevation labels (a style's contour-* layers). Only the
    // layers the style starts visible are toggled, so a hidden alternate-unit set
    // (contour-m) stays hidden. Row is hidden on styles without contours.
    const ct = document.getElementById('bm-contours');
    const ctRow = document.getElementById('bm-contours-row');
    const contourIds = map.getStyle().layers
      .filter((l) => l.id.startsWith('contour-') && (l.layout?.visibility || 'visible') === 'visible')
      .map((l) => l.id);
    if (ctRow) ctRow.style.display = contourIds.length ? '' : 'none';
    if (ct && contourIds.length) {
      const apply = () => contourIds.forEach((id) =>
        map.setLayoutProperty(id, 'visibility', ct.checked ? 'visible' : 'none'));
      // a style can start with its contours off (`contoursOff`); the box still brings them back
      ct.checked = !(activeStyle && activeStyle.contoursOff);
      ct.onchange = apply;
      apply();
    }
    // Blur distance (styles with a depth-of-field pass set `focus`): slider 0-100 ->
    // blur start at 35%..100% of the screen height; 100 = no blur. Remembered per browser.
    const bl = document.getElementById('bm-blur');
    const blRow = document.getElementById('bm-blur-row');
    const FOC = activeStyle && activeStyle.focus;
    if (blRow) blRow.style.display = FOC ? '' : 'none';
    if (bl && FOC) {
      let saved = null;
      try { saved = localStorage.getItem('am-blur'); } catch {}
      bl.value = saved != null ? saved : Math.round(((FOC.initial ?? 0.7) - 0.35) / 0.65 * 100);
      const applyBlur = () => {
        FOC.set(0.35 + (Number(bl.value) / 100) * 0.65);
        try { localStorage.setItem('am-blur', bl.value); } catch {}
      };
      bl.oninput = applyBlur;
      applyBlur();
    }
    if (im) {
      im.checked = !!(B.imagery && B.imagery.defaultOn);
      const applyImagery = () => {
        const on = im.checked;
        if (map.getLayer('imagery')) map.setLayoutProperty('imagery', 'visibility', on ? 'visible' : 'none');
        setBasemapFillsVisible(!on);   // over satellite, keep only roads + labels
      };
      im.onchange = applyImagery;
      applyImagery();                  // apply initial state
    }
  }

  // Over satellite we want only roads + labels. Hide every basemap *fill* layer —
  // that's water, landuse/urban, parks, and building footprints — keeping the line
  // (road) and symbol (label) layers. Restored when imagery is toggled off.
  function setBasemapFillsVisible(show) {
    for (const l of map.getStyle().layers) {
      if (l.type === 'fill' || l.type === 'fill-extrusion') {
        map.setLayoutProperty(l.id, 'visibility', show ? 'visible' : 'none');
      }
    }
  }

  // ----- vector overlays (trails, etc.) ------------------------------------
  // Drawn above the basemap but below the data layers (added later in load).
  // Each gets a toggle + opacity slider, built into #overlays.
  function setupOverlays() {
    const list = CFG.overlays || [];
    const host = document.getElementById('overlays');
    const ovById = Object.fromEntries(list.map((O) => [O.id, O]));
    for (const O of list) {
      // tileMinzoom/tileMaxzoom = tile availability (source); minzoom/maxzoom are
      // the layer's *visibility* range, controlled in the drawer like data layers.
      // A style may already carry the same tileset under this id (e.g. its own
      // "trails" source) — reuse it rather than adding a duplicate, which
      // MapLibre rejects and which would abort the rest of the load.
      if (!map.getSource(O.id)) {
        map.addSource(O.id, {
          type: 'vector', tiles: [O.url],
          minzoom: O.tileMinzoom ?? 0, maxzoom: O.tileMaxzoom ?? 14, attribution: O.attribution,
        });
      }
      // Insert BELOW the basemap's roads + labels (above its landcover) so the
      // overlay reads as terrain context, not on top of the map's road network.
      map.addLayer({ id: O.id, type: 'line', source: O.id, 'source-layer': O.sourceLayer,
        layout: { 'line-cap': 'round', 'line-join': 'round',
          visibility: O.defaultOn ? 'visible' : 'none' },
        paint: { 'line-color': O.color, 'line-opacity': O.defaultOpacity ?? 0.85,
          'line-width': effWidth(O),
          ...(DASH[effDash(O)] ? { 'line-dasharray': DASH[effDash(O)] } : {}) } },
        basemapUnderlayBeforeId());
      wireLine(O.id);
      applyZoomTo(O, [O.id]);
      if (host) {
        const row = document.createElement('div');
        row.className = 'layer-block';
        row.innerHTML =
          `<label class="bm-row"><input type="checkbox" data-ov="${O.id}" ` +
          `${O.defaultOn ? 'checked' : ''}><input type="color" class="swatch-color line" data-ovcolor="${O.id}" ` +
          `value="${O.color}" title="Overlay color (live)">${O.label}` +
          `<button type="button" class="ctl-toggle" data-ovexpand="${O.id}" title="Style controls">⚙</button></label>` +
          `<div class="bm-opacity"><span>Opacity</span><input type="range" data-ovop="${O.id}" ` +
          `min="0" max="100" value="${Math.round((O.defaultOpacity ?? 0.85) * 100)}"></div>` +
          `<div class="layer-controls" id="octl-${O.id}">${buildControls(O, 'lines')}</div>`;
        host.appendChild(row);
      }
    }
    if (host) {
      host.addEventListener('change', (e) => {
        const id = e.target.dataset.ov;
        if (id && map.getLayer(id))
          map.setLayoutProperty(id, 'visibility', e.target.checked ? 'visible' : 'none');
      });
      host.addEventListener('click', (e) => {
        const id = e.target.dataset.ovexpand;
        if (!id) return;
        const d = document.getElementById('octl-' + id);
        if (d) d.classList.toggle('open');
      });
      host.addEventListener('input', (e) => {
        const t = e.target;
        const opId = t.dataset.ovop;
        if (opId && map.getLayer(opId)) { map.setPaintProperty(opId, 'line-opacity', t.value / 100); return; }
        if (t.dataset.ovcolor) {   // live overlay recolor
          const O = ovById[t.dataset.ovcolor];
          if (O) O.color = t.value;
          if (map.getLayer(t.dataset.ovcolor)) map.setPaintProperty(t.dataset.ovcolor, 'line-color', t.value);
          return;
        }
        const lid = t.dataset.layer, prop = t.dataset.prop;   // drawer controls (weight/dash/zoom)
        if (lid && prop && ovById[lid]) {
          applyStyleTo(ovById[lid], [lid], prop, t.value);
          const val = t.parentElement && t.parentElement.querySelector('.ctl-val');
          if (val) val.textContent = t.value;
        }
      });
    }
  }

  // ----- rendering helpers -------------------------------------------------
  // Stacking order follows CFG.layers (first = bottom, last = top): Google
  // history at the bottom as background, then tracks, then photos, POIs on top.
  // We compute a beforeId so layers land in the right z-order no matter what
  // order they're toggled on in.
  // A style can also set `dataLinesBefore`: then lines, raw points
  // and isochrones go under ITS screen pass (so they share its depth-of-field blur),
  // while photos and POIs stay above it, crisp and clickable. Each group only
  // stacks against its own members, so the two never interleave across the pass.
  const UNDER_PASS_KINDS = new Set(['lines', 'points', 'isochrones']);
  function beforeIdFor(L) {
    const split = !!(activeStyle && activeStyle.dataLinesBefore && map.getLayer(activeStyle.dataLinesBefore));
    const under = (x) => split && UNDER_PASS_KINDS.has(x.kind);
    const idx = CFG.layers.findIndex((x) => x.id === L.id);
    for (let i = idx + 1; i < CFG.layers.length; i++) {
      const X = CFG.layers[i];
      if (under(X) === under(L) && map.getLayer(X.id)) return X.id; // insert below it
    }
    if (under(L)) return activeStyle.dataLinesBefore;
    // nothing of ours above yet. On a style with a `dataBefore` anchor sit just under
    // it (e.g. above the style's screen pass but below its labels); otherwise go on top.
    return (activeStyle && activeStyle.dataBefore && map.getLayer(activeStyle.dataBefore))
      ? activeStyle.dataBefore : undefined;
  }

  // config layer id -> [actual map layer ids] (clustered photos use 3)
  const layerIds = {};

  // ----- hidden photos (filtered out of the map + filmstrip) ---------------
  let hiddenPhotoIds = [];
  // A selected trip (trips.js) narrows the photos to its dates: [startISO, endISO].
  let tripRange = null, tripPhotosUrl = null;
  // Junk (screenshots, receipts, blurry shots - flagged `junk` by tag_photos.py) is
  // hidden unless "Show junk photos" is ticked in the Photos layer settings.
  let showJunk = false;
  function notHiddenFilter() {
    const f = ['!', ['in', ['get', 'id'], ['literal', hiddenPhotoIds]]];
    return showJunk ? f : ['all', f, ['!', ['to-boolean', ['get', 'junk']]]];
  }
  // circle-sort-key: higher draws on top (and is hit first): 360s / panos over the rest
  const PANO_ON_TOP = ['case', ['==', ['to-string', ['get', 'is_360']], 'true'], 1, 0];

  function photosHiddenFilter() {
    const notHidden = notHiddenFilter();
    if (!tripRange) return notHidden;
    const ts = ['coalesce', ['get', 'timestamp'], ''];
    return ['all', notHidden, ['>=', ts, tripRange[0]], ['<=', ts, tripRange[1] + 'T23:59:59']];
  }
  function applyPhotoFilter() {
    const f = photosHiddenFilter();
    // While a trip is open its photos come from the trip's own file ('trip-photos'):
    // the tiled dots are thinned below z9, so hide them; the z12+ thumbnails are
    // complete and just take the date filter.
    const off = ['==', ['get', 'id'], ' '];
    for (const id of ['photos', 'photos-hit']) if (map.getLayer(id)) map.setFilter(id, tripRange ? off : f);
    if (map.getLayer('photos-thumb')) map.setFilter('photos-thumb', f);
    for (const id of ['trip-photos', 'trip-photos-hit']) if (map.getLayer(id)) map.setFilter(id, notHiddenFilter());
  }

  // A trip's photos as their own layer, styled like the photo dots and clickable the
  // same way. Created on first use, emptied when the trip is closed.
  async function showTripPhotos(url) {
    const empty = { type: 'FeatureCollection', features: [] };
    let data = empty;
    if (url) { try { data = await (await fetch(url)).json(); } catch (e) { console.warn('trip photos', e); } }
    if (!map.getSource('trip-photos')) {
      if (!map.getLayer('photos')) return;            // photos layer off: nothing to mirror
      map.addSource('trip-photos', { type: 'geojson', data });
      const before = map.getLayer('photos-thumb') ? 'photos-thumb' : undefined;
      // a little larger than the normal dots, with a white ring, so they read on top
      // of the highlighted route
      map.addLayer({ id: 'trip-photos', type: 'circle', source: 'trip-photos',
        filter: notHiddenFilter(),
        paint: {
          'circle-radius': ['interpolate', ['linear'], ['zoom'], 3, 3.5, 9, 4.5, 12, 6],
          'circle-color': map.getPaintProperty('photos', 'circle-color'),
          'circle-stroke-color': '#ffffff',
          'circle-stroke-width': 1.4,
        } }, before);
      map.addLayer({ id: 'trip-photos-hit', type: 'circle', source: 'trip-photos',
        filter: notHiddenFilter(),
        paint: { 'circle-radius': ['interpolate', ['linear'], ['zoom'], 3, 6, 12, 14, 16, 20],
          'circle-color': '#000', 'circle-opacity': 0 } }, before);
      wirePhoto('trip-photos');
      wirePhoto('trip-photos-hit');
    } else {
      map.getSource('trip-photos').setData(data);
    }
  }
  window.hidePhoto = async (id) => {
    if (!id) return;
    if (!hiddenPhotoIds.includes(id)) hiddenPhotoIds.push(id);
    applyPhotoFilter();
    document.querySelectorAll('.maplibregl-popup').forEach((p) => p.remove());
    collapseSpider();
    try {
      await fetch('/api/hidden', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ id }) });
    } catch {}
    if (window.filmstripRefresh) window.filmstripRefresh();
  };

  // ----- per-layer style model (color, line dash/weight, point halo, zoom) -
  // Named dash styles -> line-dasharray (units of line-width; round caps make
  // [1,..] read as dots). 'solid' clears the dash.
  const DASH = { solid: null, dashed: [3, 2], dotted: [1, 2], dashdot: [4, 2, 1, 2] };
  const DASH_NAMES = Object.keys(DASH);

  // Effective current value = live edit (on the obj) || config default. Used by
  // both the controls UI and the config export, for layers and overlays alike.
  const effWidth     = (o) => (o.width != null ? o.width : 2);
  const effDash      = (o) => (o.dash || 'solid');
  const effHaloColor = (o) => (o.haloColor || (o.kind === 'photos' ? '#222222' : '#000000'));
  const effHaloWidth = (o) => (o.haloWidth != null ? o.haloWidth : (o.kind === 'photos' ? 0.7 : 0));
  const effMinZoom   = (o) => (o.minzoom != null ? o.minzoom : 0);
  const effMaxZoom   = (o) => (o.maxzoom != null ? o.maxzoom : 24);
  const isoMinutes   = (o) => (o.minutes != null ? o.minutes : o.minMinutes);
  // Photo dot radius at low zoom (far) and at z12 (near, just before thumbnails
  // take over). Linear between — monotonic, so dots no longer shrink near z12.
  const effDotFar    = (o) => (o.dotFar != null ? o.dotFar : 2);
  const effDotNear   = (o) => (o.dotNear != null ? o.dotNear : 4);
  const photoRadiusExpr = (o) =>
    ['interpolate', ['linear'], ['zoom'], 3, effDotFar(o), 12, effDotNear(o)];

  function applyZoomTo(obj, ids) {
    let min = effMinZoom(obj), max = effMaxZoom(obj);
    if (min >= max) max = min + 1;                       // setLayerZoomRange needs min < max
    for (const id of ids) {
      if (!map.getLayer(id)) continue;
      const isThumb = id.endsWith('-thumb');             // photo thumbnails stay a zoom>=12 feature
      map.setLayerZoomRange(id, isThumb ? Math.max(12, min) : min, max);
    }
  }

  // Style-supplied decor layers (see addLayerRender): kept out of recolour/dash,
  // and their width stays `am:widthPad` wider than the line they dress.
  const decorMeta = (id) => (map.getLayer(id) && map.getStyle().layers.find((l) => l.id === id)?.metadata) || {};
  const isDecor = (id) => !!decorMeta(id)['am:decor'];
  const decorPad = (id) => decorMeta(id)['am:widthPad'] || 0;

  // Live-apply one style prop to an obj's map sub-layers (and remember it on the
  // obj so a not-yet-loaded layer adopts it + the export reflects it). Works for
  // both data layers (ids from layerIds) and overlays (ids = [O.id]).
  function applyStyleTo(obj, ids, prop, raw) {
    const isType = (id, t) => map.getLayer(id) && map.getLayer(id).type === t;
    if (prop === 'width') {
      obj.width = parseFloat(raw);
      ids.forEach((id) => { if (isType(id, 'line')) map.setPaintProperty(id, 'line-width', obj.width + decorPad(id)); });
    } else if (prop === 'dash') {
      obj.dash = raw;
      ids.forEach((id) => { if (isType(id, 'line') && !isDecor(id)) map.setPaintProperty(id, 'line-dasharray', DASH[raw] || null); });
    } else if (prop === 'haloColor') {
      obj.haloColor = raw;
      ids.forEach((id) => { if (isType(id, 'circle')) map.setPaintProperty(id, 'circle-stroke-color', raw); });
    } else if (prop === 'haloWidth') {
      obj.haloWidth = parseFloat(raw);
      ids.forEach((id) => { if (isType(id, 'circle')) map.setPaintProperty(id, 'circle-stroke-width', obj.haloWidth); });
    } else if (prop === 'dotFar' || prop === 'dotNear') {
      obj[prop] = parseFloat(raw);
      const r = photoRadiusExpr(obj);
      ids.forEach((id) => { if (isType(id, 'circle')) map.setPaintProperty(id, 'circle-radius', r); });
    } else if (prop === 'minzoom' || prop === 'maxzoom') {
      obj[prop] = parseFloat(raw);
      applyZoomTo(obj, ids);
    }
  }

  // Build the expandable controls drawer for a layer/overlay, by kind.
  function buildControls(o, kind) {
    let s = '';
    if (kind === 'lines') {
      s += `<div class="ctl-row"><span>Weight</span>` +
        `<input type="range" data-layer="${o.id}" data-prop="width" min="0.5" max="10" step="0.5" value="${effWidth(o)}">` +
        `<b class="ctl-val">${effWidth(o)}</b></div>`;
      s += `<div class="ctl-row"><span>Dash</span><select data-layer="${o.id}" data-prop="dash">` +
        DASH_NAMES.map((n) => `<option value="${n}" ${effDash(o) === n ? 'selected' : ''}>${n}</option>`).join('') +
        `</select></div>`;
    } else if (kind === 'points' || kind === 'photos') {
      if (kind === 'photos') {
        s += `<label class="ctl-row"><input type="checkbox" id="show-junk" ${showJunk ? 'checked' : ''}>` +
          `<span>Show junk photos</span></label>`;
        s += `<div class="ctl-row"><span>Size far</span>` +
          `<input type="range" data-layer="${o.id}" data-prop="dotFar" min="0.5" max="10" step="0.25" value="${effDotFar(o)}">` +
          `<b class="ctl-val">${effDotFar(o)}</b></div>`;
        s += `<div class="ctl-row"><span>Size near</span>` +
          `<input type="range" data-layer="${o.id}" data-prop="dotNear" min="0.5" max="12" step="0.25" value="${effDotNear(o)}">` +
          `<b class="ctl-val">${effDotNear(o)}</b></div>`;
      }
      s += `<div class="ctl-row"><span>Halo</span>` +
        `<input type="color" class="swatch-color" data-layer="${o.id}" data-prop="haloColor" value="${effHaloColor(o)}"></div>`;
      s += `<div class="ctl-row"><span>Halo width</span>` +
        `<input type="range" data-layer="${o.id}" data-prop="haloWidth" min="0" max="5" step="0.1" value="${effHaloWidth(o)}">` +
        `<b class="ctl-val">${effHaloWidth(o)}</b></div>`;
    }
    s += `<div class="ctl-row"><span>Min zoom</span>` +
      `<input type="range" data-layer="${o.id}" data-prop="minzoom" min="0" max="24" step="1" value="${effMinZoom(o)}">` +
      `<b class="ctl-val">${effMinZoom(o)}</b></div>`;
    s += `<div class="ctl-row"><span>Max zoom</span>` +
      `<input type="range" data-layer="${o.id}" data-prop="maxzoom" min="0" max="24" step="1" value="${effMaxZoom(o)}">` +
      `<b class="ctl-val">${effMaxZoom(o)}</b></div>`;
    return s;
  }

  // One config fragment line: "id: { color:'#..', width:.., dash:'..', minzoom:.., maxzoom:.. }".
  function configFragment(o, isOverlay) {
    const p = [];
    if (o.kind !== 'pois') p.push(`color:'${o.color}'`);
    if (isOverlay || o.kind === 'lines') p.push(`width:${effWidth(o)}`, `dash:'${effDash(o)}'`);
    if (o.kind === 'photos') p.push(`dotFar:${effDotFar(o)}`, `dotNear:${effDotNear(o)}`);
    if (o.kind === 'points' || o.kind === 'photos') p.push(`haloColor:'${effHaloColor(o)}'`, `haloWidth:${effHaloWidth(o)}`);
    p.push(`minzoom:${effMinZoom(o)}`, `maxzoom:${effMaxZoom(o)}`);
    return `${o.id}: { ${p.join(', ')} }`;
  }

  function addLayerRender(L) {
    const src = L.id;
    const before = beforeIdFor(L);
    const ids = [];
    if (L.kind === 'isochrones') {
      // One tileset of nested drive-time polygons; a filter shows only the band
      // for the slider's minutes (they're nested, so that IS the reachable area).
      const slyr = L.format === 'pmtiles' ? { 'source-layer': L.sourceLayer } : {};
      const filt = ['==', ['get', 'minutes'], isoMinutes(L)];
      map.addLayer({ id: src, type: 'fill', source: src, ...slyr, filter: filt,
        paint: { 'fill-color': L.color, 'fill-opacity': 0.22 } }, before);
      map.addLayer({ id: src + '-line', type: 'line', source: src, ...slyr, filter: filt,
        layout: { 'line-join': 'round' },
        paint: { 'line-color': L.color, 'line-width': 1.6, 'line-opacity': 0.85 } }, before);
      ids.push(src, src + '-line');
      wireIsochrone(src);
    } else if (L.kind === 'lines') {
      const hiked = L.id === 'hiked';
      // A style can dress a data line with extra layers drawn beneath it (casings,
      // shadows: `decorateLine`) and tweak its paint. Decor layers carry
      // metadata 'am:decor', so recolour/dash leave them be and width keeps their pad.
      const base = { source: src,
        ...(L.format === 'pmtiles' ? { 'source-layer': L.sourceLayer } : {}),
        ...(L.filter ? { filter: L.filter } : {}) };
      const decor = (activeStyle && activeStyle.decorateLine && activeStyle.decorateLine(L, base, effWidth(L))) || {};
      for (const d of (decor.below || [])) { map.addLayer(d, before); ids.push(d.id); }
      map.addLayer({ id: src, type: 'line', source: src,
        ...(L.format === 'pmtiles' ? { 'source-layer': L.sourceLayer } : {}),
        ...(L.filter ? { filter: L.filter } : {}),
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: { 'line-color': L.colorExpr || L.color,   // colorExpr = data-driven (e.g. split car vs foot)
          // widthExpr = data-driven width (e.g. roads travelled, by trip count)
          'line-width': hiked ? ['interpolate', ['linear'], ['zoom'], 8, 2.5, 14, 5]
            : (L.widthExpr || effWidth(L)),
          ...(DASH[effDash(L)] ? { 'line-dasharray': DASH[effDash(L)] } : {}),
          'line-opacity': hiked ? 0.95 : 0.75, ...(decor.paint || {}) } }, before);
      ids.push(src);
      wireLine(src);
    } else if (L.kind === 'photos' && L.cluster) {
      map.addLayer({ id: src + '-clusters', type: 'circle', source: src,
        filter: ['has', 'point_count'],
        paint: { 'circle-color': L.color, 'circle-opacity': 0.82,
          'circle-stroke-color': '#fff', 'circle-stroke-width': 1.5,
          'circle-radius': ['step', ['get', 'point_count'], 13, 25, 17, 100, 22, 1000, 30] } }, before);
      map.addLayer({ id: src + '-count', type: 'symbol', source: src,
        filter: ['has', 'point_count'],
        layout: { 'text-field': ['get', 'point_count_abbreviated'], 'text-size': 12 },
        paint: { 'text-color': '#fff' } }, before);
      map.addLayer({ id: src, type: 'circle', source: src,
        filter: ['!', ['has', 'point_count']],
        layout: { 'circle-sort-key': PANO_ON_TOP },
        paint: { 'circle-radius': ['interpolate', ['linear'], ['zoom'], 3, 2.5, 12, 6],
          'circle-color': ['case', ['get', 'is_360'], '#ffd34d', L.color],
          'circle-stroke-color': '#222', 'circle-stroke-width': 0.8 } }, before);
      ids.push(src + '-clusters', src + '-count', src);
      wireClusterZoom(src + '-clusters', src);
      wirePhoto(src);
    } else if (L.kind === 'photos') {
      const slyr = L.format === 'pmtiles' ? { 'source-layer': L.sourceLayer } : {};
      // dots everywhere (complete coverage), small once thumbnails take over
      // 360s / panos sit on top of the other photos (dots, thumbnails and tap
      // targets alike), so they're easy to find and to hit
      map.addLayer({ id: src, type: 'circle', source: src, ...slyr,
        filter: photosHiddenFilter(),
        layout: { 'circle-sort-key': PANO_ON_TOP },
        paint: {
          'circle-radius': photoRadiusExpr(L),   // configurable far/near sizes (drawer)
          'circle-color': ['case', ['get', 'is_360'], '#ffd34d', L.color],
          'circle-stroke-color': effHaloColor(L), 'circle-stroke-width': effHaloWidth(L),
        } }, before);
      // Strava-style circular thumbnails at high zoom. icon-image is a per-photo
      // id (carrying the 360 flag) that we lazy-load in registerThumbLoader().
      const thumbId = src + '-thumb';
      map.addLayer({ id: thumbId, type: 'symbol', source: src, ...slyr,
        minzoom: 12,
        filter: photosHiddenFilter(),
        layout: {
          'icon-image': ['concat', 'pthumb-',
            ['case', ['get', 'is_360'], 's-', 'n-'], ['get', 'id']],
          'icon-size': ['interpolate', ['linear'], ['zoom'], 12, 0.55, 18, 1.05],
          'icon-allow-overlap': false, 'icon-padding': 2, 'icon-optional': true,
          // thumbnails that would overlap hide one another, the first placed winning:
          // a 360's goes first (lower key), so it shows over a plain photo's
          'symbol-sort-key': ['-', 1, PANO_ON_TOP],
        } }, before);
      // An invisible, finger-sized target over every photo. The dots are only a
      // few pixels wide, and in a pitched 3D view everything compresses toward the
      // horizon, so hitting one was fiddly. Opacity 0 still takes clicks.
      const hitId = src + '-hit';
      map.addLayer({ id: hitId, type: 'circle', source: src, ...slyr,
        filter: photosHiddenFilter(),
        layout: { 'circle-sort-key': PANO_ON_TOP },
        paint: {
          'circle-radius': ['interpolate', ['linear'], ['zoom'], 3, 6, 12, 14, 16, 20],
          'circle-color': '#000', 'circle-opacity': 0,
        } }, before);
      ids.push(src, thumbId, hitId);
      registerThumbLoader();
      wirePhoto(src);
      wirePhoto(thumbId);
      wirePhoto(hitId);
      // a trip opened before the photos layer existed: apply it now
      if (tripRange) { applyPhotoFilter(); showTripPhotos(tripPhotosUrl); }
    } else if (L.kind === 'pois') {
      registerPoiIcons();
      map.addLayer({ id: src, type: 'symbol', source: src,
        layout: {
          'icon-image': poiIconExpression(),
          'icon-size': ['interpolate', ['linear'], ['zoom'], 3, 0.6, 12, 1.0],
          'icon-allow-overlap': true,
          'text-field': ['get', 'name'], 'text-size': 11,
          // a style can name the fontstack its glyph server serves (`font`; e.g. OSM US
          // serves Noto Sans, not the default Open Sans)
          ...(activeStyle && activeStyle.font ? { 'text-font': activeStyle.font } : {}),
          // label sits just under the round marker, which grows with zoom
          'text-offset': ['interpolate', ['linear'], ['zoom'],
            3, ['literal', [0, 1.1]], 12, ['literal', [0, 1.6]]],
          'text-anchor': 'top', 'text-optional': true,
        },
        paint: { 'text-color': '#11457a', 'text-halo-color': '#fff', 'text-halo-width': 1.4 } }, before);
      ids.push(src);
    } else { // points
      map.addLayer({ id: src, type: 'circle', source: src,
        ...(L.format === 'pmtiles' ? { 'source-layer': L.sourceLayer } : {}),
        paint: { 'circle-radius': L.radius != null ? L.radius : 2.2,
          'circle-color': L.color, 'circle-opacity': L.opacity != null ? L.opacity : 0.55,
          'circle-stroke-color': effHaloColor(L), 'circle-stroke-width': effHaloWidth(L) } }, before);
      ids.push(src);
    }
    layerIds[src] = ids;
    if (!L.defaultOn) ids.forEach((id) => map.setLayoutProperty(id, 'visibility', 'none'));
    applyZoomTo(L, ids);   // apply configured min/max zoom (thumbnails stay >=12)
  }

  // click a cluster -> zoom to where it breaks apart
  function wireClusterZoom(clusterLayer, srcId) {
    map.on('mouseenter', clusterLayer, () => map.getCanvas().style.cursor = 'pointer');
    map.on('mouseleave', clusterLayer, () => map.getCanvas().style.cursor = '');
    map.on('click', clusterLayer, async (e) => {
      const f = e.features[0];
      try {
        const zoom = await map.getSource(srcId).getClusterExpansionZoom(f.properties.cluster_id);
        map.easeTo({ center: f.geometry.coordinates, zoom });
      } catch (err) { console.warn('cluster zoom', err); }
    });
  }

  // ----- line (track) interactions -----------------------------------------
  // Click a track line -> popup with whatever descriptive props it carries
  // (Garmin tracks have name/type/distance/duration/start; older lines may not).
  function wireLine(id) {
    map.on('mouseenter', id, () => map.getCanvas().style.cursor = 'pointer');
    map.on('mouseleave', id, () => map.getCanvas().style.cursor = '');
    map.on('click', id, (e) => {
      // hiked.js owns the 'hiked' layer (delete popup) and the 'trails' clicks
      // while its selection mode is active.
      if (id === 'hiked') return;
      if (id === 'trails' && window.__hikedSelecting) return;
      const p = e.features[0].properties || {};
      if (id === 'trails') { openTrailPopup(p, e.lngLat); return; }
      const title = p.name || p.type || 'Track';
      const when = (p.start || p.time || '').replace('T', ' ').slice(0, 16);
      const bits = [];
      if (p.type && p.type !== title) bits.push(p.type);
      if (p.distance_km != null && p.distance_km !== '') bits.push(`${p.distance_km} km`);
      if (p.duration_min != null && p.duration_min !== '') bits.push(`${p.duration_min} min`);
      const meta = [when, bits.join(' · ')].filter(Boolean).join('<br>');
      if (!p.name && !p.type && !p.distance_km && !meta) return; // nothing useful
      new maplibregl.Popup({ maxWidth: '260px' })
        .setLngLat(e.lngLat)
        .setHTML(`<b>${title}</b>${meta ? '<br><small>' + meta + '</small>' : ''}`)
        .addTo(map);
    });
  }

  // OSM US trails overlay: name (if tagged) + a link straight into the OSM iD
  // editor for that way (OSM_ID, same property hiked.js keys segments by), so a
  // bad/missing trail tag is one click away from being fixed at the source.
  function openTrailPopup(p, lngLat) {
    const wayId = p.OSM_ID;
    if (!wayId && !p.name) return; // nothing useful to show
    const name = (p.name || 'Trail segment').toString().replace(/</g, '&lt;');
    const editLink = wayId
      ? `<br><a href="https://www.openstreetmap.org/edit?editor=id&way=${encodeURIComponent(wayId)}" ` +
        `target="_blank" rel="noopener">Edit in OSM ↗</a>`
      : '';
    new maplibregl.Popup({ maxWidth: '240px' })
      .setLngLat(lngLat)
      .setHTML(`<b>${name}</b>${editLink}`)
      .addTo(map);
  }

  // isochrone fill: click -> "reachable within N min drive"
  function wireIsochrone(id) {
    map.on('mouseenter', id, () => map.getCanvas().style.cursor = 'pointer');
    map.on('mouseleave', id, () => map.getCanvas().style.cursor = '');
    map.on('click', id, (e) => {
      const min = e.features[0] && e.features[0].properties && e.features[0].properties.minutes;
      if (min == null) return;
      new maplibregl.Popup({ maxWidth: '220px' })
        .setLngLat(e.lngLat)
        .setHTML(`<b>Within ${min} min drive</b>`)
        .addTo(map);
    });
  }

  // ----- photo interactions ------------------------------------------------
  function wirePhoto(id) {
    map.on('mouseenter', id, () => map.getCanvas().style.cursor = 'pointer');
    map.on('mouseleave', id, () => map.getCanvas().style.cursor = '');
    if (id === 'spider') {
      map.on('click', id, (e) => openPhoto(e.features[0].properties, e.lngLat));
    } else {
      map.on('click', id, handlePhotoClick);   // open one, or spiderfy a cluster
    }
  }

  // If several photos overlap near the tap, fan them out ("spiderfy"); else open.
  let _photoClickTs = -1;
  function handlePhotoClick(e) {
    const ts = e.originalEvent ? e.originalEvent.timeStamp : performance.now();
    if (ts === _photoClickTs) return;          // ignore the 2nd layer firing the same click
    _photoClickTs = ts;
    // Pitched views squeeze photos together, so widen the box with the pitch: at
    // 60° a tap covers a lot more ground than the same tap looking straight down.
    const r = 26 + Math.round(map.getPitch() * 0.4);
    let feats = [];
    try {
      feats = map.queryRenderedFeatures(
        [[e.point.x - r, e.point.y - r], [e.point.x + r, e.point.y + r]], { layers: ['photos'] });
    } catch {}
    const uniq = dedupeById(feats);
    if (uniq.length > 1) spiderfy(e.point, uniq);
    else openPhoto((uniq[0] || e.features[0]).properties, e.lngLat);
  }
  function dedupeById(feats) {
    const seen = new Set(), out = [];
    for (const f of feats) {
      const id = f.properties && f.properties.id;
      if (id && !seen.has(id)) { seen.add(id); out.push(f); }
    }
    return out;
  }

  // ----- spiderfy overlapping photos ---------------------------------------
  const emptyFC = () => ({ type: 'FeatureCollection', features: [] });
  let spiderActive = false;
  // The fanned photos are HTML markers, not a symbol layer. They were a geojson
  // source + `icon-image` before, which depended on the lazy thumbnail registry and
  // stopped drawing under MapLibre 6 (lines appeared, photos didn't). Markers are
  // DOM elements: they always render, stay crisp, and need no image ids. The
  // connector lines are still a line layer, which works fine.
  const spiderMarkers = [];
  function ensureSpiderLayers() {
    if (map.getSource('spider-lines')) return;
    map.addSource('spider-lines', { type: 'geojson', data: emptyFC() });
    map.addLayer({ id: 'spider-lines', type: 'line', source: 'spider-lines',
      paint: { 'line-color': '#999', 'line-width': 1.2, 'line-opacity': 0.8 } });
    map.on('movestart', collapseSpider);       // pan/zoom dismisses the fan
  }
  function spiderfy(centerPx, feats) {
    ensureSpiderLayers();
    const n = feats.length;
    const c = map.unproject([centerPx.x, centerPx.y]);
    const lines = [];
    feats.forEach((f, i) => {
      let r, ang;
      if (n <= 9) { r = 58; ang = (2 * Math.PI * i) / n - Math.PI / 2; }
      else { ang = i * 0.55; r = 42 + 5.2 * i; }   // spiral for big clusters
      const ll = map.unproject([centerPx.x + r * Math.cos(ang), centerPx.y + r * Math.sin(ang)]);
      const coord = [ll.lng, ll.lat];
      const p = f.properties;
      const el = document.createElement('div');
      el.className = 'spider-thumb' + (p.is_360 ? ' is-360' : '');
      el.style.backgroundImage = `url("${CFG.photoUrl(p.id, 'thumb')}")`;
      el.title = p.filename || '';
      el.addEventListener('click', (ev) => { ev.stopPropagation(); openPhoto(p, ll); });
      spiderMarkers.push(new maplibregl.Marker({ element: el }).setLngLat(coord).addTo(map));
      lines.push({ type: 'Feature', geometry: { type: 'LineString', coordinates: [[c.lng, c.lat], coord] }, properties: {} });
    });
    map.getSource('spider-lines').setData({ type: 'FeatureCollection', features: lines });
    spiderActive = true;
  }
  function collapseSpider() {
    if (!spiderActive) return;
    spiderActive = false;
    while (spiderMarkers.length) spiderMarkers.pop().remove();
    if (map.getSource('spider-lines')) map.getSource('spider-lines').setData(emptyFC());
  }

  // ----- lazy circular thumbnails (Strava-style) ---------------------------
  // The thumbnail symbol layer references image ids like "pthumb-n-<id>" /
  // "pthumb-s-<id>" (s = 360). MapLibre fires styleimagemissing for each one in
  // view; we fetch that photo's thumb, mask it into a circle with a ring, and
  // register it. Only visible photos are ever fetched, and each is cached.
  const thumbPending = new Set();
  // Fetch one photo's circular thumbnail and register it under its image id.
  // Returns the image so MapLibre 6's resolver can use it directly.
  async function loadThumbImage(key) {
    if (!key.startsWith('pthumb-') || map.hasImage(key) || thumbPending.has(key)) return undefined;
    thumbPending.add(key);
    const rest = key.slice('pthumb-'.length);              // "s-<id>" | "n-<id>"
    const is360 = rest[0] === 's';
    const pid = rest.slice(2);
    try {
      const img = await loadCircleThumb(CFG.photoUrl(pid, 'thumb'),
        is360 ? '#ffd34d' : '#ffffff');
      if (!map.hasImage(key)) map.addImage(key, img, { pixelRatio: 2 });
      return img;
    } catch (err) {
      // register a blank so MapLibre stops asking (broken/missing thumb)
      if (!map.hasImage(key)) map.addImage(key, BLANK_THUMB, { pixelRatio: 2 });
      return BLANK_THUMB;
    } finally {
      thumbPending.delete(key);
    }
  }

  function registerThumbLoader() {
    if (registerThumbLoader._done) return;
    registerThumbLoader._done = true;
    // MapLibre 6 replaced the `styleimagemissing` event with a resolver that is
    // handed the image id and may return the image (or a promise of it). Without
    // this the lazy thumbnails never load on v6: photo markers and the fanned-out
    // cluster show only their rings.
    if (typeof map.setMissingStyleImageResolver === 'function') {
      map.setMissingStyleImageResolver((id) => loadThumbImage(id));
    } else {
      map.on('styleimagemissing', (e) => { loadThumbImage(e.id); });
    }
  }

  const BLANK_THUMB = { width: 2, height: 2, data: new Uint8Array(16) };

  function loadCircleThumb(url, ring) {
    return new Promise((resolve, reject) => {
      const im = new Image();
      im.crossOrigin = 'anonymous';
      im.onload = () => {
        const S = 160, r = S / 2;                                // hi-res so big circles stay crisp
        const c = document.createElement('canvas'); c.width = c.height = S;
        const ctx = c.getContext('2d');
        ctx.save();
        ctx.beginPath(); ctx.arc(r, r, r - 5, 0, Math.PI * 2); ctx.clip();
        const scale = Math.max(S / im.width, S / im.height);     // cover-fit
        const w = im.width * scale, h = im.height * scale;
        ctx.drawImage(im, (S - w) / 2, (S - h) / 2, w, h);
        ctx.restore();
        ctx.lineWidth = 6; ctx.strokeStyle = ring || '#fff';
        ctx.beginPath(); ctx.arc(r, r, r - 3, 0, Math.PI * 2); ctx.stroke();
        resolve(ctx.getImageData(0, 0, S, S));
      };
      im.onerror = reject;
      im.src = url;
    });
  }

  function openPhoto(p, lngLat) {
    const is360 = p.is_360 === true || p.is_360 === 'true';
    const web = CFG.photoUrl(p.id, 'web');
    const pano = CFG.photoUrl(p.id, 'pano');
    const when = (p.timestamp || '').replace('T', ' ').slice(0, 16);
    let action = '';
    if (is360) {   // offer the immersive viewer for any pano (sphere or wide)
      action = `<button onclick="window.openSphere('${pano}','${(p.filename||'').replace(/'/g,'')}')"
                  style="margin-top:6px">View 360° ↻</button>`;
    }
    const imgUrl = (is360 && p.pano_type) ? pano : web;
    const badge = is360 ? `<span class="badge">${p.pano_type === 'sphere' ? '360°' : 'PANO'}</span>` : '';
    new maplibregl.Popup({ maxWidth: '320px', className: 'photo-popup' })
      .setLngLat(lngLat)
      .setHTML(
        `<div class="photo-popup"><b>${p.filename || 'Photo'}</b>${badge}<br>` +
        `<small>${when}</small>${action}` +
        `<img src="${imgUrl}" onclick="window.showLightbox('${imgUrl}')" ` +
        `onerror="this.style.display='none'">` +
        `<button class="hide-photo-btn" onclick="window.hidePhoto('${p.id}')">Hide from map</button></div>`)
      .addTo(map);
  }

  window.showLightbox = (url) => {
    document.getElementById('lightbox-img').src = url;
    document.getElementById('lightbox').style.display = 'flex';
  };

  // ----- lazy layer loading ------------------------------------------------
  // Sources are created only when a layer is first shown. Heavy layers
  // (Google history/timeline) are off by default and never fetched until the
  // user toggles them — this keeps initial load light in both local and r2 mode.
  const loaded = new Set();
  const byId = Object.fromEntries(CFG.layers.map((L) => [L.id, L]));
  // Layer groups: a layer with `groupOf: '<leader id>'` gets no panel row of its own;
  // the leader's row toggles, colours and styles every member together, while each
  // member keeps its own source (e.g. nightly Garmin tiles, D1-edited hiked trails).
  const members = (L) => [L, ...CFG.layers.filter((M) => M.groupOf === L.id)];
  const isOn = (L) => (L.groupOf ? byId[L.groupOf].defaultOn : L.defaultOn);

  async function ensureLayer(L) {
    if (loaded.has(L.id)) return;
    loaded.add(L.id);
    if (L.format === 'pmtiles') {
      map.addSource(L.id, { type: 'vector', url: `pmtiles://${await CFG.tileUrl(L)}` });   // versioned on R2
    } else {
      let data;
      try { data = await (await fetch(L.url)).json(); }
      catch (err) { console.warn('skip', L.id, err); loaded.delete(L.id); return; }
      const opts = { type: 'geojson', data };
      if (L.cluster) { opts.cluster = true; opts.clusterMaxZoom = 13; opts.clusterRadius = 45; }
      map.addSource(L.id, opts);
      const el = document.querySelector(`.count[data-for="${L.id}"]`);
      if (el) el.textContent = data.features.length.toLocaleString();
    }
    addLayerRender(L);
  }

  // ----- panel -------------------------------------------------------------
  // Collapsible panel section (<details>) for layers with `section: '<key>'`,
  // created on first use. Rows stay inside #layers, so the panel's delegated
  // change/input/click handlers keep working unchanged.
  function sectionBody(key) {
    let body = panel.querySelector(`.layer-section[data-section="${key}"] .section-body`);
    if (body) return body;
    const S = (CFG.sections || {})[key] || { label: key };
    const d = document.createElement('details');
    d.className = 'layer-section';
    d.dataset.section = key;
    d.open = !S.collapsed;
    d.innerHTML = `<summary>${S.label}</summary><div class="section-body"></div>`;
    panel.appendChild(d);
    return d.querySelector('.section-body');
  }

  function addPanelRow(L) {
    const block = document.createElement('div');
    block.className = 'layer-block';
    // The swatch is a live <input type="color">; the ⚙ opens a controls drawer.
    // Both are interactive content nested in the row <label>, so per the HTML spec
    // clicking them does NOT toggle the layer checkbox.
    const cls = 'swatch-color' + (L.kind === 'lines' ? ' line' : '');
    const colorCtl = L.kind === 'pois' ? '' :
      `<input type="color" class="${cls}" data-color="${L.id}" value="${L.color}" title="Layer color (live)">`;
    block.innerHTML =
      `<label class="layer-row">` +
        `<input type="checkbox" data-id="${L.id}" ${L.defaultOn ? 'checked' : ''}>` +
        colorCtl + `${L.label}` +
        `<span class="count" data-for="${L.id}"></span>` +
        `<button type="button" class="ctl-toggle" data-expand="${L.id}" title="Style controls">⚙</button>` +
      `</label>` +
      `<div class="layer-controls" id="ctl-${L.id}">${buildControls(L, L.kind)}</div>`;
    // isochrones get an always-visible time slider (the layer's primary control)
    if (L.kind === 'isochrones') {
      const cur = isoMinutes(L);
      const s = document.createElement('div');
      s.className = 'iso-slider';
      s.innerHTML =
        `<input type="range" data-iso="${L.id}" min="${L.minMinutes}" max="${L.maxMinutes}" ` +
        `step="${L.stepMinutes}" value="${cur}"><span class="iso-val">${cur} min</span>`;
      block.appendChild(s);
    }
    (L.section ? sectionBody(L.section) : panel).appendChild(block);
  }

  // Recolor a layer live (and remember it so a not-yet-loaded layer adopts the
  // color when it's first added). Symbol layers (POI emoji, photo thumbnails)
  // carry no settable fill colour here, so they're skipped.
  function setLayerColor(L, hex) {
    L.color = hex;
    for (const id of (layerIds[L.id] || [])) {
      if (!map.getLayer(id) || isDecor(id)) continue;
      const t = map.getLayer(id).type;
      if (t === 'circle') {
        map.setPaintProperty(id, 'circle-color',
          L.kind === 'photos' ? ['case', ['get', 'is_360'], '#ffd34d', hex] : hex);
      } else if (t === 'line') {
        map.setPaintProperty(id, 'line-color', hex);
      } else if (t === 'fill') {
        map.setPaintProperty(id, 'fill-color', hex);
      }
    }
  }
  panel.addEventListener('change', (e) => {
    if (e.target.id !== 'show-junk') return;
    showJunk = e.target.checked;
    applyPhotoFilter();
    map.once('idle', () => window.filmstripRefresh && window.filmstripRefresh(true));
    map.triggerRepaint();
  });
  panel.addEventListener('change', async (e) => {
    const L = byId[e.target.dataset.id];
    if (!L) return;
    const vis = e.target.checked ? 'visible' : 'none';
    for (const M of members(L)) {
      if (e.target.checked) await ensureLayer(M);
      for (const id of (layerIds[M.id] || [M.id])) {
        if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', vis);
      }
    }
  });
  // live layer styling: color swatch (data-color) or a control (data-layer+data-prop)
  panel.addEventListener('input', (e) => {
    const t = e.target;
    if (t.dataset.color && byId[t.dataset.color]) {
      members(byId[t.dataset.color]).forEach((M) => setLayerColor(M, t.value));
      return;
    }
    if (t.dataset.iso && byId[t.dataset.iso]) {   // drive-time slider -> filter the shown band
      const L = byId[t.dataset.iso];
      L.minutes = +t.value;
      const filt = ['==', ['get', 'minutes'], L.minutes];
      for (const id of (layerIds[L.id] || [])) { if (map.getLayer(id)) map.setFilter(id, filt); }
      const lab = t.parentElement && t.parentElement.querySelector('.iso-val');
      if (lab) lab.textContent = L.minutes + ' min';
      return;
    }
    const lid = t.dataset.layer, prop = t.dataset.prop;
    if (lid && prop && byId[lid]) {
      members(byId[lid]).forEach((M) => applyStyleTo(M, layerIds[M.id] || [], prop, t.value));
      const val = t.parentElement && t.parentElement.querySelector('.ctl-val');
      if (val) val.textContent = t.value;
    }
  });
  // expand/collapse a layer's controls drawer
  panel.addEventListener('click', (e) => {
    const id = e.target.dataset.expand;
    if (!id) return;
    const d = document.getElementById('ctl-' + id);
    if (d) d.classList.toggle('open');
  });

  // dump every layer + overlay's full style as a pasteable config fragment
  // (clipboard + console) to bake in as defaults in config.js.
  document.getElementById('copy-config')?.addEventListener('click', (e) => {
    const lines = ['// layers'];
    CFG.layers.forEach((L) => lines.push(configFragment(L, false)));
    if ((CFG.overlays || []).length) {
      lines.push('// overlays');
      CFG.overlays.forEach((O) => lines.push(configFragment(O, true)));
    }
    const text = lines.join('\n');
    console.log('Adventure Map layer config:\n' + text);
    const btn = e.currentTarget, label = btn.textContent;
    const flash = (msg) => { btn.textContent = msg; setTimeout(() => (btn.textContent = label), 1600); };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(() => flash('Copied!'), () => flash('See console'));
    } else { flash('See console'); }
  });

  document.getElementById('panel-toggle').onclick =
    () => document.getElementById('panel').classList.toggle('open');

  // ----- load --------------------------------------------------------------
  map.on('load', async () => {
    // 3D globe projection by default (MapLibre v5; must be set after the style loads;
    // flattens toward a plane as you zoom in), or flat Web Mercator if you chose it
    // with the toggle last time.
    map.setProjection({ type: savedProjection() });
    setupBasemap();
    setupOverlays();
    try { hiddenPhotoIds = (await (await fetch('/api/hidden')).json()).ids || []; } catch {}
    // top-level rows first, then the sectioned ones (sections sit at the bottom)
    CFG.layers.filter((L) => !L.groupOf && !L.section).forEach(addPanelRow);
    CFG.layers.filter((L) => !L.groupOf && L.section).forEach(addPanelRow);
    for (const L of CFG.layers) {
      if (isOn(L)) {
        await ensureLayer(L);
        for (const id of (layerIds[L.id] || [])) map.setLayoutProperty(id, 'visibility', 'visible');
      }
    }
    // POI add/edit/delete UI (no-op if pois.js absent or layer missing)
    if (window.initPOIeditor) window.initPOIeditor(map);
    // Mark-trails-as-hiked tool (no-op if hiked.js absent)
    if (window.initHiked) window.initHiked(map);
    // Bottom filmstrip photo-timeline + geotagging (no-op if filmstrip.js absent)
    if (window.initFilmstrip) window.initFilmstrip(map);
    // Video clips: where each was filmed and the ground it saw (no-op if clips.js absent)
    if (window.initClips) window.initClips(map, basemapUnderlayBeforeId());
  });

  // refresh an API-backed layer's source after edits (POIs, hiked trails)
  async function reloadApiLayer(id) {
    const L = byId[id]; if (!L) return;
    if (!loaded.has(id)) { await ensureLayer(L); return; }
    try {
      const data = await (await fetch(L.url)).json();
      const s = map.getSource(id); if (s) s.setData(data);
    } catch (e) { console.warn('reload', id, e); }
  }

  // expose for the editor modules to refresh their sources after edits
  window._am = { map,
    style: activeStyle,                                       // trips.js: style hooks (tripArea)
    reloadPOIs: () => reloadApiLayer('pois'),
    reloadHiked: () => reloadApiLayer('hiked'),
    openPhoto: (props, lngLat) => openPhoto(props, lngLat),   // filmstrip thumbnail click
    isPhotoHidden: (id) => hiddenPhotoIds.includes(id),       // filmstrip drops hidden photos
    // trips.js: show only a trip's photos (range null = all), then rebuild the filmstrip
    setTripRange: async (range, photosUrl) => {
      tripRange = range;
      tripPhotosUrl = range ? photosUrl : null;
      applyPhotoFilter();
      await showTripPhotos(range ? photosUrl : null);
      map.once('idle', () => window.filmstripRefresh && window.filmstripRefresh(true));
      map.triggerRepaint();
    },
  };
})();
