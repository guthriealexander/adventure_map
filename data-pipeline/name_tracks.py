#!/usr/bin/env python3
"""Give trip tracks real names ("Crater Lake via Maroon Lake") from OpenStreetMap.

For every foot track that belongs to a trip (out/trips_draft.json), walk along it and
record which named OSM trails it follows (OSM US trails tileset, z14), where it starts
(trailheads / parking / named places nearby) and what it reaches (named peaks, lakes,
waterfalls and viewpoints near its turnaround point or anywhere on the line, from the
OpenMapTiles basemap and the trails tileset's POIs). Claude Haiku 5.5 turns those facts
into a short guidebook-style name; it may only use names from the facts. Tiles are
cached in out/_tracks/tiles/, names in out/track_names.json keyed "garmin:<id>" /
"hike:<index>" with a hash of the facts, so reruns only ask about new/changed tracks.

Usage:
    python name_tracks.py              # name new/changed trip tracks
    python name_tracks.py --limit 20   # try a few first
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
import urllib.parse
import urllib.request
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from tag_photos import load_env, MODEL, PRICE_IN, PRICE_OUT

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
TILES = OUT / "_tracks" / "tiles"
NAMES = OUT / "track_names.json"
Z = 14
SRC = {"trails": "https://tiles.openstreetmap.us/vector/trails/{z}/{x}/{y}.mvt",
       "omt": "https://tiles.openstreetmap.us/vector/openmaptiles/{z}/{x}/{y}.mvt"}
STEP_M = 40            # sample spacing along a track
MATCH_M = 25           # a sample within this of a named trail counts as on it
NEAR_M = 350           # named features this close to the turnaround / start count
ON_M = 120             # peaks/lakes this close to ANY point were "reached"
FOOT_NAMES = {"hiking": "hike", "trail_running": "trail run", "walking": "walk", "running": "run",
              "cross_country_skiing_ws": "ski", "hike": "hike"}

SCHEMA = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"],
          "additionalProperties": False}
SYSTEM = """You name recorded hikes, runs and walks for a personal map, like a trail guidebook \
would: "Crater Lake via Maroon Lake", "Mount Sniktau from Loveland Pass", "Cirque of the \
Towers loop", "Lake Mendota shoreline walk", "Green Mountain trail run". Use the destination \
(summit, lake, falls) when the track reaches one, and the main trail or the start for \
context. Only use names that appear in the facts; if no trail or feature is named, describe \
it plainly from the area ("Run near Riverside", "Walk in Old Town"). Never name a track after a shop, cafe, restaurant, fuel station or other business, and never say "start point". On a long-distance trail (Pacific Crest Trail, John Muir Trail, Continental Divide Trail, Colorado Trail, Appalachian Trail) name the stretch by the pass, lake or peak near its far end or start, e.g. "PCT over Forester Pass" or "Colorado Trail to Kenosha Pass". 2-7 words, no dates, no distances."""


# ----- geometry -----------------------------------------------------------------
def meters(a, b):
    """Approximate metres between (lon, lat) points."""
    k = math.cos(math.radians((a[1] + b[1]) / 2))
    return math.hypot((a[0] - b[0]) * k, a[1] - b[1]) * 111_320


def seg_dist(p, a, b):
    k = math.cos(math.radians(p[1]))
    ax, ay, bx, by, px, py = a[0] * k, a[1], b[0] * k, b[1], p[0] * k, p[1]
    dx, dy = bx - ax, by - ay
    t = 0 if dx == dy == 0 else max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - ax - t * dx, py - ay - t * dy) * 111_320


def sample(coords):
    out, acc = [coords[0]], 0.0
    for a, b in zip(coords, coords[1:]):
        acc += meters(a, b)
        if acc >= STEP_M:
            out.append(b)
            acc = 0.0
    if out[-1] != coords[-1]:
        out.append(coords[-1])
    return out[:600]


def tile_of(lon, lat):
    n = 2 ** Z
    lat = max(-85, min(85, lat))
    return (int((lon + 180) / 360 * n),
            int((1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n))


# ----- tiles ----------------------------------------------------------------------
_cache: dict = {}


def load_tile(src, x, y):
    """Decoded features of one z14 tile in lon/lat: {layer: [(geomtype, coords, props)]}."""
    key = (src, x, y)
    if key in _cache:
        return _cache[key]
    f = TILES / src / f"{x}_{y}.mvt"
    if not f.exists():
        f.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(SRC[src].format(z=Z, x=x, y=y),
                                     headers={"User-Agent": "adventure-map/1.0 (personal track names)"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
        except Exception:
            data = b""
        f.write_bytes(data)
        time.sleep(0.15)                                 # be polite to the free tile service
    data = f.read_bytes()
    layers = {}
    if data:
        import mapbox_vector_tile
        dec = mapbox_vector_tile.decode(data, default_options={"y_coord_down": True})
        n = 2 ** Z
        for lname, layer in dec.items():
            ext = layer.get("extent", 4096)

            def ll(pt):
                lon = (x + pt[0] / ext) / n * 360 - 180
                yy = (y + pt[1] / ext) / n
                lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy))))
                return (lon, lat)
            feats = []
            for ft in layer["features"]:
                g, props = ft["geometry"], ft.get("properties", {})
                if not props.get("name"):
                    continue
                t, c = g["type"], g["coordinates"]
                if t == "Point":
                    feats.append(("pt", [ll(c)], props))
                elif t == "MultiPoint":
                    feats += [("pt", [ll(p)], props) for p in c]
                elif t == "LineString":
                    feats.append(("ln", [ll(p) for p in c], props))
                elif t == "MultiLineString":
                    feats += [("ln", [ll(p) for p in l], props) for l in c]
                elif t in ("Polygon", "MultiPolygon"):
                    rings = c if t == "Polygon" else [r for poly in c for r in poly]
                    feats += [("ln", [ll(p) for p in r], props) for r in rings[:1]]
            layers[lname] = feats
    _cache[key] = layers
    if len(_cache) > 400:                                # bounded memory
        _cache.pop(next(iter(_cache)))
    return layers


def feats_near(pts, src, layer_names, ring=True):
    """Features from the tiles under `pts` (ring=True also adds each tile's 8
    neighbours - only worth it for a few anchor points, since a feature 350 m away
    can sit across a tile edge)."""
    seen = set()
    for p in pts:
        x, y = tile_of(*p)
        if not ring:
            seen.add((x, y))
            continue
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                seen.add((x + dx, y + dy))
    for (x, y) in seen:
        layers = load_tile(src, x, y)
        for ln in layer_names:
            yield from layers.get(ln, [])


def nearest_named(p, feats, kinds=None):
    best = None
    for gt, cs, props in feats:
        if kinds and not kinds(props):
            continue
        d = meters(p, cs[0]) if gt == "pt" else min(seg_dist(p, a, b) for a, b in zip(cs, cs[1:])) if len(cs) > 1 else 1e9
        if best is None or d < best[0]:
            best = (d, props.get("name"), props)
    return best


# ----- facts per track ---------------------------------------------------------------
AREA_CACHE = OUT / "_tracks" / "area.json"
_areas = json.loads(AREA_CACHE.read_text(encoding="utf-8")) if AREA_CACHE.exists() else {}


def place_area(lon, lat):
    """'Town, County, State' for a start point (~1 km cells, cached)."""
    key = f"{lat:.2f},{lon:.2f}"
    if key not in _areas:
        url = "https://photon.komoot.io/reverse?" + urllib.parse.urlencode({"lat": round(lat, 4), "lon": round(lon, 4)})
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "adventure-map/1.0 (personal track names)"})
            with urllib.request.urlopen(req, timeout=20) as r:
                feats = json.loads(r.read()).get("features") or []
            q = feats[0]["properties"] if feats else {}
            _areas[key] = ", ".join(dict.fromkeys(x for x in (q.get("city") or q.get("town") or q.get("village"),
                                                            q.get("county"), q.get("state"), q.get("country")) if x))
        except Exception:
            return ""
        time.sleep(0.25)
    return _areas[key]


def area(garmin_name):
    """Garmin's auto label is '<county or town> <Activity>'; keep just the place, so the
    model doesn't parrot "Park County Walking"."""
    if not garmin_name:
        return ""
    words = garmin_name.split()
    while words and words[-1].lower() in ("walking", "running", "hiking", "trail", "cycling", "skiing", "biking"):
        words.pop()
    return " ".join(words)


