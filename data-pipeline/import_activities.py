#!/usr/bin/env python3
"""Import GPS activities from a folder of FIT, GPX and TCX files.

Any watch or app can export these: Coros, Garmin, Suunto, Polar, Wahoo, Apple Watch
(through an export app), Strava's "Export original", and most phone tracking apps.
Drop the files (or .gz copies) into the activities folder and run this, or let
auto_import.py run it every night. Each file becomes one line in
out/activities.geojsonl, the same format garmin_sync.py writes, so trips, map
matching and the Activities layer treat them all alike.

The folder is AM_ACTIVITIES_DIR in site.env, or data-pipeline/activities/ by default.
Files already imported are skipped (they are recognised by their content, so renaming
or moving a file doesn't import it twice).

    python import_activities.py            # import new files
    python import_activities.py --dir D:\\exports\\coros

FIT files need the fitdecode package (in requirements.txt); GPX and TCX need nothing
extra.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "out" / "activities.geojsonl"
SEMI = 180 / 2 ** 31                       # FIT positions are in semicircles


def load_site_env():
    f = HERE / "site.env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def read_bytes(path: Path) -> bytes:
    data = path.read_bytes()
    return gzip.decompress(data) if path.suffix.lower() == ".gz" else data


def kind_of(path: Path) -> str:
    name = path.name.lower()
    if name.endswith(".gz"):
        name = name[:-3]
    return name.rsplit(".", 1)[-1]


def iso(t) -> str:
    if isinstance(t, datetime):
        if t.tzinfo:
            t = t.astimezone(timezone.utc).replace(tzinfo=None)
        return t.isoformat(timespec="seconds")
    return str(t or "")


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_gpx(data: bytes):
    root = ET.fromstring(data)
    name = sport = ""
    pts, start = [], ""
    for el in root.iter():
        tag = local_name(el.tag)
        if tag == "trk":
            for ch in el:
                t = local_name(ch.tag)
                if t == "name" and not name:
                    name = (ch.text or "").strip()
                elif t == "type" and not sport:
                    sport = (ch.text or "").strip()
        elif tag in ("trkpt", "rtept"):
            lat, lon = el.get("lat"), el.get("lon")
            if lat is None or lon is None:
                continue
            pts.append([round(float(lon), 6), round(float(lat), 6)])
            if not start:
                for ch in el:
                    if local_name(ch.tag) == "time":
                        start = (ch.text or "").strip()
    return pts, name, sport, start


def parse_tcx(data: bytes):
    root = ET.fromstring(data)
    pts, sport, start = [], "", ""
    for el in root.iter():
        tag = local_name(el.tag)
        if tag == "Activity" and not sport:
            sport = el.get("Sport") or ""
        elif tag == "Trackpoint":
            lat = lon = t = None
            for ch in el.iter():
                ct = local_name(ch.tag)
                if ct == "LatitudeDegrees":
                    lat = float(ch.text)
                elif ct == "LongitudeDegrees":
                    lon = float(ch.text)
                elif ct == "Time":
                    t = (ch.text or "").strip()
            if lat is not None and lon is not None:
                pts.append([round(lon, 6), round(lat, 6)])
                if not start and t:
                    start = t
    return pts, "", sport, start


def parse_fit(data: bytes):
    try:
        import fitdecode
    except ImportError:
        raise SystemExit("FIT files need the fitdecode package: pip install fitdecode")
    import io
    pts, sport, start, device = [], "", "", ""
    with fitdecode.FitReader(io.BytesIO(data)) as fit:
        for frame in fit:
            if not isinstance(frame, fitdecode.FitDataMessage):
                continue
            if frame.name == "record":
                lat = frame.get_value("position_lat", fallback=None)
                lon = frame.get_value("position_long", fallback=None)
                if lat is not None and lon is not None:
                    pts.append([round(lon * SEMI, 6), round(lat * SEMI, 6)])
                    if not start:
                        start = iso(frame.get_value("timestamp", fallback=""))
            elif frame.name == "session" and not sport:
                sport = str(frame.get_value("sport", fallback="") or "")
            elif frame.name == "file_id" and not device:
                device = str(frame.get_value("manufacturer", fallback="") or "")
    return pts, "", sport, start, device


def km(pts) -> float:
    total = 0.0
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        kx = 111.32 * math.cos(math.radians((y1 + y2) / 2))
        total += math.hypot((x2 - x1) * kx, (y2 - y1) * 110.57)
    return round(total, 2)


def main():
    load_site_env()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=os.environ.get("AM_ACTIVITIES_DIR") or str(HERE / "activities"))
    args = ap.parse_args()
    folder = Path(args.dir)
    if not folder.exists():
        print(f"activities: +0 new (no folder at {folder})")
        return
    OUT.parent.mkdir(exist_ok=True)
    seen = set()
    if OUT.exists():
        for line in OUT.open(encoding="utf-8"):
            try:
                seen.add(str(json.loads(line)["properties"]["activityId"]))
            except Exception:
                pass
    added = skipped = failed = 0
    with OUT.open("a", encoding="utf-8") as fh:
        for path in sorted(folder.rglob("*")):
            if not path.is_file() or kind_of(path) not in ("fit", "gpx", "tcx"):
                continue
            try:
                data = read_bytes(path)
            except OSError as e:
                print(f"  could not read {path.name}: {e}")
                failed += 1
                continue
            aid = "file-" + hashlib.sha1(data).hexdigest()[:16]
            if aid in seen:
                skipped += 1
                continue
            try:
                k = kind_of(path)
                if k == "fit":
                    pts, name, sport, start, device = parse_fit(data)
                else:
                    pts, name, sport, start = parse_gpx(data) if k == "gpx" else parse_tcx(data)
                    device = ""
            except SystemExit:
                raise
            except Exception as e:
                print(f"  could not parse {path.name}: {e}")
                failed += 1
                continue
            seen.add(aid)
            if len(pts) < 2:
                skipped += 1                       # no GPS (treadmill, pool, indoor)
                continue
            props = {"activityId": aid, "source": device or "file", "name": name or path.stem,
                     "type": sport.lower(), "start": start, "distance_km": km(pts), "file": path.name}
            fh.write(json.dumps({"type": "Feature", "geometry": {"type": "LineString", "coordinates": pts},
                                 "properties": props}) + "\n")
            added += 1
    print(f"activities: +{added} new, {skipped} already imported or without GPS, {failed} failed -> {OUT.name}")


if __name__ == "__main__":
    sys.exit(main())
