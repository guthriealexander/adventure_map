#!/usr/bin/env python3
"""Incremental Adventure Map import — meant to be run on a schedule.

Runs the full photo pipeline end to end, doing only the work that's new:

  1. scan    the photo folder (AM_PHOTO_DIR in site.env; default: the OneDrive
             Camera Roll) for new geotagged photos
             (dedupes against the manifest; makes derivatives inline)
  2. derivs  safety net — regenerate any missing thumb/web/pano (skips existing)
  3. upload  push only the photo derivatives not yet on R2 (skip-existing)
  4. tile    rebuild photos.pmtiles from the full manifest, upload it
  5. deploy  publish the Pages site
  6. verify  confirm Cloudflare Access still gates the site

If the scan adds zero new photos, steps 4–5 are skipped (nothing changed), but
the gate is always re-verified. Everything is idempotent, so a failed/interrupted
run is safe to re-run.

Usage:
    python auto_import.py                      # full run
    python auto_import.py --dir "D:\\Photos"    # scan a different folder
    python auto_import.py --no-deploy          # build + upload, skip the deploy
    python auto_import.py --skip-scan          # just upload/tile/deploy what's local

Schedule it (Windows Task Scheduler), e.g. daily at 02:00 — see register_task.ps1.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent           # .../adventure-map/data-pipeline
ROOT = HERE.parent                                # .../adventure-map
WEB = ROOT / "web"
MANIFEST = HERE / "out" / "photos.geojson"
DERIVS = HERE / "derivatives"


def _load_site_env():
    """site.env (next to this script; see site.env.example): AM_SITE_URL, the deployed
    site, for the privacy gate check; AM_PAGES_PROJECT, the Cloudflare Pages project;
    AM_PHOTO_DIR, the photo folder the nightly scan reads. Real env vars win."""
    f = HERE / "site.env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load_site_env()
HOST = os.environ.get("AM_SITE_URL", "").rstrip("/")
PAGES_PROJECT = os.environ.get("AM_PAGES_PROJECT", "adventure-map")
# Photos: AM_PHOTO_DIR, else the OneDrive Camera Roll (%OneDrive%, else derived from
# this file's location). Never hard-code a drive letter.
ONEDRIVE = Path(os.environ.get("OneDrive") or HERE.parents[3])
DEFAULT_DIR = os.environ.get("AM_PHOTO_DIR") or str(ONEDRIVE / "Pictures" / "Camera Roll")
PY = sys.executable


def log(msg: str):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def run(cmd, **kw):
    """Run a command in the pipeline dir, streaming output. Returns CompletedProcess."""
    log("$ " + " ".join(str(c) for c in cmd))
    kw.setdefault("encoding", "utf-8")   # decode wrangler/tool output as UTF-8,
    kw.setdefault("errors", "replace")   # not Windows cp1252 (avoids decode crashes)
    return subprocess.run(cmd, cwd=str(HERE), **kw)


def scan(photo_dir: str) -> int:
    """Scan a folder for new photos. Returns the number added to the manifest."""
    r = run([PY, "photos_ingest.py", "scan",
             "--dir", photo_dir,
             "--manifest", str(MANIFEST),
             "--outdir", str(DERIVS),
             "--no-derivs"],   # fast EXIF pass; parallel derivs runs next
            capture_output=True, text=True)
    sys.stdout.write(r.stdout)
    if r.stderr:
        sys.stderr.write(r.stderr)
    m = re.search(r"added=([\d,]+)", r.stdout)
    return int(m.group(1).replace(",", "")) if m else 0


def backup_pois():
    """Pull the editable POIs out of D1 to GeoJSON + SQL (off-cloud copy).

    POIs change via the web app, not the pipeline, so this runs every night
    regardless of whether photos/tracks changed. Non-fatal on failure."""
    if not (HERE / "export_pois.py").exists():
        return
    try:
        r = run([PY, "export_pois.py"], capture_output=True, text=True, timeout=420)
    except subprocess.TimeoutExpired:
        log("WARNING: POI backup timed out — skipping (does not block the rest of the run)")
        return
    sys.stdout.write(r.stdout or "")
    if r.returncode != 0:
        log("WARNING: POI backup failed (wrangler logged in?)")
        if r.stderr:
            sys.stderr.write(r.stderr)


def tag_photos():
    """Tag photos added since the last run (tag_photos.py new). Non-fatal: with no API
    key or on an error the photos simply stay untagged until the next run. If any were
    flagged private, delete R2 copies just in case (upload skips them anyway)."""
    if not (HERE / "tag_photos.py").exists():
        return
    r = run([PY, "tag_photos.py", "new"], capture_output=True, text=True)
    sys.stdout.write(r.stdout or "")
    if r.returncode != 0:
        log("WARNING: photo tagging skipped (Anthropic key missing or API error)")
        if r.stderr:
            sys.stderr.write(r.stderr[-2000:])
        return
    m = re.search(r"newly private=(\d+)", r.stdout or "")
    if m and int(m.group(1)):
        log(f"{m.group(1)} new photo(s) flagged private - kept off the map")
        run([PY, "upload_r2.py", "purge-private"])


def garmin_sync() -> int:
    """Pull new Garmin GPS tracks. Returns number of new tracks (0 on any issue).

    Non-interactive: if the cached login is missing/expired it just warns and
    returns 0 (run `python garmin_sync.py login` once to (re)authorize)."""
    if not (HERE / "garmin_sync.py").exists():
        return 0
    r = run([PY, "garmin_sync.py", "sync"], capture_output=True, text=True)
    sys.stdout.write(r.stdout)
    if r.stderr:
        sys.stderr.write(r.stderr)
    if r.returncode != 0:
        log("WARNING: garmin sync skipped (login token missing/expired?)")
        return 0
    m = re.search(r"\+(\d+) new tracks", r.stdout)
    return int(m.group(1)) if m else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", default=DEFAULT_DIR, help="photo folder to scan")
    ap.add_argument("--skip-scan", action="store_true")
    ap.add_argument("--no-garmin", action="store_true", help="skip the Garmin Connect sync")
    ap.add_argument("--no-deploy", action="store_true")
    ap.add_argument("--force-tile", action="store_true",
                    help="rebuild + deploy even if the scan added nothing")
    args = ap.parse_args()

    t0 = time.time()
    log("=== Adventure Map auto-import starting ===")

    added = garmin_added = 0
    if args.skip_scan:
        log("skipping scan (--skip-scan)")
        args.force_tile = True
    else:
        added = scan(args.dir)
        log(f"scan added {added:,} new photo(s)")

    if not args.no_garmin:
        garmin_added = garmin_sync()
        log(f"garmin added {garmin_added:,} new track(s)")

    # always snapshot the editable POIs (they change via the web app, not here)
    backup_pois()

    # places recognised in new no-GPS photos (geotag_ai.py skips ones already done),
    # then publish the geotag gallery's catalog + suggestions
    if (HERE / "geotag_ai.py").exists():
        r = run([PY, "geotag_ai.py"], capture_output=True, text=True)
        sys.stdout.write((r.stdout or "")[-600:])
        if r.returncode == 0:
            run([PY, "upload_r2.py", "suggestions"])
        else:
            log("WARNING: location suggestions skipped (Anthropic key missing or API error)")

    if added == 0 and garmin_added == 0 and not args.force_tile:
        log("nothing new — verifying gate and exiting")
        verify_gate()
        log(f"=== done in {time.time()-t0:.0f}s (no changes) ===")
        return

    # 2. photos: derivatives + upload + tile (only if photos changed)
    if added or args.force_tile:
        # safety-net derivatives (parallel; skips ones already made by the scan)
        run([PY, "photos_ingest.py", "derivs",
             "--manifest", str(MANIFEST), "--outdir", str(DERIVS)])
        # caption + tag the new photos (Claude) BEFORE anything is published: ones it
        # flags sensitive go on private_photos.txt, which the upload and tiles skip
        tag_photos()
        # upload only the derivatives missing from R2
        run([PY, "upload_r2.py", "photos"])
        run([PY, "build_pmtiles.py", "photos"])

    # 3. garmin: rebuild the tracks tile if there are new tracks
    if garmin_added or args.force_tile:
        run([PY, "build_pmtiles.py", "garmin_tracks"])

    # 4. upload whatever tiles changed
    run([PY, "upload_r2.py", "tiles"])

    # 4b. trips panel: re-detect trips, name new/changed ones (Claude), publish trips.json
    run([PY, "build_trips.py"])
    run([PY, "name_tracks.py"])          # OSM-based names for new trip tracks
    run([PY, "build_trips.py"])          # ...folded into trips.json
    run([PY, "name_trips.py"])
    run([PY, "upload_r2.py", "trips"])

    # 5. deploy
    if args.no_deploy:
        log("skipping deploy (--no-deploy)")
    else:
        deploy()

    # 6. always confirm the gate is closed
    verify_gate()
    log(f"=== done in {time.time()-t0:.0f}s "
        f"({added:,} new photos, {garmin_added:,} new tracks) ===")


def deploy():
    """Publish the Pages site. Requires a cached wrangler login on this machine.

    IMPORTANT: must run from web/ (no path arg) so wrangler reads web/wrangler.toml
    and bundles the Functions WITH their R2 (MEDIA) + D1 (DB) bindings. Deploying
    with a path from the repo root skips wrangler.toml and ships functions with no
    bindings -> /media/* and /api/* return HTML errors and the map shows no data.
    """
    # Review hold: while out/.hold_deploy exists, the nightly run imports and tiles as
    # usual but never publishes, so work-in-progress in web/ can't ship by accident.
    # Delete the file (or `python -c "import auto_import as a; a.deploy()"`) to release.
    hold = HERE / "out" / ".hold_deploy"
    if hold.exists():
        note = hold.read_text(encoding="utf-8", errors="replace").strip()
        log(f"deploy HELD by out/.hold_deploy{' — ' + note if note else ''}")
        return

    # fingerprint JS/CSS links so browsers pick up new code without a hard refresh
    try:
        from stamp_assets import stamp
        changed = stamp()
        log("stamped assets: " + (", ".join(changed) if changed else "no changes"))
    except Exception as e:
        log(f"WARNING: asset stamping failed ({e}); deploying anyway")

    npx = "npx.cmd" if sys.platform == "win32" else "npx"
    env = os.environ.copy()
    env["CI"] = "1"                       # non-interactive: never block on a prompt
    env["WRANGLER_SEND_METRICS"] = "false"
    try:
        r = subprocess.run([npx, "wrangler", "pages", "deploy", "--project-name", PAGES_PROJECT],
                           cwd=str(WEB), stdin=subprocess.DEVNULL, env=env, timeout=600,
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        log("WARNING: deploy timed out")
        return
    out = (r.stdout or "") + (r.stderr or "")
    sys.stdout.write(out)
    # wrangler can print an auth ERROR without a failing exit status, so require
    # its explicit success line rather than trusting the return code alone
    if r.returncode != 0 or "Deployment complete" not in out:
        hint = " (auth: set CLOUDFLARE_API_TOKEN)" if "CLOUDFLARE_API_TOKEN" in out else ""
        log(f"WARNING: deploy did NOT complete{hint}")


# Every deploy also publishes a preview copy at <hash>.<project>.pages.dev that shares
# the same D1/R2 data. An Access app on the bare hostname alone does NOT cover those,
# so the gate check probes a preview-style subdomain too - a random one exercises the
# "*." wildcard rule that must cover them.
GATE_HOSTS = ([HOST] + ([HOST.replace("://", "://gatecheck-probe.", 1)] if HOST.endswith(".pages.dev") else [])
              if HOST else [])
# the data lives behind the Functions, so check those paths, not just the page
GATE_PATHS = ["/", "/api/pois", "/api/geotags", "/media/tiles/photos.pmtiles"]


def verify_gate():
    """Privacy guard: the site must never serve content unauthenticated — on the
    main hostname OR any preview-deployment hostname.
    OK = a redirect to Cloudflare Access, or a 401/403 (forbidden). The only true
    alarm is a 200, which would mean content is being served without login."""
    if not GATE_HOSTS:
        log("gate check skipped: set AM_SITE_URL in site.env")
        return
    opener = urllib.request.build_opener(_NoRedirect())
    ok = alerts = 0
    for host in GATE_HOSTS:
        for path in GATE_PATHS:
            url = host + path
            # Browser-like UA — Cloudflare may bot-block a bare urllib UA with a 403,
            # which would otherwise look like (but isn't) an exposure.
            req = urllib.request.Request(url, method="GET",
                headers={"User-Agent": "Mozilla/5.0 (gate-check) AppleWebKit/537.36"})
            try:
                resp = opener.open(req, timeout=20)
                code, loc = resp.getcode(), resp.headers.get("location", "")
            except urllib.error.HTTPError as e:
                code, loc = e.code, e.headers.get("location", "")
            except Exception as e:
                log(f"WARNING: gate check could not reach {url}: {e}")
                continue
            if (code in (301, 302, 303, 307, 308) and "cloudflareaccess.com" in loc) \
                    or code in (401, 403):
                ok += 1
            elif code == 200:
                alerts += 1
                log(f"!!! PRIVACY ALERT: {url} returned 200 unauthenticated — content is "
                    "being served WITHOUT login. Check Cloudflare Access immediately.")
            else:
                log(f"WARNING: unexpected gate status {code} for {url} "
                    f"(location={loc!r}); verify manually.")
    total = len(GATE_HOSTS) * len(GATE_PATHS)
    if alerts:
        log(f"!!! gate FAILED: {alerts} of {total} URLs served without login")
    else:
        log(f"gate OK: {ok}/{total} URLs redirect to Cloudflare Access "
            "(main + preview hostnames)")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None  # surface the 302 instead of following it


if __name__ == "__main__":
    main()
