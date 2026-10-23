// Adventure Map — runtime config.
// On localhost the app reads straight from the data-pipeline folder. Deployed, it
// reads from window.SITE.mediaBase (site.js): files published with the site, a
// bucket, or Cloudflare's /media Functions. window.SITE.editing turns on the
// features that write to the /api backend.

window.MAP_CONFIG = (() => {
  const host = location.hostname;
  const isLocal = host === 'localhost' || host === '127.0.0.1';
  const DATA_MODE = isLocal ? 'local' : 'site';
  const SITE = window.SITE || {};
  // absolute, so pmtiles:// URLs and fetches resolve the same everywhere
  const BASE = new URL(String(SITE.mediaBase ?? 'media').replace(/\/*$/, '/'), location.href).href.replace(/\/$/, '');
  const editing = !isLocal && !!SITE.editing;      // local dev has no /api

  // Where data + photos come from.
  const DATA = isLocal ? '../data-pipeline/out' : `${BASE}/data`;
  const PHOTOS = isLocal ? '../data-pipeline/derivatives' : `${BASE}/photos`;
  const TILES = isLocal ? '../data-pipeline/out' : `${BASE}/tiles`;

  // Photo derivative URL. kind = thumb | web | pano.
  const photoUrl = (id, kind) => `${PHOTOS}/${id}/${kind}.webp`;

  // Catalog of un-geotagged photos (for the in-app geotagging gallery). The app
  // reads the slim rows file (id, timestamp, is_360 — written by upload.py);
  // the full GeoJSON is only a fallback if the slim file isn't there.
  const untaggedUrl = `${DATA}/untagged.min.json`;
  const tripsUrl = `${DATA}/trips.json`;            // build_trips.py -> Trips panel
  const geotagAiUrl = `${DATA}/geotag_ai_suggestions.json`;   // geotag_ai.py: places seen in no-GPS photos
  const tripPhotosUrl = (id) => `${DATA}/trips/${id}.json`;   // one trip's photo points
  const tripDriveUrl = (id) => `${DATA}/trips/${id}.drive.json`;   // roads driven (multi-day trips)
  const untaggedFullUrl = `${DATA}/untagged.geojson`;

  // { photo_id: {lon,lat,delta_min,source,method} } -- nearest-in-time GPS match
  // suggestions for untagged photos, built offline by suggest_geotags.py from the
  // Garmin/Google track data. The filmstrip shows these as a one-tap "accept" GPS
  // match; missing file (not yet generated) just means no suggestions show up.
  const geotagSuggestionsUrl = `${DATA}/geotag_suggestions.geojson`;

  // kind: points | lines | photos | pois.  format: pmtiles | api.
  const tile = (name) => ({ format: 'pmtiles', tile: name, url: `${TILES}/${name}.pmtiles`, sourceLayer: name });
  // Tile archives are versioned (tiles/<name>.<hash>.pmtiles, upload.py) and
  // data/tiles.json names the current ones. Read once per page load, so an open page
  // keeps the version it started with even after the nightly upload replaces it.
  // Local dev (and a missing manifest) uses the plain <name>.pmtiles.
  let tileVersions = null;
  const tileUrl = async (L) => {
    if (isLocal || !L.tile) return L.url;
    tileVersions ||= fetch(`${DATA}/tiles.json`, { cache: 'no-cache' })
      .then((r) => (r.ok ? r.json() : {})).catch(() => ({}));
    const v = (await tileVersions)[L.tile];
    return v ? `${TILES}/${L.tile}.${v}.pmtiles` : L.url;
  };
  // Collapsible panel sections (layers opt in with `section: '<key>'`).
  const sections = { raw: { label: 'Raw data', collapsed: true } };

  const layers = [
    // Drive-time isochrones: one tileset, filtered by a 60->240 min slider. First
    // in the list so it draws at the BOTTOM of the data stack (translucent context
    // fill beneath the points/lines). Off by default.
    { id: 'isochrones', label: 'Drive time', kind: 'isochrones', color: '#3b82f6',
      defaultOn: false, minMinutes: 60, maxMinutes: 240, stepMinutes: 30, minutes: 120,
      ...tile('isochrones') },

    // section: 'raw' -> listed under the collapsed "Raw data" heading at the bottom
    // of the panel, all off by default: the unprocessed source data behind the
    // map-matched layers. Heaviest tilesets too (google_old = 1.4M legacy points),
    // so keeping them off also keeps page loads light.
    { id: 'google_old', section: 'raw', label: 'Google history (old)', kind: 'points', color: '#8aa0b8',
      defaultOn: false, radius: 2.75, ...tile('google_old') },   // +25% over the 2.2 default

    { id: 'google_paths', section: 'raw', label: 'Google timeline (new)', kind: 'points', color: '#c44e9a',
      defaultOn: false, ...tile('google_paths') },

    // Legacy Garmin track points (from the old GeoPackage export).
    { id: 'garmin', section: 'raw', label: 'GPS points (legacy)', kind: 'points', color: '#ff6d00',
      opacity: 1, defaultOn: false, minzoom: 8, ...tile('garmin') },

    // Every road travelled, map-matched and aggregated into one line per network
    // edge: the Google location history (2013-2026) plus the driving from the hikes
    // layer (incl. the Google "Directions from" routes), which used to be a separate
    // Drives layer. Shaded and widened by how many trips used each road.
    // One flat colour: the layer is dense enough that shading it by trip count
    // fought with the basemap. (The n_trips property is still there, so colorExpr /
    // widthExpr can bring the frequency shading back any time.)
    // Colours are chosen to sit with the panel's palette: light grey for the road network (was charcoal, before
    // that dusty rose; set from the panel's Copy config 2026-10-22), bright gold-ochre for your own tracks
    // (the style's own highlight colour; its trails are red, so orange would read
    // as "just another trail"), and plum for photos (a hue the style doesn't use).
    { id: 'roads_travelled', label: 'Roads travelled', kind: 'lines', defaultOn: true,
      color: '#a8a8a8', width: 2.5, dash: 'solid', ...tile('roads_travelled') },

    // "Hikes" is one panel entry over three sources (groupOf: 'hikes'), each kept
    // separate so it can update on its own:
    //   * GPS activities: FIT / GPX / TCX files from any watch or app
    //     (import_activities.py) and, optionally, Garmin Connect (garmin_sync.py)
    //   * the hikes layer, map-matched onto the trail network (snap_regions.py),
    //     foot stretches only (its driving is folded into Roads travelled above)
    //   * trail segments marked as hiked in the app (🥾 tool, stored in D1)
    { id: 'activities', groupOf: 'hikes', label: 'Activities', kind: 'lines', color: '#bb00ff',
      defaultOn: true, ...tile('activities') },

    { id: 'hikes', label: 'Hikes', kind: 'lines', color: '#bb00ff',
      defaultOn: true, minzoom: 8, ...tile('hikes_snapped'),
      filter: ['==', ['get', 'mode'], 'hike'] },

    // The pre-snapping GPS lines, kept as-is. Off by default; turn on to compare.
    { id: 'hikes_raw', section: 'raw', label: 'Original tracks (unsnapped)', kind: 'lines', color: '#b0592a',
      defaultOn: false, width: 1.5, dash: 'dot', ...tile('hikes') },

    // Trail segments marked as hiked in the app (selected from the OSM trails
    // overlay). Stored by the /api backend, so only with editing on.
    ...(editing ? [{ id: 'hiked', groupOf: 'hikes', label: 'Hiked trails', kind: 'lines', color: '#bb00ff',
      defaultOn: true, minzoom: 8, format: 'api', url: '/api/hiked' }] : []),

    // Photos as vector tiles (individual points, no clustering) for performance.
    { id: 'photos', label: 'Photos', kind: 'photos', color: '#9b4683',
      defaultOn: true, dotFar: 4, dotNear: 4, haloColor: '#fbf8f1', haloWidth: 0.8, ...tile('photos') },

    // Editable POIs from the D1-backed API. Locally (no API) it reads the nightly
    // off-cloud backup written by export_pois.py, so POIs can be checked in dev.
    { id: 'pois', label: 'My POIs', kind: 'pois', color: '#2e86de',
      defaultOn: true, minzoom: 8, format: 'api', url: editing ? '/api/pois' : `${DATA}/pois.geojson` },
  ];

  // Selectable basemap styles (the "Style" switcher in the panel). All vector,
  // so the hillshade blend (insert below the basemap's roads/labels) works on
  // each. MapTiler needs the key; the others are key-free. Switching persists
  // the choice and reloads — robust against the editor modules' own map layers.
  const styles = [
    // OpenFreeMap Liberty: key-free vector basemap. Add your own styles here - a
    // style URL (`url`), an inline style object (`style`) or a `build()` function
    // that returns one; see "Adding a basemap style" in the README for the hooks.
    { id: 'liberty', label: 'Liberty',
      url: 'https://tiles.openfreemap.org/styles/liberty' },
  ];
  // If a style ever fails to load, app.js falls back to a key-free one.
  const defaultStyleId = 'liberty';

  // Raster base layers, drawn BELOW the vector basemap + all data.
  // hillshade: terrain shading (OpenStreetMap US). imagery: satellite (Esri).
  const basemap = {
    hillshade: {
      url: 'https://tiles.openstreetmap.us/raster/hillshade/{z}/{x}/{y}.jpg',
      // off by default on every style now (it was only auto-off on USGS Topo,
      // which bakes in its own relief); tick Hillshade in the panel to add it
      minzoom: 1, maxzoom: 12, defaultOn: false, defaultOpacity: 0.6,
      attribution: 'Hillshade &copy; <a href="https://tiles.openstreetmap.us/" target="_blank" rel="noopener">OpenStreetMap US</a>',
    },
    imagery: {
      url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
      minzoom: 0, maxzoom: 19, defaultOn: false,
      attribution: 'Imagery &copy; <a href="https://www.esri.com/" target="_blank" rel="noopener">Esri</a>, Maxar, Earthstar Geographics',
    },
  };

  // Vector overlays drawn ABOVE the basemap but below the data layers.
  // Toggleable, each with an opacity slider.
  const overlays = [
    {
      id: 'trails', label: 'Trails (OSM US)',
      url: 'https://tiles.openstreetmap.us/vector/trails/{z}/{x}/{y}.mvt',
      sourceLayer: 'trail', tileMinzoom: 0, tileMaxzoom: 14,   // tile availability (source)
      color: '#000000', width: 1, dash: 'dashed', minzoom: 10, defaultOn: false, defaultOpacity: 0.85,
      attribution: 'Trails &copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors',
    },
  ];

  return { DATA_MODE, isLocal, editing, photoUrl, tileUrl, tripsUrl, tripPhotosUrl, tripDriveUrl, geotagAiUrl, untaggedUrl, untaggedFullUrl, geotagSuggestionsUrl, layers, sections, basemap, overlays, styles, defaultStyleId };
})();
