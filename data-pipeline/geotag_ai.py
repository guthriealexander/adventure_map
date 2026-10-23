#!/usr/bin/env python3
"""Suggest locations for photos with no GPS, from what's in the picture (Claude Haiku 5.5).

For each photo in out/untagged.geojson, the model sees the image plus where the person
probably was that day (the trip in progress from out/trips_draft.json + names, or the home
base from homes.json), and returns the usual photo tags (tag_photos.SCHEMA) plus a place to
look up and how sure it is. High/medium-confidence places are forward-geocoded with Photon,
biased to that day's whereabouts and rejected if implausibly far from them. Results:

  out/geotag_ai.json              everything (cache; reruns skip photos already done)
  out/geotag_ai_suggestions.json  id -> {lon, lat, place, confidence, source:"claude"} for
                                  the filmstrip, which shows them apart from GPS matches
                                  and never bulk-accepts them
  out/photo_tags.json             the tags too, so placing a photo later needs no re-tag

Photos flagged sensitive go on private_photos.txt (the geotag gallery skips those).

Usage:
    python geotag_ai.py                 # all untagged photos not done yet
    python geotag_ai.py --limit 50      # try a sample
"""
from __future__ import annotations

import argparse
import json
import math
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

import tag_photos as T
from private_photos import load_private_ids, add_private_ids

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
DONE = OUT / "geotag_ai.json"
SUGG = OUT / "geotag_ai_suggestions.json"
PHOTON = "https://photon.komoot.io/api/"

SCHEMA = json.loads(json.dumps(T.SCHEMA))
SCHEMA["properties"]["place_query"] = {"anyOf": [{"type": "string"}, {"type": "null"}]}
SCHEMA["properties"]["place_confidence"] = {"type": "string", "enum": ["high", "medium", "low", "none"]}
SCHEMA["required"] += ["place_query", "place_confidence"]

SYSTEM = T.SYSTEM + """

This photo has no GPS. Also return:
- place_query: the most specific place you can identify FROM THE IMAGE ITSELF - a named \
peak, lake, landmark, building, trailhead sign, town - written as a search query with its \
region, e.g. "Maroon Lake, Colorado" or "Delicate Arch, Utah". Use the trip context only \
to choose between look-alikes, never as the answer by itself. null if nothing identifiable \
is visible (an indoor room, a close-up, a generic forest).
- place_confidence: high = a recognisable landmark or a readable sign; medium = very likely \
given the scene and the trip context; low = a guess; none = no place."""


def km(a_lat, a_lon, b_lat, b_lon):
    p = math.pi / 180
    h = (math.sin((b_lat - a_lat) * p / 2) ** 2
         + math.cos(a_lat * p) * math.cos(b_lat * p) * math.sin((b_lon - a_lon) * p / 2) ** 2)
    return 12742 * math.asin(math.sqrt(h))


# ----- where was the person that day? ------------------------------------------
def load_context():
    trips = json.loads((OUT / "trips_draft.json").read_text(encoding="utf-8"))["trips"]
    names_f = OUT / "_trips" / "names.json"
    names = json.loads(names_f.read_text(encoding="utf-8")) if names_f.exists() else {}
    homes = json.loads((HERE / "homes.json").read_text(encoding="utf-8"))["homes"]
    return trips, names, homes


def context_for(ts, trips, names, homes):
    """(text for the model, (lat, lon) bias, max plausible km) for a photo's date."""
    if not ts or ts[5:10] == "01-01" or ts.startswith("0000"):
        return "Date unknown (no reliable timestamp).", None, None
    day = ts[:10]
    for t in trips:
        if t["kind"] != "local" and t["start"] <= day <= t["end"]:
            w, s, e, n = t["bbox"]
            nm = names.get(t["id"], {})
            label = t.get("name") or nm.get("name") or "a trip"
            where = nm.get("place") or "unknown area"
            return (f"Taken {day}, during a trip: {label} ({where}), roughly between "
                    f"lat {s:.2f}..{n:.2f}, lon {w:.2f}..{e:.2f}.", ((s + n) / 2, (w + e) / 2),
                    max(150, km(s, w, n, e) / 2 + 150))
    for h in homes:
        if h["from"] <= day <= h["to"]:
            return (f"Taken {day}, not on a recorded trip; home base then: {h['name']}.",
                    (h["lat"], h["lon"]), 400)
    return f"Taken {day}.", None, None


# ----- geocoding ----------------------------------------------------------------------
COARSE = {"city", "county", "state", "country", "district"}   # a pin at a town centre doesn't place a photo


