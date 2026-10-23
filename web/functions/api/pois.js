// POI collection API (behind Cloudflare Access).
//   GET  /api/pois   -> GeoJSON FeatureCollection of all POIs
//   POST /api/pois   -> create one  {name,type,lon,lat,note}
// Binding: env.DB (D1).

const json = (data, status = 200) =>
  new Response(JSON.stringify(data), {
    status, headers: { 'content-type': 'application/json' },
  });

export async function onRequestGet({ env }) {
  const { results } = await env.DB.prepare(
    'SELECT id,name,type,lon,lat,note,updated_at FROM pois ORDER BY id'
  ).all();
  return json({
    type: 'FeatureCollection',
    features: results.map((r) => ({
      type: 'Feature',
      geometry: { type: 'Point', coordinates: [r.lon, r.lat] },
      properties: {
        id: r.id, name: r.name, type: r.type, note: r.note,
        updated_at: r.updated_at,
      },
    })),
  });
}

export async function onRequestPost({ request, env }) {
  let b;
  try { b = await request.json(); } catch { return json({ error: 'bad json' }, 400); }
  const name = (b.name || '').toString().trim();
  const lon = Number(b.lon), lat = Number(b.lat);
  if (!name || !isFinite(lon) || !isFinite(lat)) {
    return json({ error: 'name, lon, lat required' }, 400);
  }
  const r = await env.DB.prepare(
    'INSERT INTO pois (name,type,lon,lat,note) VALUES (?,?,?,?,?)'
  ).bind(name, b.type || null, lon, lat, b.note || null).run();
  return json({ id: r.meta.last_row_id, name, type: b.type || null, lon, lat, note: b.note || null }, 201);
}
