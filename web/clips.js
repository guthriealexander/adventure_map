/* Video clips on the map: where each was filmed (a point) and the ground it saw,
   painted from the clip itself (an image draped on the terrain, under the roads).
   Tap either to fly into the clip: the camera flies to where the phone was, facing
   where it faced, and the video takes over; a tracked clip turns the map with it.
   Reads video-lab/clips.json, written by video-lab/publish.py; without it, nothing. */
(function () {
  const BASE = 'video-lab/';
  // the terrain the clips were matched against, at true scale (the styles exaggerate theirs)
  const DEM = 'https://tiles.mapterhorn.com/{z}/{x}/{y}.webp';
  const ROLL_SIGN = -1;
  const HIT_M = 800;                                // tap area: this far out over the clip's view
  const CSS = `
  #clip-vid { position: fixed; left: 50%; top: 50%; transform: translate(-50%, -50%); z-index: 3000;
    opacity: 0; transition: opacity 0.4s ease; pointer-events: none; }
  #clip-vid.on { opacity: 1; }
  .clip-dim { position: fixed; top: 0; bottom: 0; z-index: 2999; pointer-events: none; opacity: 0; transition: opacity 1s ease; }
  .clip-dim.on { opacity: 1; }
  #clip-dimL { left: 0; background: linear-gradient(to left, transparent 0, rgba(250,248,242,0.45) 240px); }
  #clip-dimR { right: 0; background: linear-gradient(to right, transparent 0, rgba(250,248,242,0.45) 240px); }
  #clip-back { position: fixed; bottom: 22px; left: 50%; transform: translateX(-50%); z-index: 3001; display: none;
    padding: 9px 16px; border: 0; border-radius: 20px; background: rgba(22,22,20,0.86); color: #f1ede4;
    font: 13px system-ui, sans-serif; cursor: pointer; }
  /* in a clip, only the map and the clip's own controls */
  body.in-clip > *:not(#map):not(.clip-ui) { visibility: hidden !important; }
  body.in-clip .maplibregl-control-container, body.in-clip .maplibregl-ctrl,
  body.in-clip .maplibregl-ctrl-attrib { visibility: hidden !important; }`;

  let map, clips = [], saved = null, vid;
  const state = { phase: 'map', active: null, following: false };
  const $ = (id) => document.getElementById(id);
  const rad = Math.PI / 180;
  const smoothstep = (a, b, x) => { const u = Math.min(1, Math.max(0, (x - a) / (b - a))); return u * u * (3 - 2 * u); };

  // Cloudflare Pages answers a missing file with the site's own page (200, HTML)
  async function optionalJSON(url) {
    try {
      const r = await fetch(url, { cache: 'no-cache' });
      return r.ok && !(r.headers.get('content-type') || '').includes('text/html') ? await r.json() : null;
    } catch { return null; }
  }

  async function initClips(m, beforeId) {
    map = m;
    const man = await optionalJSON(`${BASE}clips.json`);
    clips = (man && man.clips) || [];
    if (!clips.length) return;

    for (const c of clips) {
      const b = c.box, id = `clip-paint-${c.id}`;
      map.addSource(id, { type: 'image', url: `${BASE}${c.paint}`,
        coordinates: [[b.w, b.n], [b.e, b.n], [b.e, b.s], [b.w, b.s]] });
      map.addLayer({ id, type: 'raster', source: id, minzoom: 9.5,
        paint: { 'raster-fade-duration': 0, 'raster-resampling': 'linear',
          'raster-opacity': ['interpolate', ['linear'], ['zoom'], 9.5, 0, 11, 1] } }, beforeId);
    }
    // invisible: the near part of the ground each clip saw, so a tap there flies in
    map.addSource('clip-hits', { type: 'geojson', data: { type: 'FeatureCollection', features: clips.map((c) => {
      const [x0, y0] = c.wedge[0], kx = Math.cos(c.lat * rad) * 111320, ky = 110574;
      const ring = c.wedge.map(([x, y]) => {
        const d = Math.hypot((x - x0) * kx, (y - y0) * ky), k = d > HIT_M ? HIT_M / d : 1;
        return [x0 + (x - x0) * k, y0 + (y - y0) * k];
      });
      return { type: 'Feature', properties: { id: c.id }, geometry: { type: 'Polygon', coordinates: [ring] } };
    }) } });
    map.addLayer({ id: 'clip-hits', type: 'fill', source: 'clip-hits', minzoom: 9.5,
      paint: { 'fill-color': '#000', 'fill-opacity': 0.001 } }, beforeId);
    map.addSource('clip-points', { type: 'geojson', data: { type: 'FeatureCollection', features: clips.map((c) => ({
      type: 'Feature', properties: { id: c.id, name: c.name }, geometry: { type: 'Point', coordinates: [c.lon, c.lat] } })) } });
    map.addLayer({ id: 'clip-points', type: 'circle', source: 'clip-points', minzoom: 5,
      paint: { 'circle-radius': ['interpolate', ['linear'], ['zoom'], 5, 3, 12, 6], 'circle-color': '#e8a400',
        'circle-stroke-color': '#222', 'circle-stroke-width': 1.5 } });

    const style = document.createElement('style');
    style.textContent = CSS;
    document.head.appendChild(style);
    vid = Object.assign(document.createElement('video'), { id: 'clip-vid', className: 'clip-ui', muted: true, loop: true, playsInline: true });
    vid.setAttribute('playsinline', '');
    const back = Object.assign(document.createElement('button'), { id: 'clip-back', className: 'clip-ui', textContent: 'Back out to the map' });
    back.onclick = flyOut;
    for (const side of ['L', 'R']) document.body.appendChild(Object.assign(document.createElement('div'), { id: `clip-dim${side}`, className: 'clip-dim clip-ui' }));
    document.body.append(vid, back);
    addEventListener('keydown', (e) => { if (e.key === 'Escape') flyOut(); });

    const clipAt = (pt) => {
      const f = map.queryRenderedFeatures([[pt.x - 10, pt.y - 10], [pt.x + 10, pt.y + 10]], { layers: ['clip-points', 'clip-hits'] })[0];
      return f && clips.find((c) => c.id === f.properties.id);
    };
    map.on('click', (e) => { const c = state.phase === 'map' && clipAt(e.point); if (c) flyInto(c); });
    let pointing = false;
    map.on('mousemove', (e) => {
      const on = state.phase === 'map' && !!clipAt(e.point);
      if (on !== pointing) { pointing = on; map.getCanvas().style.cursor = on ? 'pointer' : ''; }
    });
    if (window._am) window._am.flyToClip = (id) => flyInto(clips.find((c) => c.id === id));
  }

  // ---- in and out of clip mode: true-scale terrain of our own (held: the app's
  // switch-terrain-by-pitch pauses), room to look up, the camera free of the ground
  function enterMode() {
    saved = { fov: map.getVerticalFieldOfView(), maxPitch: map.getMaxPitch(), terrain: map.getTerrain() };
    if (window._am) window._am.terrainLocked = true;
    if (!map.getSource('clip-dem')) map.addSource('clip-dem', { type: 'raster-dem', tiles: [DEM], tileSize: 512, maxzoom: 12, encoding: 'terrarium' });
    map.setTerrain({ source: 'clip-dem', exaggeration: 1 });
    map.setMaxPitch(110);
    map.setCenterClampedToGround(false);
    map.setLayoutProperty('clip-points', 'visibility', 'none');
    document.body.classList.add('in-clip');
  }
  function leaveMode() {
    map.setCenterClampedToGround(true);
    map.setTerrain(saved.terrain || null);
    map.setMaxPitch(saved.maxPitch);
    if (window._am) window._am.terrainLocked = false;
    map.setLayoutProperty('clip-points', 'visibility', 'visible');
    document.body.classList.remove('in-clip');
  }

  function videoBox(c) {           // the video, contained in the window; and the map FOV that matches it
    const A = c.aspect, vw = innerWidth, vh = innerHeight;
    const [w, h] = vw / vh > A ? [vh * A, vh] : [vw, vw / A];
    const half = Math.atan(Math.tan((c.pose.vfov * Math.PI) / 360) * (vh / h));
    return { w, h, fov: (2 * half * 180) / Math.PI };
  }
  function dims(c, on) {
    const side = on ? Math.max(0, (innerWidth - videoBox(c).w) / 2) : 0;
    for (const id of ['clip-dimL', 'clip-dimR']) { $(id).style.width = `${side}px`; $(id).classList.toggle('on', on && side > 0); }
  }

  // ---- camera ---------------------------------------------------------------------------
  // Standing at a pose: aimed at a point 300 m ahead (MapLibre picks tile detail from the
  // view centre; placed by position + angles, the centre lands ~20 km out and the ground
  // underfoot comes in coarse).
  function poseCamera(p, lift = 0, ahead = 300) {
    const h = p.heading * rad, alt = p.ground_m + p.eye_m + lift;
    const kx = 111320 * Math.cos(p.lat * rad), ky = 110574;
    const to = [p.lon + (Math.sin(h) * ahead) / kx, p.lat + (Math.cos(h) * ahead) / ky];
    const o = map.calculateCameraOptionsFromTo([p.lon, p.lat], alt, to, alt + ahead * Math.tan(p.pitch * rad));
    return { ...o, roll: ROLL_SIGN * p.roll };
  }
  // The drawn terrain is coarser than the elevation data: keep the eye above the highest
  // drawn ground within ~12 m.
  function liftFor(c) {
    const p = c.pose, kx = 111320 * Math.cos(p.lat * rad);
    let q = map.queryTerrainElevation([p.lon, p.lat]);
    if (q == null) return 0;
    for (let a = 0; a < 360; a += 45) {
      const e = map.queryTerrainElevation([p.lon + (Math.sin(a * rad) * 12) / kx, p.lat + (Math.cos(a * rad) * 12) / 110574]);
      if (e != null) q = Math.max(q, e);
    }
    return Math.max(0, q + 1 - p.ground_m);
  }
  // Where the camera is now (lng, lat, altitude m), from the view: back from the centre
  // along the bearing and up, by the pitch, at the distance the field of view implies.
  function cameraNow() {
    const c = map.getCenter(), pitch = map.getPitch() * rad, bearing = map.getBearing() * rad;
    const mpp = (40075016.7 * Math.cos(c.lat * rad)) / (512 * 2 ** map.getZoom());
    const dist = (map.getCanvas().clientHeight / 2 / Math.tan((map.getVerticalFieldOfView() * rad) / 2)) * mpp;
    const back = dist * Math.sin(pitch), kx = 111320 * Math.cos(c.lat * rad);
    return { lng: c.lng - (Math.sin(bearing) * back) / kx, lat: c.lat - (Math.cos(bearing) * back) / 110574,
      alt: (map.getCenterElevation ? map.getCenterElevation() : 0) + dist * Math.cos(pitch) };
  }

  // The flight between `from` and the clip's pose, one continuous move: the distance left
  // shrinks geometrically while the direction the camera sits in swings round to behind
  // and a little above the pose, so it ends gliding forward along the way the phone faced.
  // All along it aims at the spot the clip looks at and rises over terrain under it.
  // Run backwards to fly out.
  function flyCamera(c, from, inward, done) {
    const p = c.pose, kx = 111320 * Math.cos(p.lat * rad), ky = 110574;
    const fx = Math.sin(p.heading * rad), fy = Math.cos(p.heading * rad), altP = p.ground_m + p.eye_m;
    const V0 = [(from.lng - p.lon) * kx, (from.lat - p.lat) * ky, from.alt - altP];
    const d0 = Math.max(1, Math.hypot(...V0));
    const az0 = Math.atan2(V0[0], V0[1]), el0 = Math.asin(V0[2] / d0);
    const azE = Math.atan2(-fx, -fy), elE = 12 * rad;
    const dAz = Math.atan2(Math.sin(azE - az0), Math.cos(azE - az0));
    const k = Math.max(1, Math.log(d0 / 40));
    const geo = (u) => (Math.exp(-k * u) - Math.exp(-k)) / (1 - Math.exp(-k));
    const ms = Math.min(4500, Math.max(2200, 1500 + 400 * Math.log(d0 / 100 + 1)));
    const at = (u) => {
      const w = smoothstep(0, 0.8, u), d = d0 * geo(u);
      const az = az0 + dAz * w, el = el0 + (elE - el0) * w;
      return [Math.sin(az) * Math.cos(el) * d, Math.cos(az) * Math.cos(el) * d, altP + Math.sin(el) * d, d];
    };
    const ahead = 300, to = [p.lon + (fx * ahead) / kx, p.lat + (fy * ahead) / ky], toAlt = altP + ahead * Math.tan(p.pitch * rad);
    const ease = (t) => (t < 0.5 ? 2 * t * t : 1 - (-2 * t + 2) ** 2 / 2);
    const t0 = performance.now();
    let boost = 0;
    const step = () => {
      if (state.active !== c) return;
      const t = Math.min(1, (performance.now() - t0) / ms);
      const [x, y, z, d] = at(inward ? ease(t) : 1 - ease(t));
      const lng = p.lon + x / kx, lat = p.lat + y / ky;
      const ground = map.queryTerrainElevation([lng, lat]) ?? 0;
      boost = Math.max(Math.max(0, ground + 2 + Math.min(150, d * 0.35) - z), boost * 0.96);
      const o = map.calculateCameraOptionsFromTo([lng, lat], z + boost, to, toAlt);
      map.jumpTo({ ...o, roll: ROLL_SIGN * p.roll * smoothstep(0.6, 1, 1 - smoothstep(0, 250, d)) });
      if (t < 1) requestAnimationFrame(step);
      else {
        if (inward) map.jumpTo(poseCamera(p));
        done();
      }
    };
    requestAnimationFrame(step);
  }
  // Ease the FOV to `fov` over `ms` holding the pose `at(e)` (e: 0..1 with the ease).
  function standStill(c, fov, ms, each, done, at = () => c.pose) {
    const f0 = map.getVerticalFieldOfView(), t0 = performance.now();
    const step = () => {
      if (state.active !== c) return;
      const t = Math.min(1, (performance.now() - t0) / ms), e = t * t * (3 - 2 * t);
      if (each) each(e);
      map.setVerticalFieldOfView(f0 + (fov - f0) * e);
      map.jumpTo(poseCamera(at(e), c.lift));
      if (t < 1) requestAnimationFrame(step); else done();
    };
    requestAnimationFrame(step);
  }

  // ---- a tracked clip: heading / tilt / roll every 0.1 s (video-lab/track_skyline.py)
  function livePose(c, t) {
    const tr = c.samples;
    if (!tr || !tr.length) return c.pose;
    let i = 0;
    while (i < tr.length - 2 && tr[i + 1].t < t) i++;
    const a = tr[i], b = tr[i + 1] || a, u = Math.min(1, Math.max(0, (t - a.t) / Math.max(1e-6, b.t - a.t)));
    return { ...c.pose, ...blendPose(a, b, u) };
  }
  function blendPose(a, b, u) {
    const dh = ((b.heading - a.heading + 540) % 360) - 180;
    return { heading: (a.heading + dh * u + 360) % 360, pitch: a.pitch + (b.pitch - a.pitch) * u, roll: a.roll + (b.roll - a.roll) * u };
  }
  function follow(c) {
    if (!c.samples) return;
    state.following = true;
    const step = () => {
      if (state.active !== c || state.phase !== 'there') return;
      map.jumpTo(poseCamera(livePose(c, vid.currentTime), c.lift));
      requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  // ---- flying in and out ----------------------------------------------------------------
  async function flyInto(c) {
    if (!c || state.phase !== 'map') return;
    state.phase = 'flying'; state.active = c; c.lift = 0;
    if (c.samples === undefined) c.samples = ((await optionalJSON(`${BASE}${c.track}`)) || {}).samples || null;
    enterMode();
    const box = videoBox(c);
    // the video loads during the flight, so it plays the moment the camera lands
    vid.src = `${BASE}${c.video}`;
    vid.style.width = `${box.w}px`; vid.style.height = `${box.h}px`;
    vid.addEventListener('loadedmetadata', () => { vid.currentTime = 0.5; }, { once: true });
    vid.load();
    flyCamera(c, cameraNow(), true, () => {
      if (state.active !== c) return;
      state.phase = 'there';
      dims(c, true);
      $('clip-back').style.display = 'block';
      vid.play().catch(() => {});
      vid.classList.add('on');
      const lift = liftFor(c);
      standStill(c, box.fov, 600, (e) => { c.lift = lift * e; }, () => follow(c), () => livePose(c, vid.currentTime));
    });
  }

  function exitPoint(c, back = 900, up = 350) {
    const p = c.pose, h = p.heading * rad;
    return { lng: p.lon - (Math.sin(h) * back) / (111320 * Math.cos(p.lat * rad)),
      lat: p.lat - (Math.cos(h) * back) / 110574, alt: p.ground_m + p.eye_m + up };
  }
  function flyOut() {
    if (state.phase !== 'there') return;
    const c = state.active;
    state.phase = 'flying';
    vid.classList.remove('on');
    setTimeout(() => vid.pause(), 450);
    dims(c, false);
    $('clip-back').style.display = 'none';
    // turn back to the opening view (if it was following the video) as the view narrows,
    // then one move back to a viewpoint over the spot
    const was = state.following ? livePose(c, vid.currentTime) : c.pose;
    state.following = false;
    standStill(c, saved.fov, was === c.pose ? 400 : 700, null, () => {
      c.lift = 0;
      flyCamera(c, exitPoint(c), false, () => {
        state.phase = 'map'; state.active = null;
        leaveMode();
      });
    }, (e) => ({ ...c.pose, ...blendPose(was, c.pose, e) }));
  }

  window.initClips = initClips;
})();