def track_facts(kind, coords, km, garmin_name):
    pts = sample(coords)
    start, end = pts[0], pts[-1]
    far = max(pts, key=lambda p: meters(start, p))
    loop = meters(start, end) < 400 and meters(start, far) > 600
    # trails followed, in order, with metres on each
    # named trail segments, bucketed into ~200 m cells so each sample only checks
    # the segments around it (a sample's own tile is enough: 25 m rarely crosses an edge)
    CELL = 0.002
    grid = {}
    seen = set()
    for gt, cs, props in feats_near(pts, "trails", ["trail"], ring=False):
        if gt != "ln" or len(cs) < 2:
            continue
        for a, b in zip(cs, cs[1:]):
            if (a, b) in seen:
                continue
            seen.add((a, b))
            for cx in range(int(min(a[0], b[0]) // CELL) - 1, int(max(a[0], b[0]) // CELL) + 2):
                for cy in range(int(min(a[1], b[1]) // CELL) - 1, int(max(a[1], b[1]) // CELL) + 2):
                    grid.setdefault((cx, cy), []).append((a, b, props["name"]))
    on = OrderedDict()
    for p in pts:
        best = None
        for a, b, nm in grid.get((int(p[0] // CELL), int(p[1] // CELL)), ()):
            d = seg_dist(p, a, b)
            if d <= MATCH_M and (best is None or d < best[0]):
                best = (d, nm)
        if best:
            on[best[1]] = on.get(best[1], 0) + STEP_M
    trails = [(n, round(m / 1000, 1)) for n, m in on.items() if m >= 200]
    # named features: OMT peaks / water names / POIs and trail POIs
    omt = list(feats_near([start, far, end], "omt", ["mountain_peak", "water_name", "poi", "park"]))
    tpoi = list(feats_near([start, far, end], "trails", ["trail_poi"]))
    near = lambda p, fs: sorted({(round(meters(p, cs[0])), props["name"]) for gt, cs, props in fs
                                 if gt == "pt" and meters(p, cs[0]) <= NEAR_M})
    reached = set()
    for gt, cs, props in omt:
        if gt == "pt" and props.get("class") in ("peak", "volcano", "saddle", "lake", "waterfall"):
            if min(meters(cs[0], p) for p in pts[::3]) <= ON_M:
                reached.add(props["name"])
    lines = [
        f"Activity: {FOOT_NAMES.get(kind, kind)}, {km:.1f} km, {'loop' if loop else 'out-and-back or point-to-point'}.",
        f"Area: {place_area(*start) or area(garmin_name)}.",
        "Named trails followed, in order (km on each): " + ("; ".join(f"{n} ({k})" for n, k in trails) or "none") + ".",
        "Named things near the start: " + ("; ".join(n for _, n in near(start, omt + tpoi)[:5]) or "none") + ".",
        "Named things near the farthest point: " + ("; ".join(n for _, n in near(far, omt + tpoi)[:6]) or "none") + ".",
        "Peaks, lakes or falls passed within ~100 m: " + ("; ".join(sorted(reached)) or "none") + ".",
    ]
    return "\n".join(l for l in lines if l)


def load_tracks():
    trips = json.loads((OUT / "trips_draft.json").read_text(encoding="utf-8"))["trips"]
    want = {}
    for t in trips:
        for x in t["tracks"]:
            if x["type"] != "drive":
                want[f"{x['src']}:{x['id']}"] = x
    out = []
    for line in open(OUT / "garmin_tracks.geojsonl", encoding="utf-8"):
        f = json.loads(line)
        key = f"garmin:{f['properties']['activityId']}"
        if key in want:
            out.append((key, f["properties"]["type"], f["geometry"]["coordinates"], want[key]["km"], f["properties"]["name"]))
    for i, line in enumerate(open(OUT / "hikes.geojsonl", encoding="utf-8")):
        key = f"hike:{i}"
        if key in want:
            g = json.loads(line)["geometry"]
            ls = [l for l in (g["coordinates"] if g["type"] == "MultiLineString" else [g["coordinates"]]) if l]
            coords = [c for l in ls for c in l]
            if len(coords) > 1:
                out.append((key, "hike", coords, want[key]["km"], None))
    return out


def ask(client, text):
    r = client.messages.create(model=MODEL, max_tokens=800, system=SYSTEM,
                               output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
                               messages=[{"role": "user", "content": text}])
    txt = next((b.text for b in r.content if b.type == "text"), None)
    name = json.loads(txt)["name"] if (txt and r.stop_reason == "end_turn") else None
    return name, r.usage.input_tokens, r.usage.output_tokens


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    load_env()
    import anthropic
    client = anthropic.Anthropic()
    names = json.loads(NAMES.read_text(encoding="utf-8")) if NAMES.exists() else {}
    tracks = load_tracks()
    jobs = []
    for n, (key, kind, coords, km, gname) in enumerate(tracks, 1):
        text = track_facts(kind, coords, km, gname)
        h = hashlib.sha1(text.encode()).hexdigest()[:16]
        if names.get(key, {}).get("hash") != h:
            jobs.append((key, text, h))
        if n % 50 == 0:
            print(f"  facts {n}/{len(tracks)}", flush=True)
        if args.limit and len(jobs) >= args.limit:
            break
    print(f"{len(tracks)} trip tracks, {len(jobs)} to (re)name")
    tin = tout = 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(ask, client, text): (key, text, h) for key, text, h in jobs}
        for fut in as_completed(futs):
            key, text, h = futs[fut]
            try:
                name, i, o = fut.result()
            except anthropic.APIError as e:
                print(f"  ! {key}: {e}")
                continue
            tin, tout = tin + i, tout + o
            if name:
                names[key] = {"name": name, "hash": h}
                if len(names) % 100 == 0:
                    NAMES.write_text(json.dumps(names, indent=1), encoding="utf-8")
    NAMES.write_text(json.dumps(names, indent=1), encoding="utf-8")
    AREA_CACHE.write_text(json.dumps(_areas), encoding="utf-8")
    print(f"named {len(names)} tracks; cost ${(tin * PRICE_IN + tout * PRICE_OUT) / 1e6:.3f}")


if __name__ == "__main__":
    main()
