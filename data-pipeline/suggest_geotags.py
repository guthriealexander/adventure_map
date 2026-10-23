#!/usr/bin/env python3
"""Suggest GPS placements for un-geotagged photos by nearest-in-time match
against the timestamped track/point layers already in the pipeline.

This is a SUGGESTION engine only -- it never writes geotags itself. It writes
out/geotag_suggestions.geojson, a {photo_id: {lon,lat,delta_min,source,method}}
map that the web app's filmstrip reads to show a one-tap "GPS match" suggestion
next to each un-referenced photo; you still confirm or reject it there.

Two real point time-series are used for exact nearest-point matching:
  - Garmin legacy points        (out/garmin.geojson)            -- true UTC ("...Z")
  - Google Timeline path points (out/google_path_points.geojson) -- true UTC (explicit offset)
GPS activities (out/activities.geojsonl: any watch, or Garmin) only carry a start
time + duration (no per-vertex time), so they're matched by interpolating a
vertex proportionally across the activity's time window -- flagged as
method:"interp" in the output, since it's the track's time *resolution*, not
an actual GPS reading at that instant.

THE CATCH: camera EXIF timestamps carry no timezone -- it's just whatever the
camera's clock said. Garmin's `start` is also naive local time (presumably the
same zone as the camera), so those compare directly. But the Garmin-points and
Google-path sources are real UTC instants, so this script tries interpreting
the photo's naive timestamp as each of several US local timezones and keeps
whichever interpretation lines up best -- bounded by --max-delta-min so a
wrong-timezone guess just fails to find anything close, rather than silently
producing a confident wrong answer. Suggestions are for your review in the
filmstrip, not automatic placement, so a low false-positive rate matters more
than perfect recall.

Also skipped: untagged photos whose timestamp is a synthetic "Jan 1 of the
folder's year" placeholder (photos_ingest.py's fallback for scans with no EXIF
date at all) -- matching those against real GPS data would produce a
confident-looking but meaningless suggestion.

Usage:
    python suggest_geotags.py
    python suggest_geotags.py --max-delta-min 30
"""
from __future__ import annotations

import argparse
import bisect
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

OUT = Path("out")
SYNTHETIC_TS_RE = re.compile(r"^\d{4}-01-01T00:00:00$")
# Plausible US local-time offsets from UTC (hours west) used to interpret a
# naive EXIF timestamp when matching against true-UTC point sources. Covers
# continental US (4-8) through Alaska/Hawaii (9-10).
US_UTC_OFFSETS_H = [4, 5, 6, 7, 8, 9, 10]


def parse_utc(ts: str) -> datetime:
    """Parse an ISO timestamp that carries a timezone (Z or +HH:MM) -> aware UTC."""
    ts = ts.strip()
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_naive(ts: str):
    """Parse a naive local (no-tz) timestamp. None if missing/placeholder/synthetic."""
    ts = (ts or "").strip()
    if not ts or ts.startswith("0000-00-00") or SYNTHETIC_TS_RE.match(ts):
        return None
    try:
        return datetime.fromisoformat(ts.replace(" ", "T", 1))
    except ValueError:
        return None


class PointSeries:
    """A sorted-by-time list of (epoch_seconds, lon, lat) with nearest-time lookup."""

    def __init__(self, points):
        self.points = sorted(points, key=lambda p: p[0])
        self.epochs = [p[0] for p in self.points]

    def nearest(self, epoch):
        if not self.points:
            return None
        i = bisect.bisect_left(self.epochs, epoch)
        cands = [j for j in (i - 1, i) if 0 <= j < len(self.points)]
        if not cands:
            return None
        j = min(cands, key=lambda k: abs(self.epochs[k] - epoch))
        return abs(self.epochs[j] - epoch), self.points[j][1], self.points[j][2]


def load_point_source(path: Path) -> PointSeries:
    """Load a Point-geometry GeoJSON with a true-UTC `time` property."""
    if not path.exists():
        return PointSeries([])
    data = json.loads(path.read_text(encoding="utf-8"))
    pts = []
    for f in data.get("features", []):
        t = (f.get("properties") or {}).get("time")
        geom = f.get("geometry") or {}
        if not t or geom.get("type") != "Point":
            continue
        try:
            dt = parse_utc(t)
        except ValueError:
            continue
        lon, lat = geom["coordinates"][:2]
        pts.append((dt.timestamp(), lon, lat))
    return PointSeries(pts)


