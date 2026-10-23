#!/usr/bin/env python3
"""Caption + tag photos with Claude Haiku 5.5 (vision), for search, filtering and
auto-hiding junk on the Adventure Map.

Each photo's web derivative (derivatives/<id>/web.webp, 1600 px) goes to the model
with its date/altitude as context; the reply is constrained to a JSON schema:
caption, scene, tags (fixed vocabulary, so the map can filter on them), people,
quality 1-5, landmark (only when clearly identifiable) and keep (false = junk such
as screenshots, receipts, pocket shots).

Credentials: ANTHROPIC_API_KEY from the environment or a local `.anthropic.env`
(gitignored), same pattern as storage.env.

Usage:
    python tag_photos.py new                   # nightly: tag photos added since last run (direct)
    python tag_photos.py submit                # queue every untagged photo (Batch API, half price)
    python tag_photos.py collect               # fetch finished batches -> out/photo_tags.json
    python tag_photos.py trial                 # 200-photo sample -> out/_tags/trial.json + review.html
    python tag_photos.py trial --n 50          # smaller sample
    python tag_photos.py review                # rebuild review.html from trial.json
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import random
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "out" / "photos.geojson"
DERIVS = HERE / "derivatives"
TAGS_DIR = HERE / "out" / "_tags"

MODEL = "claude-haiku-5-5"
PRICE_IN, PRICE_OUT = 0.10, 0.50          # $ per 1M tokens (prompts <= 100K tokens)

SCENES = ["summit", "alpine", "forest", "lake_river", "waterfall", "desert_canyon",
          "coast", "snow_winter", "camp", "trail", "road_vehicle", "town_city",
          "indoor", "people", "wildlife", "plants_flowers", "food", "sky_sunset",
          "document_screenshot", "other"]
TAGS = ["mountains", "peak", "ridge", "valley", "meadow", "wildflowers", "trees",
        "fall_colors", "lake", "river", "waterfall", "snow", "glacier", "rock",
        "canyon", "desert", "ocean", "beach", "sunset", "sunrise", "clouds",
        "night_sky", "trail", "trailhead_sign", "tent", "campfire", "cabin_hut",
        "road", "car_truck", "bike", "skiing", "climbing", "boat", "dog", "wildlife",
        "bird", "selfie", "group", "food", "town", "building", "map_sign",
        "screenshot", "document", "receipt", "blurry", "panorama", "aerial"]

SCHEMA = {
    "type": "object",
    "properties": {
        "caption": {"type": "string",
                    "description": "One plain sentence, at most 15 words, describing the photo."},
        "scene": {"type": "string", "enum": SCENES},
        "tags": {"type": "array", "items": {"type": "string", "enum": TAGS}},
        "people": {"type": "string", "enum": ["none", "one", "few", "group"]},
        "quality": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "landmark": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "keep": {"type": "boolean"},
        "sensitive": {"type": "boolean"},
    },
    "required": ["caption", "scene", "tags", "people", "quality", "landmark", "keep", "sensitive"],
    "additionalProperties": False,
}

SYSTEM = """You tag photos from one person's personal outdoor adventure archive (hiking, \
backpacking, road trips, mostly the American West) for a private map.

For each photo return:
- caption: one plain, factual sentence (max 15 words). No hype, no guessing names of people.
- scene: the single best scene category.
- tags: every tag from the list that clearly applies (usually 2-6).
- people: how many people are visible.
- quality: how good the photo is as a photo of the place or moment. 5 = striking, \
would pick it to represent the trip; 3 = ordinary but fine; 1 = accidental, badly \
blurred or nearly nothing visible.
- landmark: the name of a specific, identifiable place ONLY if you are confident \
(a well-known peak, lake, arch, monument, or a sign that names it). Otherwise null.
- keep: false only for photos that don't belong on a trip map: screenshots, receipts, \
documents, whiteboards, accidental pocket/ground shots, near-black or blank frames. \
Ordinary photos of people, food or cars on a trip are kept, and so are photos from \
conferences, talks, museums and other events.
- sensitive: true if anyone in the photo - especially a child - is nude or partly \
undressed (bathing, swimming or playing without clothes, diaper only, changing), or \
shows someone using a toilet or potty. These family photos stay private and are hidden \
from the map. Swimsuits and shirtless adults at a beach or lake, and empty bathrooms, \
are not sensitive.

