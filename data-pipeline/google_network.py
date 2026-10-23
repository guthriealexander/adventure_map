#!/usr/bin/env python3
"""Aggregate matched trips into one "roads travelled" network.

Sources (both produced by `snap_regions.py match ... --keep-edges`):
  * Google history  out/_snap/match_results_google.json   every edge of every trip
  * Hikes layer     out/_snap/match_results.json          only the DRIVING: all edges
                    of drive tracks (incl. the Google "Directions from" routes), and
                    road-class edges of mixed drive+hike trips. Foot travel stays in
                    the Hikes layer.
Drive stretches of the hikes layer that found no network (kept as raw GPS) are
carried over as plain lines so nothing that was on the Drives layer disappears.

One line per network edge, with:
    n_trips     how many trips used it (both sources)
    n_days      distinct dates (Google trips carry a date; hikes-layer drives don't)
    first/last  first and last date seen
    mode        dominant travel mode (drive / hike / unknown)
    class       the Overture road class ("gps" for carried-over raw stretches)

    python google_network.py
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
SNAP = HERE / "out" / "_snap"
DRIVE_MODES = {"IN_PASSENGER_VEHICLE", "IN_BUS", "MOTORCYCLING", "IN_TRAIN", "IN_FERRY"}
FOOT_MODES = {"WALKING", "RUNNING", "HIKING", "CYCLING"}
ROAD_CLASSES = {"motorway", "trunk", "primary", "secondary", "tertiary", "residential",
                "unclassified", "living_street"}
FOOT_CLASSES = {"footway", "path", "steps", "cycleway", "bridleway", "pedestrian"}
SPUR_M = 250.0          # dead-end stubs shorter than this, used once, are matching noise
MINOR_CLASSES = {"residential", "service", "unclassified", "living_street", "unknown"}
SPUR_ROUNDS = 3


def edge_len_m(coords):
    import math
    return sum(math.hypot((b[0] - a[0]) * 111320 * math.cos(math.radians(a[1])), (b[1] - a[1]) * 110540)
               for a, b in zip(coords, coords[1:]))


def prune_spurs(agg, geom):
    """Drop once-used dead-end stubs (driveway / side-street spikes where a noisy fix
    pulled the route off the road and straight back). Repeated a few rounds so a
    two-edge spike goes too; real dead ends you use (home, trailheads) have repeats."""
    removed = 0
    for _ in range(SPUR_ROUNDS):
        deg = defaultdict(int)
        for key in agg:
            _, u, v = key.split("|")
            deg[u] += 1; deg[v] += 1
        drop = [key for key, a in agg.items()
                if a["n"] == 1 and (deg[key.split("|")[1]] == 1 or deg[key.split("|")[2]] == 1)
                and edge_len_m(geom[key]["coords"]) < SPUR_M]
        if not drop:
            break
        for key in drop:
            del agg[key]
        removed += len(drop)
    return removed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--google", type=Path, default=SNAP / "match_results_google.json")
    ap.add_argument("--hikes", type=Path, default=SNAP / "match_results.json")
    ap.add_argument("--hikes-snapped", type=Path, default=SNAP / "all_snapped.geojson")
    ap.add_argument("--out", type=Path, default=SNAP / "google_network.geojson")
    args = ap.parse_args()

    agg = defaultdict(lambda: {"n": 0, "seen": 0, "days": set(), "modes": defaultdict(int)})
    geom = {}

    def add(key, e, date, mode):
        a = agg[key]
        a["n"] += 1
        a["seen"] += 1 if e.get("seen", True) else 0
        if date:
            a["days"].add(date)
        a["modes"][mode] += 1
        geom.setdefault(key, e)

    # Google history: everything except recorded walks (those aren't roads travelled)
    for r in json.loads(args.google.read_text(encoding="utf-8")):
        p = r["props"]
        gmode = p.get("mode")
        if gmode in FOOT_MODES:
            continue
        mode = ("drive" if gmode in DRIVE_MODES else "hike" if gmode in FOOT_MODES
                else r.get("category") if r.get("category") in ("drive", "hike") else "unknown")
        for key, e in (r.get("edge_geom") or {}).items():
            if e["cls"] not in FOOT_CLASSES:
                add(key, e, p.get("date"), mode)
    n_google = len(agg)

    # Hikes layer: its driving only
    n_hike_tracks = 0
    if args.hikes.exists():
        for r in json.loads(args.hikes.read_text(encoding="utf-8")):
            cat = r.get("category")
            if cat not in ("drive", "mixed"):
                continue
            edges = r.get("edge_geom") or {}
            if not edges:
                continue
            n_hike_tracks += 1
            for key, e in edges.items():
                if e["cls"] in FOOT_CLASSES:
                    continue
                if cat == "drive" or e["cls"] in ROAD_CLASSES:
                    add(key, e, None, "drive")
    n_before = len(agg)
    # a minor street used once with no GPS fix anywhere near it was only a guess the
    # router made between two fixes (a highway between distant fixes is still kept)
    guesses = [k for k, a in agg.items()
               if geom[k]["cls"] in MINOR_CLASSES and a["n"] == 1 and a["seen"] == 0]
    for k in guesses:
        del agg[k]
    spurs = prune_spurs(agg, geom)

    feats = []
    for key, a in agg.items():
        e = geom[key]
        days = sorted(a["days"])
        mode = max(a["modes"].items(), key=lambda kv: kv[1])[0]
        feats.append({"type": "Feature",
                      "properties": {"n_trips": a["n"], "n_days": len(days),
                                     "first": days[0] if days else None,
                                     "last": days[-1] if days else None,
                                     "mode": mode, "class": e["cls"]},
                      "geometry": {"type": "LineString", "coordinates": e["coords"]}})

    # carried-over raw drive stretches (no network nearby) from the hikes layer
    raw_n = 0
    if args.hikes_snapped.exists():
        for f in json.loads(args.hikes_snapped.read_text(encoding="utf-8"))["features"]:
            p = f["properties"]
            if p.get("part") == "raw" and p.get("mode") == "drive":
                for line in f["geometry"]["coordinates"]:
                    if len(line) > 1:
                        feats.append({"type": "Feature",
                                      "properties": {"n_trips": 1, "n_days": 0, "first": None, "last": None,
                                                     "mode": "drive", "class": "gps"},
                                      "geometry": {"type": "LineString", "coordinates": line}})
                        raw_n += 1

    args.out.write_text(json.dumps({"type": "FeatureCollection", "features": feats}), encoding="utf-8")
    hist = defaultdict(int)
    for f in feats:
        n = f["properties"]["n_trips"]
        hist["1" if n == 1 else "2-5" if n <= 5 else "6-20" if n <= 20 else "21+"] += 1
    print(f"{len(feats):,} lines -> {args.out} ({args.out.stat().st_size / 1e6:.0f} MB)")
    print(f"  edges from Google history: {n_google:,} | +{n_before - n_google:,} new edges from "
          f"{n_hike_tracks:,} hikes-layer drive/mixed tracks | -{len(guesses):,} unseen minor streets "
          f"| -{spurs:,} dead-end spurs pruned "
          f"| +{raw_n} raw drive stretches")
    print("  trips per edge:", dict(hist))


if __name__ == "__main__":
    main()
