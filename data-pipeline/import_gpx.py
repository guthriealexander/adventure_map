#!/usr/bin/env python3
"""Import GPX tracks (from any app or device) into the Hikes layer.

Each <trk> in each .gpx file becomes one line of out/hikes.geojsonl: a MultiLineString
(one part per <trkseg>) with its name, type "hike" and, when the points carry times,
a `begin` timestamp - build_trips.py uses that to put the track in the right trip.
Re-running skips tracks already imported (same file + track + first point time).

    python import_gpx.py path/to/gpx-folder          # dry run: list what would be added
    python import_gpx.py path/to/gpx-folder --write
    python build_pmtiles.py hikes                     # then rebuild the layer

Optional, afterwards: snap_regions.py matches the tracks to the OpenStreetMap trail
and road network (the cleaner hikes_snapped layer).
"""
from __future__ import annotations

import argparse
import json
import math
import xml.etree.ElementTree as ET
from pathlib import Path

OUT = Path(__file__).resolve().parent / "out" / "hikes.geojsonl"
MIN_KM = 0.3


def km(a, b):
    p = math.pi / 180
    h = (math.sin((b[1] - a[1]) * p / 2) ** 2
         + math.cos(a[1] * p) * math.cos(b[1] * p) * math.sin((b[0] - a[0]) * p / 2) ** 2)
    return 12742 * math.asin(math.sqrt(min(1.0, h)))


def tracks(path: Path):
    root = ET.parse(path).getroot()
    ns = root.tag.split("}")[0][1:] if root.tag.startswith("{") else ""
    q = (lambda t: f"{{{ns}}}{t}") if ns else (lambda t: t)
    for n, trk in enumerate(root.iter(q("trk"))):
        name = trk.findtext(q("name")) or path.stem
        parts, first = [], None
        for seg in trk.iter(q("trkseg")):
            pts = []
            for p in seg.iter(q("trkpt")):
                pts.append([round(float(p.get("lon")), 6), round(float(p.get("lat")), 6)])
                t = p.findtext(q("time"))
                if t and first is None:
                    first = t.replace("Z", "")[:19]
            if len(pts) >= 2:
                parts.append(pts)
        if parts:
            yield n, name, first, parts


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", type=Path)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    have = set()
    if OUT.exists():
        for line in OUT.open(encoding="utf-8"):
            key = (json.loads(line).get("properties") or {}).get("gpx_key")
            if key:
                have.add(key)
    new = []
    for f in sorted(args.folder.rglob("*.gpx")):
        for n, name, begin, parts in tracks(f):
            key = f"{f.name}#{n}#{begin}"
            dist = sum(km(a, b) for p in parts for a, b in zip(p, p[1:]))
            if key in have or dist < MIN_KM:
                continue
            props = {"name": name, "type": "hike", "source": "gpx", "gpx_key": key}
            if begin:
                props["begin"] = begin
            new.append({"type": "Feature", "properties": props,
                        "geometry": {"type": "MultiLineString", "coordinates": parts}})
            print(f"  ADD {name!r}: {dist:.1f} km, {begin or 'no date'}  ({f.name})")
    print(f"{len(new)} track(s) to add")
    if args.write and new:
        OUT.parent.mkdir(exist_ok=True)
        with OUT.open("a", encoding="utf-8") as fh:
            for ft in new:
                fh.write(json.dumps(ft) + "\n")
        print(f"appended to {OUT} - now run: python build_pmtiles.py hikes")


if __name__ == "__main__":
    main()
