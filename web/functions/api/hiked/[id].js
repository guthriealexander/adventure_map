// Single hiked-trail API (behind Cloudflare Access).
//   DELETE /api/hiked/:id  -> remove a saved trail segment
// Binding: env.DB (D1).

const json = (data, status = 200) =>
  new Response(JSON.stringify(data), {
    status, headers: { 'content-type': 'application/json' },
  });

export async function onRequestDelete({ params, env }) {
  const id = Number(params.id);
  const r = await env.DB.prepare('DELETE FROM hiked_trails WHERE id=?').bind(id).run();
  if (!r.meta.changes) return json({ error: 'not found' }, 404);
  return json({ ok: true, id });
}
