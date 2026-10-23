// Photo geotagging API (behind Cloudflare Access).
//   GET  /api/geotags  -> { id: {lon,lat,status} } for every tagged/skipped photo
//   POST /api/geotags  -> upsert one or many:
//        { id, lon, lat }            place a single photo
//        { ids:[...], lon, lat }     place a group at one spot
//        { id, status:'skipped' }    mark "not taggable"
// Binding: env.DB (D1).

const json = (data, status = 200) =>
  new Response(JSON.stringify(data), {
    status, headers: { 'content-type': 'application/json' },
  });

export async function onRequestGet({ env }) {
  const { results } = await env.DB.prepare(
    'SELECT id,lon,lat,status FROM photo_geotags'
  ).all();
  const out = {};
  for (const r of results) out[r.id] = { lon: r.lon, lat: r.lat, status: r.status };
  return json(out);
}

export async function onRequestPost({ request, env }) {
  let b;
  try { b = await request.json(); } catch { return json({ error: 'bad json' }, 400); }

  const ids = Array.isArray(b.ids) ? b.ids : (b.id != null ? [String(b.id)] : []);
  if (!ids.length) return json({ error: 'id or ids required' }, 400);

  const status = b.status === 'skipped' ? 'skipped' : 'placed';
  let lon = null, lat = null;
  if (status === 'placed') {
    lon = Number(b.lon); lat = Number(b.lat);
    if (!isFinite(lon) || !isFinite(lat)) return json({ error: 'lon, lat required' }, 400);
  }

  const stmt = env.DB.prepare(
    `INSERT INTO photo_geotags (id,lon,lat,status,updated_at)
     VALUES (?,?,?,?,datetime('now'))
     ON CONFLICT(id) DO UPDATE SET lon=excluded.lon, lat=excluded.lat,
       status=excluded.status, updated_at=datetime('now')`
  );
  await env.DB.batch(ids.map((id) => stmt.bind(String(id), lon, lat, status)));
  return json({ ok: true, count: ids.length, status, lon, lat });
}
