#!/usr/bin/env python3
"""Convert Google's new-format Timeline export into GeoJSON layers.

The 2024+ "semanticSegments" export differs completely from the old
Records.json / location_history format. This parses it into three layers:

  * visits      (Point)        - places you stopped, with placeId + semanticType
  * activities  (LineString)   - trips between places, with mode + distance
  * path_points (Point)        - the GPS-ish breadcrumb (timelinePath + rawSignals)

`path_points` is the layer that unifies with the historic google_location_history
GeoPackage (both are time-stamped point observations).

Pure stdlib - no third-party deps. Run on the desktop.

Usage:
    python convert_google_timeline.py INPUT.json [--outdir out] [--raw-signals]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# "40.0150000°, -105.2705000°"  ->  (lon, lat)   [GeoJSON order]
_LATLNG_RE = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*°?\s*,\s*(-?\d+(?:\.\d+)?)\s*°?"
)


def parse_latlng(s: str | None):
    """Parse Google's '<lat>°, <lon>°' string -> (lon, lat) or None."""
    if not s:
        return None
    m = _LATLNG_RE.search(s)
    if not m:
        return None
    lat = float(m.group(1))
    lon = float(m.group(2))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return [lon, lat]


def feature(geom_type, coords, props):
    return {
        "type": "Feature",
        "geometry": {"type": geom_type, "coordinates": coords},
        "properties": props,
    }


def convert(data: dict, include_raw: bool):
    visits, activities, path_points = [], [], []

    for seg in data.get("semanticSegments", []):
        start = seg.get("startTime")
        end = seg.get("endTime")

        if "visit" in seg:
            v = seg["visit"]
            cand = v.get("topCandidate", {})
            loc = parse_latlng(
                cand.get("placeLocation", {}).get("latLng")
            )
            if loc:
                visits.append(feature("Point", loc, {
                    "kind": "visit",
                    "start_time": start,
                    "end_time": end,
                    "place_id": cand.get("placeId"),
                    "semantic_type": cand.get("semanticType"),
                    "probability": v.get("probability"),
                }))

        if "activity" in seg:
            a = seg["activity"]
            p0 = parse_latlng(a.get("start", {}).get("latLng"))
            p1 = parse_latlng(a.get("end", {}).get("latLng"))
            if p0 and p1:
                activities.append(feature("LineString", [p0, p1], {
                    "kind": "activity",
                    "start_time": start,
                    "end_time": end,
                    "mode": a.get("topCandidate", {}).get("type"),
                    "distance_m": a.get("distanceMeters"),
                    "probability": a.get("probability"),
                }))

        for pt in seg.get("timelinePath", []):
            loc = parse_latlng(pt.get("point"))
            if loc:
                path_points.append(feature("Point", loc, {
                    "source": "timelinePath",
                    "time": pt.get("time"),
                }))

    if include_raw:
        for sig in data.get("rawSignals", []):
            pos = sig.get("position")
            if not pos:
                continue
            loc = parse_latlng(pos.get("LatLng") or pos.get("latLng"))
            if loc:
                path_points.append(feature("Point", loc, {
                    "source": "rawSignal",
                    "time": pos.get("timestamp"),
                    "accuracy_m": pos.get("accuracyMeters"),
                    "altitude_m": pos.get("altitudeMeters"),
                    "speed_mps": pos.get("speedMetersPerSecond"),
                    "gps_source": pos.get("source"),
                }))

    return visits, activities, path_points


def write_fc(features, path: Path):
    fc = {"type": "FeatureCollection", "features": features}
    path.write_text(json.dumps(fc), encoding="utf-8")
    print(f"  wrote {len(features):>7,} features -> {path}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input", type=Path)
    ap.add_argument("--outdir", type=Path, default=Path("out"))
    ap.add_argument(
        "--raw-signals", action="store_true",
        help="also include rawSignals[].position GPS fixes in path_points",
    )
    args = ap.parse_args(argv)

    print(f"reading {args.input} ...")
    data = json.loads(args.input.read_text(encoding="utf-8"))
    visits, activities, path_points = convert(data, args.raw_signals)

    args.outdir.mkdir(parents=True, exist_ok=True)
    write_fc(visits, args.outdir / "google_visits.geojson")
    write_fc(activities, args.outdir / "google_activities.geojson")
    write_fc(path_points, args.outdir / "google_path_points.geojson")
    print("done.")


if __name__ == "__main__":
    main()
