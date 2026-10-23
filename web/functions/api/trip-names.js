// Trip names you've edited in the Trips panel (behind Cloudflare Access). They win
// over the generated names in trips.json, which build_trips/name_trips rewrite nightly.
//   GET  /api/trip-names -> { names: { <trip id>: { name, start, end } } }
//   POST /api/trip-names -> { id, name, start, end }   set / change a name
//        { id, reset: true }                           back to the generated name
// start/end let the panel re-attach a name if a rebuild changes the trip's id.
// Binding: env.DB (D1), table trip_names (worker/schema.sql).

const json = (data, status = 200) =>
  new Response(JSON.stringify(data), {
    status, headers: { 'content-type': 'application/json', 'cache-control': 'no-store' },
  });

export async function onRequestGet({ env }) {
  const { results } = await env.DB.prepare('SELECT id, name, start, "end" FROM trip_names').all();
  return json({ names: Object.fromEntries(results.map((r) => [r.id, { name: r.name, start: r.start, end: r.end }])) });
}

export async function onRequestPost({ request, env }) {
  let b;
  try { b = await request.json(); } catch { return json({ error: 'bad json' }, 400); }
  const id = b.id != null ? String(b.id) : '';
  if (!id) return json({ error: 'id required' }, 400);
  if (b.reset) {
    await env.DB.prepare('DELETE FROM trip_names WHERE id=?').bind(id).run();
    return json({ ok: true, id, reset: true });
  }
  const name = String(b.name || '').trim().slice(0, 120);
  if (!name) return json({ error: 'name required' }, 400);
  await env.DB.prepare(
    `INSERT INTO trip_names (id, name, start, "end", updated_at) VALUES (?, ?, ?, ?, datetime('now'))
     ON CONFLICT(id) DO UPDATE SET name=excluded.name, start=excluded.start, "end"=excluded."end", updated_at=excluded.updated_at`,
  ).bind(id, name, b.start ? String(b.start) : null, b.end ? String(b.end) : null).run();
  return json({ ok: true, id, name });
}