The date and altitude given with each photo are hints from its metadata."""


def load_env():
    envf = HERE / ".anthropic.env"
    if envf.exists():
        for line in envf.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if v.strip():                       # a blank template line isn't a key
                    os.environ.setdefault(k.strip(), v.strip())
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        sys.exit("No Anthropic API key: paste one after ANTHROPIC_API_KEY= in "
                 f"{envf} (create a key at console.anthropic.com).")


def load_manifest():
    feats = json.loads(MANIFEST.read_text(encoding="utf-8"))["features"]
    out = []
    for f in feats:
        p = f["properties"]
        f = DERIVS / p["id"] / "web.webp"
        if f.exists() and f.stat().st_size > 0:          # an empty file is a broken derivative
            out.append(p)
    return out


def sample(photos, n, seed=7):
    """Spread the sample across years (every year represented, bigger years get more),
    with a few 360 panoramas. Deterministic for a given seed."""
    rng = random.Random(seed)
    by_year = defaultdict(list)
    for p in photos:
        by_year[(p.get("timestamp") or "")[:4] or "unknown"].append(p)
    years = sorted(by_year)
    picks = []
    base = max(1, n // (len(years) * 3))           # a floor for small years
    for y in years:
        picks += rng.sample(by_year[y], min(base, len(by_year[y])))
    chosen = {p["id"] for p in picks}
    rest = [p for p in photos if p["id"] not in chosen]
    picks += rng.sample(rest, max(0, n - len(picks)))   # the rest proportional to volume
    return picks[:n]


def context_line(p):
    bits = []
    if p.get("timestamp"):
        bits.append(f"taken {p['timestamp'][:10]}")
    if p.get("altitude") is not None:
        bits.append(f"altitude {round(p['altitude'] * 3.28084):,} ft")
    if p.get("is_360"):
        bits.append("360 panorama (equirectangular)")
    return "Photo metadata: " + (", ".join(bits) if bits else "none") + "."


def image_bytes(p, size=None):
    """The photo's 1600 px web derivative, optionally shrunk so its long edge is `size`
    px (fewer image tokens: 800 px costs roughly a quarter of 1600 px)."""
    src = DERIVS / p["id"] / "web.webp"
    if not size:
        return src.read_bytes()
    from io import BytesIO
    from PIL import Image
    im = Image.open(src)
    im.thumbnail((size, size))
    buf = BytesIO()
    im.save(buf, "WEBP", quality=85)
    return buf.getvalue()


def build_params(p, size=None, url=None):
    """Messages API params for one photo (trial and batch). With `url` the image is
    fetched from there (a short-lived signed bucket link) instead of sent inline, which
    keeps a batch request ~1 KB instead of ~250 KB."""
    if url:
        source = {"type": "url", "url": url}
    else:
        img = base64.standard_b64encode(image_bytes(p, size)).decode()
        source = {"type": "base64", "media_type": "image/webp", "data": img}
    return {
        "model": MODEL,
        "max_tokens": 2000,
        "system": SYSTEM,
        "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": source},
            {"type": "text", "text": context_line(p)},
        ]}],
    }


def tag_one(client, p, size=None):
    import anthropic
    try:
        r = client.messages.create(**build_params(p, size))
    except anthropic.APIStatusError as e:
        return {"id": p["id"], "error": f"{e.status_code}: {e.message}"}
    except anthropic.APIConnectionError as e:
        return {"id": p["id"], "error": f"connection: {e}"}
    usage = {"in": r.usage.input_tokens, "out": r.usage.output_tokens}
    if r.stop_reason == "refusal":
        return {"id": p["id"], "error": "refusal", "usage": usage}
    text = next((b.text for b in r.content if b.type == "text"), None)
    if r.stop_reason == "max_tokens" or text is None:
        return {"id": p["id"], "error": f"no result ({r.stop_reason})", "usage": usage}
    return {"id": p["id"], "tags": json.loads(text), "usage": usage}


def cmd_trial(args):
    load_env()
    import anthropic
    client = anthropic.Anthropic()
    photos = load_manifest()
    picks = sample(photos, args.n)
    meta = {p["id"]: p for p in picks}
    print(f"tagging {len(picks)} of {len(photos):,} photos with {MODEL} ...")
    t0, results = time.time(), []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(tag_one, client, p, args.size) for p in picks]
        for i, fut in enumerate(as_completed(futs), 1):
            res = fut.result()
            res["meta"] = meta[res["id"]]
            results.append(res)
            if res.get("error"):
                print(f"  ! {res['id']}: {res['error']}")
            if i % 25 == 0:
                print(f"  {i}/{len(picks)}")
    tin = sum(r.get("usage", {}).get("in", 0) for r in results)
    tout = sum(r.get("usage", {}).get("out", 0) for r in results)
    cost = tin / 1e6 * PRICE_IN + tout / 1e6 * PRICE_OUT
    ok = sum(1 for r in results if "tags" in r)
    summary = {"model": MODEL, "size": args.size or 1600, "n": len(results), "ok": ok, "input_tokens": tin,
               "output_tokens": tout, "cost_usd": round(cost, 4),
               "per_photo_usd": round(cost / max(1, len(results)), 6),
               "full_library_estimate_usd": round(cost / max(1, len(results)) * len(photos), 2),
               "seconds": round(time.time() - t0)}
    TAGS_DIR.mkdir(parents=True, exist_ok=True)
    sfx = f"_{args.name}" if args.name else ""
    (TAGS_DIR / f"trial{sfx}.json").write_text(json.dumps({"summary": summary, "results": results}, indent=1),
                                              encoding="utf-8")
    write_review(summary, results, TAGS_DIR / f"review{sfx}.html")
    print(json.dumps(summary, indent=1))
    print(f"review: {TAGS_DIR / f'review{sfx}.html'}")


def cmd_review(args):
    for src in sorted(TAGS_DIR.glob("trial*.json")):
        d = json.loads(src.read_text(encoding="utf-8"))
        dst = TAGS_DIR / src.name.replace("trial", "review").replace(".json", ".html")
        write_review(d["summary"], d["results"], dst)
        print(f"review: {dst}")


def write_review(summary, results, path=None):
    """Static review page: thumbnails with caption/scene/tags/quality/keep, filterable."""
    from private_photos import load_private_ids
    private = load_private_ids()          # blurred here too, whatever the model said
    rows = sorted(results, key=lambda r: (r["meta"].get("timestamp") or ""))
    data = json.dumps([{"id": r["id"], "date": (r["meta"].get("timestamp") or "")[:10],
                        "err": r.get("error"), **(r.get("tags") or {}),
                        **({"sensitive": True} if r["id"] in private else {})} for r in rows])
    html = REVIEW_HTML.replace("__DATA__", data).replace("__SUMMARY__", json.dumps(summary))
    (path or TAGS_DIR / "review.html").write_text(html, encoding="utf-8")


REVIEW_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Photo Tag Trial</title>
<style>
:root { --bg:#f6f5f1; --card:#fff; --ink:#1d1f21; --soft:#666; --line:#e2e0d8; --bad:#c0392b; --chip:#eceae3; }
@media (prefers-color-scheme: dark) { :root { --bg:#17181a; --card:#222427; --ink:#ececec; --soft:#9a9a9a; --line:#33363a; --bad:#ff6b5b; --chip:#2e3135; } }
* { box-sizing:border-box; } body { margin:0; background:var(--bg); color:var(--ink); font:14px/1.4 system-ui,sans-serif; }
header { position:sticky; top:0; background:var(--bg); padding:12px 16px; border-bottom:1px solid var(--line); z-index:2; }
h1 { font-size:17px; margin:0 0 6px; } .sum { color:var(--soft); font-size:13px; }
.bar { display:flex; flex-wrap:wrap; gap:8px; margin-top:8px; align-items:center; }
select, label { font:inherit; color:inherit; } select { background:var(--card); border:1px solid var(--line); border-radius:6px; padding:3px 6px; }
.grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(230px,1fr)); gap:12px; padding:16px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; overflow:hidden; }
.card.junk { outline:2px solid var(--bad); }
.card img { width:100%; aspect-ratio:4/3; object-fit:cover; display:block; background:var(--chip); }
.card.sens img { filter:blur(22px); }   /* sensitive photos are never shown in the clear */
.card.sens { outline:2px solid var(--bad); }
.b { padding:8px 10px; } .cap { margin:0 0 6px; } .meta { color:var(--soft); font-size:12px; }
.chips { display:flex; flex-wrap:wrap; gap:4px; margin-top:6px; } .chip { background:var(--chip); border-radius:999px; padding:1px 8px; font-size:12px; }
.junkb { color:var(--bad); font-weight:600; font-size:12px; } .lm { font-weight:600; }
</style></head><body>
<header><h1>Photo tag trial</h1><div class="sum" id="sum"></div>
<div class="bar"><select id="scene"><option value="">all scenes</option></select>
<select id="q"><option value="">any quality</option><option>5</option><option>4</option><option>3</option><option>2</option><option>1</option></select>
<label><input type="checkbox" id="junk"> junk only</label><label><input type="checkbox" id="sens"> sensitive only</label><label><input type="checkbox" id="lm"> landmarks only</label>
<span class="sum" id="count"></span></div></header>
<div class="grid" id="grid"></div>
<script>
const D = __DATA__, S = __SUMMARY__;
document.getElementById('sum').textContent = `${S.ok}/${S.n} tagged with ${S.model} · ${S.input_tokens.toLocaleString()} in / ${S.output_tokens.toLocaleString()} out tokens · $${S.cost_usd} ($${S.per_photo_usd}/photo) · whole library ≈ $${S.full_library_estimate_usd} (≈ half with the Batch API)`;
const sc = document.getElementById('scene');
[...new Set(D.map(d => d.scene).filter(Boolean))].sort().forEach(s => sc.add(new Option(s, s)));
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function render() {
  const f = { s: sc.value, q: document.getElementById('q').value, j: document.getElementById('junk').checked, l: document.getElementById('lm').checked, x: document.getElementById('sens').checked };
  const rows = D.filter(d => (!f.s || d.scene === f.s) && (!f.q || String(d.quality) === f.q) && (!f.j || d.keep === false) && (!f.l || d.landmark) && (!f.x || d.sensitive));
  document.getElementById('count').textContent = `${rows.length} shown`;
  document.getElementById('grid').innerHTML = rows.map(d => `<div class="card ${d.keep === false ? 'junk' : ''} ${d.sensitive ? 'sens' : ''}">
    <img loading="lazy" src="../../derivatives/${d.id}/web.webp" alt="">
    <div class="b">${d.err ? `<p class="junkb">error: ${esc(d.err)}</p>` : `
      <p class="cap">${esc(d.caption)}</p>
      ${d.landmark ? `<div class="lm">📍 ${esc(d.landmark)}</div>` : ''}
      <div class="meta">${esc(d.date)} · ${esc(d.scene)} · ${'★'.repeat(d.quality || 0)} · people: ${esc(d.people)}
        ${d.keep === false ? ' · <span class="junkb">JUNK</span>' : ''}${d.sensitive ? ' · <span class="junkb">SENSITIVE (hidden)</span>' : ''}</div>
      <div class="chips">${(d.tags || []).map(t => `<span class="chip">${esc(t)}</span>`).join('')}</div>`}
    </div></div>`).join('');
}
['scene', 'q', 'junk', 'lm', 'sens'].forEach(id => document.getElementById(id).addEventListener('input', render));
render();
</script></body></html>
"""