def photon(query, bias):
    params = {"q": query, "limit": 5}
    if bias:
        params |= {"lat": round(bias[0], 3), "lon": round(bias[1], 3)}
    req = urllib.request.Request(PHOTON + "?" + urllib.parse.urlencode(params),
                                 headers={"User-Agent": "adventure-map/1.0 (photo location suggestions)"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read()).get("features") or []
    except Exception:
        return []


def geocode(query, bias, max_km):
    """Look the place up, falling back to less specific versions of the query
    ("Echo Canyon, Chiricahua NM, Arizona" -> "Chiricahua NM, Arizona"); skip results
    that are only a town/county/state, or implausibly far from that day's whereabouts."""
    parts = [x.strip() for x in query.split(",") if x.strip()]
    for k in range(0, max(1, len(parts) - 1)):
        q = ", ".join(parts[k:])
        for f in photon(q, bias):
            lon, lat = f["geometry"]["coordinates"][:2]
            p = f["properties"]
            if bias and max_km and km(bias[0], bias[1], lat, lon) > max_km:
                continue
            if p.get("type") in COARSE:
                return None      # the best match is only a town/region: too vague to place a photo
            label = ", ".join(dict.fromkeys(x for x in (p.get("name"), p.get("state"), p.get("country")) if x))
            return {"lon": round(lon, 6), "lat": round(lat, 6), "place": label, "exact": k == 0}
        time.sleep(0.2)
    return None


def one(client, p, ctx):
    import anthropic
    params = T.build_params(p)
    params["system"] = SYSTEM
    params["output_config"]["format"]["schema"] = SCHEMA
    params["messages"][0]["content"][1]["text"] = ctx[0]
    try:
        r = client.messages.create(**params)
    except anthropic.APIError as e:
        return p["id"], None, (0, 0), str(e)
    text = next((b.text for b in r.content if b.type == "text"), None)
    if r.stop_reason != "end_turn" or not text:
        return p["id"], None, (r.usage.input_tokens, r.usage.output_tokens), f"stop {r.stop_reason}"
    return p["id"], json.loads(text), (r.usage.input_tokens, r.usage.output_tokens), None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--random", action="store_true", help="with --limit: a random sample, not the first N")
    args = ap.parse_args()
    T.load_env()
    import anthropic
    client = anthropic.Anthropic()

    done = json.loads(DONE.read_text(encoding="utf-8")) if DONE.exists() else {}
    private = load_private_ids()
    trips, names, homes = load_context()
    feats = json.loads((OUT / "untagged.geojson").read_text(encoding="utf-8"))["features"]
    todo = []
    for f in feats:
        p = f["properties"]
        src = T.DERIVS / p["id"] / "web.webp"
        if p["id"] in done or p["id"] in private or not src.exists() or src.stat().st_size == 0:
            continue
        todo.append(p)
    if args.limit:
        if args.random:
            import random
            random.Random(11).shuffle(todo)
        todo = todo[:args.limit]
    print(f"{len(feats):,} photos without GPS; {len(done):,} already done; asking about {len(todo):,}", flush=True)

    tags = T.load_json(T.TAGS_FILE, {})
    tin = tout = n = 0
    sensitive = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {}
        for p in todo:
            ctx = context_for(p.get("timestamp") or "", trips, names, homes)
            futs[ex.submit(one, client, p, ctx)] = (p, ctx)
        for fut in as_completed(futs):
            p, ctx = futs[fut]
            pid, res, (i, o), err = fut.result()
            tin, tout, n = tin + i, tout + o, n + 1
            if err:
                print(f"  ! {pid}: {err}")
                continue
            entry = {"tags": res, "context": ctx[0]}
            if res.get("sensitive"):
                sensitive.append(pid)
            elif res.get("place_query") and res.get("place_confidence") in ("high", "medium"):
                g = geocode(res["place_query"], ctx[1], ctx[2])
                time.sleep(0.2)
                if g:
                    entry["suggestion"] = {**g, "query": res["place_query"],
                                           "confidence": res["place_confidence"], "source": "claude"}
            done[pid] = entry
            tags[pid] = {k: v for k, v in res.items() if k in T.SCHEMA["properties"]} | {"model": T.MODEL}
            if n % 250 == 0:
                T.save_json(DONE, done)
                print(f"  {n:,}/{len(todo):,}  ${(tin * T.PRICE_IN + tout * T.PRICE_OUT) / 1e6:.2f}", flush=True)
    T.save_json(DONE, done)
    T.save_json(T.TAGS_FILE, tags)
    added = add_private_ids(sensitive, "flagged sensitive by geotag_ai")
    private = load_private_ids()
    sugg = {i: e["suggestion"] for i, e in done.items() if e.get("suggestion") and i not in private}
    SUGG.write_text(json.dumps(sugg, separators=(",", ":")), encoding="utf-8")
    conf = {}
    for e in sugg.values():
        conf[e["confidence"]] = conf.get(e["confidence"], 0) + 1
    print(f"asked {n:,}: {len(sugg):,} location suggestions {conf}, {len(added)} newly private; "
          f"cost ${(tin * T.PRICE_IN + tout * T.PRICE_OUT) / 1e6:.2f}")


if __name__ == "__main__":
    main()
