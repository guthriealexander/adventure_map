// Hidden-photos API (behind Cloudflare Access).
//   GET  /api/hidden  -> { ids: [...] } photo ids to hide from the map
//   POST /api/hidden  -> { id }            hide one
//        { id, unhide:true }               un-hide one
// Binding: env.DB (D1).

const json = (data, status = 200) =>
  new Response(JSON.stringify(data), {
    status, headers: { 'content-type': 'application/json' },
  });

export async function onRequestGet({ env }) {
  const { results } = await env.DB.prepare('SELECT id FROM hidden_photos').all();
  return json({ ids: results.map((r) => r.id) });
}

export async function onRequestPost({ request, env }) {
  let b;
  try { b = await request.json(); } catch { return json({ error: 'bad json' }, 400); }
  const id = (b.id != null) ? String(b.id) : '';
  if (!id) return json({ error: 'id required' }, 400);
  if (b.unhide) {
    await env.DB.prepare('DELETE FROM hidden_photos WHERE id=?').bind(id).run();
    return json({ ok: true, id, hidden: false });
  }
  await env.DB.prepare('INSERT OR IGNORE INTO hidden_photos (id) VALUES (?)').bind(id).run();
  return json({ ok: true, id, hidden: true });
}