# ----- full library: Message Batches (half price, results within 24 h) ----------
# Images go by signed R2 link (every derivative is already uploaded), so 49k
# requests fit in a handful of batches instead of ~9 GB of inline base64.
TAGS_FILE = HERE / "out" / "photo_tags.json"       # id -> tags (the durable result)
BATCHES_FILE = TAGS_DIR / "batches.json"           # submitted batches + collected flag
BATCH_SIZE = 10_000
LINK_TTL = 3 * 24 * 3600                           # signed links outlive the 24 h batch window


def load_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def save_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)                              # never leave a half-written results file


def cmd_submit(args):
    load_env()
    import anthropic
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request
    from private_photos import load_private_ids
    import upload

    upload.load_env()
    st = upload.store()
    client = anthropic.Anthropic()
    tags = load_json(TAGS_FILE, {})
    batches = load_json(BATCHES_FILE, [])
    pending = {i for b in batches if not b.get("collected") for i in b["ids"]}
    private = load_private_ids()
    todo = [p for p in load_manifest()
            if p["id"] not in tags and p["id"] not in private and p["id"] not in pending]
    if args.limit:
        todo = todo[:args.limit]
    print(f"{len(tags):,} tagged, {len(pending):,} in open batches, {len(private)} private; "
          f"submitting {len(todo):,}")
    for start in range(0, len(todo), BATCH_SIZE):
        chunk = todo[start:start + BATCH_SIZE]
        reqs = []
        for p in chunk:
            # a signed link to the bucket copy; folder storage has none, so the
            # image goes inline instead (bigger requests, same result)
            url = st.presign(f"photos/{p['id']}/web.webp", LINK_TTL)
            reqs.append(Request(custom_id=p["id"],
                                params=MessageCreateParamsNonStreaming(**build_params(p, url=url))))
        b = client.messages.batches.create(requests=reqs)
        batches.append({"id": b.id, "created": time.strftime("%Y-%m-%d %H:%M"),
                        "ids": [p["id"] for p in chunk], "collected": False})
        save_json(BATCHES_FILE, batches)
        print(f"  batch {b.id}: {len(chunk):,} photos ({b.processing_status})")


