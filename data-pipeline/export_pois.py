#!/usr/bin/env python3
"""Export the editable POIs from Cloudflare D1 to GeoJSON (and keep a SQL backup).

POIs are the one live/editable layer — they live in D1 (SQLite), not in the
PMTiles. This pulls the current rows down so you can open them in QGIS, archive
them, or re-seed the database.

Usage (run from the data-pipeline folder; needs a cached `wrangler login`):
    python export_pois.py                       # -> out/pois.geojson + out/pois_backup.sql
    python export_pois.py --out out/pois.geojson
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
WEB = HERE.parent / "web"          # wrangler.toml (with the D1 binding) lives here
DB = "adventure-map"
NPX = "npx.cmd" if sys.platform == "win32" else "npx"


def _wenv():
    """Env that makes wrangler non-interactive (auto-confirm prompts) so it never
    blocks waiting for input under Task Scheduler / a headless run."""
    e = os.environ.copy()
    e["CI"] = "1"
    e["WRANGLER_SEND_METRICS"] = "false"
    return e


def _wrangler(args, timeout):
    """Run a wrangler command with closed stdin + a timeout so it can't hang."""
    return subprocess.run([NPX, "wrangler", *args], cwd=str(WEB),
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          stdin=subprocess.DEVNULL, env=_wenv(), timeout=timeout)


def wrangler_json(sql: str, attempts: int = 3):
    """Run a read query against the remote D1 and return the result rows.

    Retries with backoff: `wrangler d1 execute --remote` occasionally fails with a
    transient network/auth blip (this is what broke the nightly POI backup). On the
    final failure we surface BOTH streams — wrangler writes its error to stdout, not
    stderr, so the old stderr-only message looked empty and undiagnosable."""
    last = None
    for i in range(attempts):
        r = _wrangler(["d1", "execute", DB, "--remote", "--json", "--command", sql], timeout=120)
        last = r
        if r.returncode == 0:
            try:
                # wrangler returns a list of result sets; rows are under [0]["results"]
                return json.loads(r.stdout)[0]["results"]
            except (json.JSONDecodeError, KeyError, IndexError):
                pass  # malformed/partial output — treat as a failed attempt, retry
        if i < attempts - 1:
            time.sleep(3 * (i + 1))
    detail = ((last.stdout or "") + "\n" + (last.stderr or "")).strip() if last else "(no output)"
    sys.exit(f"wrangler d1 query failed after {attempts} attempts:\n{detail}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=HERE / "out" / "pois.geojson")
    ap.add_argument("--sql", type=Path, default=HERE / "out" / "pois_backup.sql")
    args = ap.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)

    rows = wrangler_json("SELECT id,name,type,lon,lat,note,created_at,updated_at "
                         "FROM pois ORDER BY id;")
    feats = []
    for r in rows:
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
            "properties": {k: r[k] for k in
                           ("id", "name", "type", "note", "created_at", "updated_at")},
        })
    fc = {"type": "FeatureCollection", "features": feats}
    args.out.write_text(json.dumps(fc, indent=2), encoding="utf-8")
    print(f"wrote {len(feats)} POIs -> {args.out}")

    # also keep a raw SQL dump (full backup, re-importable with `wrangler d1 execute`)
    try:
        r = _wrangler(["d1", "export", DB, "--remote", "--output", str(args.sql)], timeout=300)
        if r.returncode == 0:
            print(f"SQL backup -> {args.sql}")
        else:
            detail = ((r.stdout or "") + " " + (r.stderr or "")).strip()[:300]
            print(f"  ! SQL backup skipped (d1 export failed): {detail}", file=sys.stderr)
    except subprocess.TimeoutExpired:
        print("  ! SQL backup skipped (d1 export timed out)", file=sys.stderr)


if __name__ == "__main__":
    main()
