// Serve R2 objects same-origin at /media/<key>.
// Everything sits behind Cloudflare Access, so nothing here is public.
// Supports HTTP Range (required by PMTiles) and long-cache for immutable
// photo derivatives / tiles.
//
// Binding: env.MEDIA  -> the R2 bucket (see wrangler.toml).

const TYPES = {
  webp: 'image/webp', jpg: 'image/jpeg', jpeg: 'image/jpeg', png: 'image/png',
  json: 'application/json', geojson: 'application/json',
  pmtiles: 'application/octet-stream',
};

export async function onRequestGet({ params, env, request }) {
  const key = Array.isArray(params.path) ? params.path.join('/') : params.path;
  if (!key) return new Response('Not found', { status: 404 });

  const range = request.headers.get('range');
  let opts = {};
  if (range) {
    const m = /bytes=(\d+)-(\d*)/.exec(range);
    if (m) {
      const offset = parseInt(m[1], 10);
      const end = m[2] ? parseInt(m[2], 10) : undefined;
      opts = { range: end != null ? { offset, length: end - offset + 1 } : { offset } };
    }
  }

  const obj = await env.MEDIA.get(key, opts);
  if (!obj) return new Response('Not found', { status: 404 });

  const ext = key.split('.').pop().toLowerCase();
  const headers = new Headers();
  obj.writeHttpMetadata(headers);
  headers.set('etag', obj.httpEtag);
  headers.set('content-type', TYPES[ext] || obj.httpMetadata?.contentType || 'application/octet-stream');
  // Photos: derivatives almost never change, so let the browser keep them a week
  // and serve them instantly from its cache (no request, no Worker invocation), then
  // quietly revalidate in the background for up to 30 days after that. Trade-off:
  // ids come from filename+timestamp, not content, so a derivative re-made in place
  // (e.g. an orientation re-fix) can show the old version for up to a week.
  // Tiles/geojson change with nightly rebuilds, so they stay on quick revalidation.
  // Always `private` — everything here is behind Access.
  const isImage = ext === 'webp' || ext === 'jpg' || ext === 'jpeg' || ext === 'png';
  headers.set('cache-control', isImage
    ? 'private, max-age=604800, stale-while-revalidate=2592000'
    : 'private, max-age=60, must-revalidate');
  headers.set('accept-ranges', 'bytes');

  if (range && obj.range) {
    const total = obj.size;
    const start = obj.range.offset || 0;
    const len = obj.range.length ?? (total - start);
    headers.set('content-range', `bytes ${start}-${start + len - 1}/${total}`);
    return new Response(obj.body, { status: 206, headers });
  }
  return new Response(obj.body, { headers });
}
