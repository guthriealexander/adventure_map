#!/usr/bin/env python3
"""Name and summarise trips with Claude Haiku 5.5, and pick each trip's hero photo.

Reads out/trips_draft.json (build_trips.py) and out/photo_tags.json (tag_photos.py).
For every trip it gathers: dates and stats, place names along the way (Photon reverse
geocoding, cached in out/_trips/geocode.json), Garmin activity names, and the captions /
landmarks of the trip's best photos. Haiku returns {name, summary, place, hero}; the hero
must be one of the candidate photos offered. Results are cached per trip in
out/_trips/names.json, keyed by a hash of the inputs, so nightly runs only re-ask for
trips that changed. Then out/trips.json is rewritten for the web panel.

Usage:
    python name_trips.py              # name new/changed trips, rewrite out/trips.json
    python name_trips.py --limit 10   # try a few first
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from tag_photos import load_env, MODEL, PRICE_IN, PRICE_OUT
from build_trips import write_public

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
WORK = OUT / "_trips"
GEOCODE = WORK / "geocode.json"
NAMES = WORK / "names.json"
PHOTON = "https://photon.komoot.io/reverse"
# name_tracks.py output: real track names ("Crater Lake via Maroon Lake") beat Garmin's
TRACK_NAMES = (json.loads((OUT / "track_names.json").read_text(encoding="utf-8"))
               if (OUT / "track_names.json").exists() else {})

SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "2-6 words, the way a person would label this trip."},
        "summary": {"type": "string", "description": "1-2 plain sentences on what the trip was."},
        "place": {"type": "string", "description": "Short region label, e.g. 'Aspen, Colorado' or 'Utah and Arizona'."},
        "hero": {"type": "string", "description": "The id of the best candidate photo to represent the trip."},
    },
    "required": ["name", "summary", "place", "hero"],
    "additionalProperties": False,
}

SYSTEM = """You label trips in one person's private adventure map (hiking, backpacking, road \
trips, travel). From the facts given, return:
- name: 2-6 words, specific and natural, like a person would title a photo album: \
"Maroon Bells fall colors", "Moab with friends", "Thanksgiving with family", "Lisbon". \
Prefer the main destination or landmark; never invent a place that isn't in the facts. \
No dates in the name.
- summary: 1-2 plain sentences saying what the trip was (where, what kind of trip, highlights \
supported by the facts). No hype, no guessing names of people.
- place: a short region label.
- hero: the id of the candidate photo that best represents the trip (a striking photo of \
the place or activity, not a screenshot, car interior or blurry shot).
If the person had a name already for the trip, keep it as the name."""


def load(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=1), encoding="utf-8")
    tmp.replace(path)


# ----- places ---------------------------------------------------------------------
def reverse(lat, lon, cache):
    key = f"{lat:.2f},{lon:.2f}"                        # ~1 km cells, plenty for a label
    if key in cache:
        return cache[key]
    url = PHOTON + "?" + urllib.parse.urlencode({"lat": round(lat, 4), "lon": round(lon, 4)})
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "adventure-map/1.0 (personal trip labels)"})
        with urllib.request.urlopen(req, timeout=20) as r:
            feats = json.loads(r.read()).get("features") or []
        p = feats[0]["properties"] if feats else {}
        label = ", ".join(dict.fromkeys(x for x in (p.get("name"), p.get("city"), p.get("county"),
                                                     p.get("state"), p.get("country")) if x))
    except Exception:
        return None                                      # not cached -> retried next run
    cache[key] = label
    time.sleep(0.3)                                      # be polite to the free service
    return label


def trip_points(t, photos_by_id):
    """A few representative points: bbox centre plus spread photo positions."""
    w, s, e, n = t["bbox"]
    pts = [((s + n) / 2, (w + e) / 2)]
    ph = [photos_by_id[i] for i in t.get("photo_ids", []) if i in photos_by_id]
    for k in range(1, 5):
        if len(ph) > k * 4:
            p = ph[len(ph) * k // 5]
            pts.append((p["lat"], p["lon"]))
    return pts


# ----- facts for the model ------------------------------------------------------------
def facts(t, tags, places):
    cand = [(i, tags[i]) for i in t.get("photo_ids", []) if i in tags
            and tags[i].get("keep", True) and not tags[i].get("sensitive")]
    cand.sort(key=lambda x: (-x[1].get("quality", 0), x[0]))
    best = cand[:10]
    landmarks = Counter(x.get("landmark") for _, x in cand if x.get("landmark"))
    scenes = Counter(x.get("scene") for _, x in cand)
    garmin = Counter((TRACK_NAMES.get(f"{x['src']}:{x['id']}") or {}).get("name") or x.get("name")
                     for x in t["tracks"] if x.get("name") or f"{x['src']}:{x['id']}" in TRACK_NAMES)
    lines = [
        f"Dates: {t['start']} to {t['end']} ({t['days']} day{'s' if t['days'] != 1 else ''}); "
        f"home base at the time: {t['home']}.",
        f"Kind: {'a named trip' if t['kind'] == 'named' else 'hikes near home over the year' if t['kind'] == 'local' else 'a trip away from home'}."
        + (f" Its name: {t['name']}." if t.get("name") else ""),
        "Places along the way: " + ("; ".join(dict.fromkeys(p for p in places if p)) or "unknown") + ".",
        f"On foot: {t['hiked_km']:.1f} km; photos: {t['photos']}"
        + (f"; highest photo altitude {round(t['high_m'] * 3.28084):,} ft" if t.get("high_m") else "") + ".",
    ]
    if garmin:
        lines.append("Recorded hikes/runs/walks: " + "; ".join(f"{k} x{v}" for k, v in garmin.most_common(8)) + ".")
    if landmarks:
        lines.append("Landmarks recognised in photos: " + "; ".join(f"{k} x{v}" for k, v in landmarks.most_common(8)) + ".")
    if scenes:
        lines.append("Photo scenes: " + ", ".join(f"{k} {v}" for k, v in scenes.most_common(6)) + ".")
    lines.append("Candidate photos (id: caption [quality]):")
    lines += [f"- {i}: {x.get('caption', '')} [{x.get('quality', '?')}]" for i, x in best]
    return "\n".join(lines), [i for i, _ in best]


def name_one(client, t, text, candidates):
    r = client.messages.create(
        model=MODEL, max_tokens=1500, system=SYSTEM,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": text}])
    out = None
    if r.stop_reason == "end_turn":
        txt = next((b.text for b in r.content if b.type == "text"), None)
        out = json.loads(txt) if txt else None
        if out and out["hero"] not in candidates:          # never trust an id we didn't offer
            out["hero"] = candidates[0] if candidates else None
    return out, r.usage.input_tokens, r.usage.output_tokens


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()
    load_env()
    import anthropic
    client = anthropic.Anthropic()

    trips = load(OUT / "trips_draft.json", {})["trips"]
    tags = load(OUT / "photo_tags.json", {})
    cache = load(GEOCODE, {})
    names = load(NAMES, {})
    photos_by_id = {f["properties"]["id"]: {"lat": f["geometry"]["coordinates"][1], "lon": f["geometry"]["coordinates"][0]}
                    for f in json.loads((OUT / "photos.geojson").read_text(encoding="utf-8"))["features"]}

    jobs = []
    for t in trips:
        places = [reverse(lat, lon, cache) for lat, lon in trip_points(t, photos_by_id)]
        text, cands = facts(t, tags, places)
        h = hashlib.sha1(text.encode()).hexdigest()[:16]
        if names.get(t["id"], {}).get("hash") != h:
            jobs.append((t, text, cands, h))
    save(GEOCODE, cache)
    if args.limit:
        jobs = jobs[:args.limit]
    print(f"{len(trips)} trips, {len(jobs)} to (re)name, {len(tags):,} tagged photos available")

    tin = tout = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(name_one, client, t, text, cands): (t, h) for t, text, cands, h in jobs}
        for fut in as_completed(futs):
            t, h = futs[fut]
            try:
                out, i, o = fut.result()
            except anthropic.APIError as e:
                print(f"  ! {t['id']}: {e}")
                continue
            tin, tout = tin + i, tout + o
            if out:
                names[t["id"]] = {**out, "hash": h}
                print(f"  {t['start']}  {out['name']}  ({out['place']})")
    save(NAMES, names)
    write_public(trips, names={k: v for k, v in names.items()},
                 heroes={k: v.get("hero") for k, v in names.items()})
    print(f"cost ${(tin * PRICE_IN + tout * PRICE_OUT) / 1e6:.3f} ({tin:,} in / {tout:,} out tokens)")
    print(f"wrote {OUT / 'trips.json'}")


if __name__ == "__main__":
    main()
