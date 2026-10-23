#!/usr/bin/env python3
"""Pull GPS tracks from Garmin Connect -> GeoJSON LineStrings for the map.

Only activities that actually have a GPS track (hike, walk, run, ride, etc.) are
imported — anything without coordinates (treadmill, strength, etc.) is skipped.
Each activity becomes ONE LineString feature with name/type/time/distance props.
Incremental: a state file remembers which activity ids are already imported, so
re-runs only fetch what's new.

Auth uses the unofficial `garminconnect` library. Tokens are cached (via garth)
in ./.garmin so you only log in (and clear MFA) once; after that it's headless
and safe for Task Scheduler.

Credentials come from environment or a local `.garmin.env` (gitignored):
    GARMIN_EMAIL=you@example.com
    GARMIN_PASSWORD=...

Usage:
    pip install garminconnect garth
    python garmin_sync.py login          # ONE TIME, interactive (handles MFA)
    python garmin_sync.py sync           # import all new GPS activities
    python garmin_sync.py sync --limit 50    # cap new imports this run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOKENSTORE = HERE / ".garmin"
OUT = HERE / "out" / "garmin_tracks.geojsonl"
STATE = HERE / "garmin_state.json"
MAX_POINTS = 2000   # downsample very long tracks so the tile stays light


def load_env():
    envf = HERE / ".garmin.env"
    if envf.exists():
        for line in envf.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def connect(interactive: bool):
    """Return a logged-in Garmin client, reusing cached tokens when possible."""
    from garminconnect import Garmin
    g = Garmin()
    try:
        g.login(str(TOKENSTORE))   # uses cached tokens if present
        return g
    except Exception:
        pass
    load_env()
    email = os.environ.get("GARMIN_EMAIL")
    pw = os.environ.get("GARMIN_PASSWORD")
    if not email or not pw:
        sys.exit("No cached login and no GARMIN_EMAIL/GARMIN_PASSWORD "
                 "(set them in .garmin.env). Run `garmin_sync.py login` once.")
    if not interactive:
        sys.exit("Login token missing/expired and this is a non-interactive run. "
                 "Run `python garmin_sync.py login` once to refresh it.")
    prompt = lambda: input("Garmin MFA code (blank if none): ").strip()
    g = Garmin(email, pw, prompt_mfa=prompt)
    try:
        g.login()
    except Exception as e:
        if "429" in str(e) or "rate" in str(e).lower():
            sys.exit("\nGarmin is rate-limiting login from this IP (HTTP 429).\n"
                     "Wait ~30-60 min (don't retry repeatedly — it extends the block), "
                     "then run `python garmin_sync.py login` again.")
        raise
    TOKENSTORE.mkdir(parents=True, exist_ok=True)
    g.client.dump(str(TOKENSTORE))   # persist the session (garth Client) for reuse
    print(f"  login OK, tokens cached -> {TOKENSTORE}")
    return g


def cmd_login(args):
    connect(interactive=True)
    print("Garmin login complete. Future `sync` runs are headless.")


# --- GPX -> coordinates -----------------------------------------------------
def parse_gpx(data: bytes):
    """Return a list of [lon, lat] from GPX track points (namespace-agnostic)."""
    coords = []
    root = ET.fromstring(data)
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] == "trkpt":
            lat = el.get("lat"); lon = el.get("lon")
            if lat and lon:
                coords.append([float(lon), float(lat)])
    return coords


def downsample(coords, cap=MAX_POINTS):
    if len(coords) <= cap:
        return coords
    step = len(coords) // cap + 1
    out = coords[::step]
    if out[-1] != coords[-1]:
        out.append(coords[-1])   # always keep the finish point
    return out


def has_gps(act: dict) -> bool:
    return act.get("startLatitude") is not None or bool(act.get("hasPolyline"))


def cmd_sync(args):
    from garminconnect import Garmin
    g = connect(interactive=args.interactive)

    state = json.loads(STATE.read_text()) if STATE.exists() else {"imported": []}
    imported = set(str(i) for i in state.get("imported", []))
    OUT.parent.mkdir(parents=True, exist_ok=True)

    added = skipped_known = skipped_nogps = errors = 0
    start, page = 0, 50
    with OUT.open("a", encoding="utf-8") as fh:
        while True:
            batch = g.get_activities(start, page)
            if not batch:
                break
            for act in batch:
                aid = str(act.get("activityId"))
                if aid in imported:
                    skipped_known += 1
                    continue
                if not has_gps(act):
                    skipped_nogps += 1
                    imported.add(aid)   # remember so we don't re-check it
                    continue
                try:
                    gpx = g.download_activity(aid, dl_fmt=Garmin.ActivityDownloadFormat.GPX)
                    coords = downsample(parse_gpx(gpx))
                except Exception as e:
                    errors += 1
                    print(f"  ! {aid}: {e}", file=sys.stderr)
                    if "429" in str(e):   # backed off by Garmin — pause longer
                        time.sleep(5)
                    continue
                time.sleep(args.sleep)   # be polite so we don't trip rate limiting
                if len(coords) < 2:
                    skipped_nogps += 1
                    imported.add(aid)
                    continue
                feat = {
                    "type": "Feature",
                    "geometry": {"type": "LineString", "coordinates": coords},
                    "properties": {
                        "activityId": aid,
                        "name": act.get("activityName") or "",
                        "type": (act.get("activityType") or {}).get("typeKey") or "",
                        "start": act.get("startTimeLocal") or act.get("startTimeGMT") or "",
                        "distance_km": round((act.get("distance") or 0) / 1000, 2),
                        "duration_min": round((act.get("duration") or 0) / 60, 1),
                    },
                }
                fh.write(json.dumps(feat) + "\n")
                fh.flush()
                imported.add(aid)
                added += 1
                if added % 25 == 0:
                    print(f"  imported {added} tracks...", flush=True)
                if args.limit and added >= args.limit:
                    break
            else:
                start += page
                continue
            break   # inner loop hit the --limit cap

    state["imported"] = sorted(imported, key=lambda x: int(x) if x.isdigit() else 0)
    STATE.write_text(json.dumps(state))
    print(f"garmin sync: +{added} new tracks, {skipped_known} already had, "
          f"{skipped_nogps} no-GPS, {errors} errors | total imported "
          f"{len(imported):,} -> {OUT}")
    # exit code 0 always; auto_import.py reads the '+N' from stdout


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    lo = sub.add_parser("login", help="one-time interactive login (handles MFA)")
    lo.set_defaults(func=cmd_login)

    sy = sub.add_parser("sync", help="import new GPS activities as LineStrings")
    sy.add_argument("--limit", type=int, default=0, help="max new tracks this run (0=all)")
    sy.add_argument("--sleep", type=float, default=0.7,
                    help="seconds to wait between track downloads (rate-limit safety)")
    sy.add_argument("--interactive", action="store_true",
                    help="allow an MFA prompt if the token is missing/expired")
    sy.set_defaults(func=cmd_sync)

    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
