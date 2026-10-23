// Single POI API (behind Cloudflare Access).
//   PUT/PATCH /api/pois/:id  -> update {name,type,lon,lat,note}
//   DELETE    /api/pois/:id  -> remove
// Binding: env.DB (D1).

const json = (data, status = 200) =>
  new Response(JSON.stringify(data), {
    status, headers: { 'content-type': 'application/json' },
  });

async function update({ params, request, env }) {
  const id = Number(params.id);
  let b;
  try { b = await request.json(); } catch { return json({ error: 'bad json' }, 400); }

  // build a partial update from provided fields only
  const cols = [], vals = [];
  for (const k of ['name', 'type', 'lon', 'lat', 'note']) {
    if (k in b) { cols.push(`${k}=?`); vals.push(b[k]); }
  }
  if (!cols.length) return json({ error: 'nothing to update' }, 400);
  cols.push("updated_at=datetime('now')");
  vals.push(id);
  const r = await env.DB.prepare(`UPDATE pois SET ${cols.join(',')} WHERE id=?`)
    .bind(...vals).run();
  if (!r.meta.changes) return json({ error: 'not found' }, 404);
  return json({ ok: true, id });
}

export const onRequestPut = update;
export const onRequestPatch = update;

export async function onRequestDelete({ params, env }) {
  const id = Number(params.id);
  const r = await env.DB.prepare('DELETE FROM pois WHERE id=?').bind(id).run();
  if (!r.meta.changes) return json({ error: 'not found' }, 404);
  return json({ ok: true, id });
}