def cmd_collect(args):
    """Fetch every ended, uncollected batch into photo_tags.json; sensitive -> private list."""
    load_env()
    import anthropic
    from private_photos import add_private_ids
    client = anthropic.Anthropic()
    tags = load_json(TAGS_FILE, {})
    batches = load_json(BATCHES_FILE, [])
    tin = tout = 0
    for b in batches:
        if b.get("collected"):
            continue
        st = client.messages.batches.retrieve(b["id"])
        c = st.request_counts
        print(f"batch {b['id']}: {st.processing_status} - {c.succeeded:,} ok, {c.errored:,} errored, "
              f"{c.expired:,} expired, {c.processing:,} processing")
        if st.processing_status != "ended":
            continue
        got = bad = 0
        sensitive = []
        for r in client.messages.batches.results(b["id"]):
            if r.result.type != "succeeded":
                bad += 1                                  # left untagged -> next `submit` retries it
                continue
            msg = r.result.message
            tin += msg.usage.input_tokens
            tout += msg.usage.output_tokens
            text = next((x.text for x in msg.content if x.type == "text"), None)
            if msg.stop_reason != "end_turn" or text is None:
                bad += 1
                continue
            t = json.loads(text)
            tags[r.custom_id] = {**t, "model": MODEL}
            got += 1
            if t.get("sensitive"):
                sensitive.append(r.custom_id)
        b["collected"] = True
        save_json(TAGS_FILE, tags)
        save_json(BATCHES_FILE, batches)
        added = add_private_ids(sensitive, "flagged sensitive by tag_photos")
        print(f"  collected {got:,} tagged, {bad:,} to retry, {len(added)} newly private")
    if tin or tout:
        cost = (tin * PRICE_IN + tout * PRICE_OUT) / 1e6 * 0.5      # batch = half price
        print(f"this collect: {tin:,} in / {tout:,} out tokens = ${cost:.2f}")
    print(f"photo_tags.json: {len(tags):,} photos tagged")