def load_tracks(path: Path):
    """Load Garmin activity LineStrings (JSONL) for proportional-time interpolation."""
    if not path.exists():
        return []
    tracks = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        f = json.loads(line)
        props = f.get("properties") or {}
        coords = (f.get("geometry") or {}).get("coordinates") or []
        start = parse_naive(props.get("start"))
        dur = props.get("duration_min")
        if not start or dur is None or len(coords) < 2:
            continue
        end = start + timedelta(minutes=float(dur))
        tracks.append({"name": props.get("name") or "track", "start": start, "end": end, "coords": coords})
    return tracks


def best_utc_match(naive_dt, series: PointSeries, label: str):
    """Try interpreting naive_dt as each plausible US local timezone, return
    the (delta_seconds, lon, lat, source_label) with the smallest gap."""
    best = None
    for off_h in US_UTC_OFFSETS_H:
        candidate_utc = naive_dt.replace(tzinfo=timezone.utc) + timedelta(hours=off_h)
        hit = series.nearest(candidate_utc.timestamp())
        if hit and (best is None or hit[0] < best[0]):
            best = hit
    if best is None:
        return None
    delta_s, lon, lat = best
    return delta_s, lon, lat, label


def best_track_match(naive_dt, tracks):
    """Photos whose timestamp falls within an activity's [start,end] window get
    a proportionally-interpolated vertex (nearest-vertex by elapsed fraction)."""
    best = None
    for tr in tracks:
        if not (tr["start"] <= naive_dt <= tr["end"]):
            continue
        span = (tr["end"] - tr["start"]).total_seconds()
        frac = 0.0 if span <= 0 else (naive_dt - tr["start"]).total_seconds() / span
        n = len(tr["coords"])
        idx = max(0, min(n - 1, round(frac * (n - 1))))
        lon, lat = tr["coords"][idx][:2]
        # resolution of this estimate = half the average inter-vertex time gap
        delta_s = (span / max(1, n - 1)) / 2
        if best is None or delta_s < best[0]:
            best = (delta_s, lon, lat, f'garmin_track:{tr["name"]}')
    return best


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--untagged", type=Path, default=OUT / "untagged.geojson")
    ap.add_argument("--garmin-points", type=Path, default=OUT / "garmin.geojson")
    ap.add_argument("--google-points", type=Path, default=OUT / "google_path_points.geojson")
    ap.add_argument("--garmin-tracks", "--activities", type=Path, default=OUT / "activities.geojsonl")
    ap.add_argument("--out", type=Path, default=OUT / "geotag_suggestions.geojson")
    ap.add_argument("--max-delta-min", type=float, default=20.0,
                     help="only suggest a match within this many minutes (default 20)")
    args = ap.parse_args()

    untagged = json.loads(args.untagged.read_text(encoding="utf-8"))
    garmin_pts = load_point_source(args.garmin_points)
    google_pts = load_point_source(args.google_points)
    tracks = load_tracks(args.garmin_tracks)
    print(f"loaded {len(garmin_pts.points):,} Garmin points, {len(google_pts.points):,} Google path points, "
          f"{len(tracks):,} Garmin tracks")

    max_delta_s = args.max_delta_min * 60
    out = {}
    by_source = {}
    skipped_no_ts = 0
    for f in untagged.get("features", []):
        p = f.get("properties") or {}
        pid = p.get("id")
        naive = parse_naive(p.get("timestamp"))
        if not pid or not naive:
            skipped_no_ts += 1
            continue

        candidates = [c for c in (
            best_utc_match(naive, garmin_pts, "garmin_points"),
            best_utc_match(naive, google_pts, "google_path"),
            best_track_match(naive, tracks),
        ) if c]
        if not candidates:
            continue

        delta_s, lon, lat, source = min(candidates, key=lambda c: c[0])
        if delta_s > max_delta_s:
            continue
        method = "interp" if source.startswith("garmin_track:") else "point"
        out[pid] = {"lon": round(lon, 6), "lat": round(lat, 6),
                    "delta_min": round(delta_s / 60, 1), "source": source, "method": method}
        key = source.split(":")[0]
        by_source[key] = by_source.get(key, 0) + 1

    args.out.write_text(json.dumps(out), encoding="utf-8")
    total = len(untagged.get("features", []))
    print(f"matched {len(out):,} / {total:,} untagged photos "
          f"(skipped {skipped_no_ts:,} with no usable real timestamp)")
    for src, n in sorted(by_source.items(), key=lambda x: -x[1]):
        print(f"  {src}: {n:,}")
    print(f"-> {args.out}")
    print("next:  python upload.py data    # publishes geotag_suggestions.geojson")


if __name__ == "__main__":
    main()
