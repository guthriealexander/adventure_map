// Hiked-trails API (behind Cloudflare Access).
//   GET  /api/hiked  -> GeoJSON FeatureCollection of saved trail segments
//   POST /api/hiked  -> add one or many: { features:[{osm_id,name,geometry}] }
//                       or a single { osm_id, name, geometry }
// Binding: env.DB (D1).

const json = (data, status = 200) =>
  new Response(JSON.stringify(data), {
    status, headers: { 'content-type': 'application/json' },
  });

export async function onRequestGet({ env }) {
  const { results } = await env.DB.prepare(
    'SELECT id,osm_id,name,geojson FROM hiked_trails ORDER BY id'
  ).all();
  const features = [];
  for (const r of results) {
    let geom;
    try { geom = JSON.parse(r.geojson); } catch { continue; }
    features.push({
      type: 'Feature', geometry: geom,
      properties: { id: r.id, osm_id: r.osm_id, name: r.name },
    });
  }
  return json({ type: 'FeatureCollection', features });
}

export async function onRequestPost({ request, env }) {
  let b;
  try { b = await request.json(); } catch { return json({ error: 'bad json' }, 400); }
  const feats = Array.isArray(b.features) ? b.features
    : (b.geometry ? [{ osm_id: b.osm_id, name: b.name, geometry: b.geometry }] : []);
  const valid = feats.filter((f) => f && f.geometry && f.geometry.type && f.geometry.coordinates);
  if (!valid.length) return json({ error: 'geometry required' }, 400);

  const stmt = env.DB.prepare(
    'INSERT INTO hiked_trails (osm_id,name,geojson) VALUES (?,?,?)'
  );
  await env.DB.batch(valid.map((f) =>
    stmt.bind(f.osm_id != null ? String(f.osm_id) : null,
              f.name || null, JSON.stringify(f.geometry))));
  return json({ ok: true, added: valid.length }, 201);
}