def cmd_new(args):
    """Nightly: tag photos added since the last run, directly (a night's handful doesn't
    need a batch). Sensitive ones go on the private list; prints `newly private=N`
    for auto_import.py."""
    load_env()
    import anthropic
    from private_photos import load_private_ids, add_private_ids
    client = anthropic.Anthropic()
    tags = load_json(TAGS_FILE, {})
    private = load_private_ids()
    todo = [p for p in load_manifest() if p["id"] not in tags and p["id"] not in private][:args.max]
    if not todo:
        print("tagged=0 newly private=0")
        return
    tin = tout = 0
    sensitive = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for res in ex.map(lambda p: tag_one(client, p), todo):
            u = res.get("usage", {})
            tin, tout = tin + u.get("in", 0), tout + u.get("out", 0)
            if "tags" in res:
                tags[res["id"]] = {**res["tags"], "model": MODEL}
                if res["tags"].get("sensitive"):
                    sensitive.append(res["id"])
            else:
                print(f"  ! {res['id']}: {res.get('error')}")
    save_json(TAGS_FILE, tags)
    added = add_private_ids(sensitive, "flagged sensitive by tag_photos")
    cost = (tin * PRICE_IN + tout * PRICE_OUT) / 1e6
    print(f"tagged={sum(1 for p in todo if p['id'] in tags)} newly private={len(added)} cost=${cost:.3f}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("new", help="nightly: tag untagged photos directly (no batch)")
    n.add_argument("--max", type=int, default=2000, help="cap per run (cost guard)")
    s = sub.add_parser("submit", help="queue untagged photos as Message Batches (signed R2 links)")
    s.add_argument("--limit", type=int, default=0, help="only submit the first N (for a test)")
    sub.add_parser("collect", help="download finished batches into out/photo_tags.json")
    t = sub.add_parser("trial", help="tag a sample and build a review page")
    t.add_argument("--n", type=int, default=200)
    t.add_argument("--workers", type=int, default=8)
    t.add_argument("--size", type=int, default=None, help="shrink the long edge to N px before sending")
    t.add_argument("--name", default="", help="suffix for the output files, e.g. 800 -> trial_800.json")
    sub.add_parser("review", help="rebuild the review pages from out/_tags/trial*.json")
    args = ap.parse_args()
    {"trial": cmd_trial, "review": cmd_review, "submit": cmd_submit, "collect": cmd_collect,
     "new": cmd_new}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
