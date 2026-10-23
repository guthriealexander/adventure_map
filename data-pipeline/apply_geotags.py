#!/usr/bin/env python3
"""Apply in-app geotags back to the original photos.

After you place un-geotagged photos in the web app (saved to D1), this:
  1. pulls the 'placed' geotags from D1,
  2. writes GPS into each original file's EXIF (piexif; JPEG/TIFF),
  3. folds the photo into the geotagged manifest so it appears on the map as a
     normal photo (its thumbnail is already on R2 from the catalog step),
  4. removes it from the untagged catalog and marks it 'applied' in D1,
  5. rebuilds + uploads photos.pmtiles and deploys (unless --no-deploy).

Usage (from data-pipeline/, needs a cached wrangler login):
    pip install piexif
    python apply_geotags.py                 # apply all placed, rebuild + deploy
    python apply_geotags.py --dry-run        # show what would happen, write nothing
    python apply_geotags.py --no-deploy      # write EXIF + manifests, skip deploy
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from auto_import import deploy, verify_gate, run, log, PY, WEB

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "out" / "photos.geojson"
MANIFEST_SRC = MANIFEST.with_suffix(".src.json")
UNTAGGED = HERE / "out" / "untagged.geojson"
UNTAGGED_SRC = UNTAGGED.with_suffix(".src.json")
DB = "adventure-map"


def d1(sql, json_out=False):
    import os
    npx = "npx.cmd" if sys.platform == "win32" else "npx"
    cmd = [npx, "wrangler", "d1", "execute", DB, "--remote", "--command", sql]
    if json_out:
        cmd.append("--json")
    env = os.environ.copy()
    env["CI"] = "1"                       # non-interactive: auto-confirm, never hang on a prompt
    env["WRANGLER_SEND_METRICS"] = "false"
    r = subprocess.run(cmd, cwd=str(WEB), capture_output=True, text=True,
                       encoding="utf-8", errors="replace",   # avoid Windows cp1252 decode errors
                       stdin=subprocess.DEVNULL, env=env, timeout=180)
    if r.returncode != 0:
        sys.exit(f"wrangler d1 failed:\n{r.stderr}")
    return json.loads(r.stdout) if json_out else None


def placed_geotags():
    data = d1("SELECT id,lon,lat FROM photo_geotags WHERE status='placed';", json_out=True)
    return data[0]["results"]


def _dms(deg):
    deg = abs(deg)
    d = int(deg)
    m = int((deg - d) * 60)
    s = (deg - d - m / 60) * 3600
    return ((d, 1), (m, 1), (int(round(s * 10000)), 10000))


def write_exif_gps(path: str, lon: float, lat: float) -> bool:
    """Write GPS into the file's EXIF in place. Returns True on success."""
    import piexif
    try:
        try:
            exif = piexif.load(path)
        except Exception:
            exif = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}
        exif["GPS"] = {
            piexif.GPSIFD.GPSLatitudeRef: "N" if lat >= 0 else "S",
            piexif.GPSIFD.GPSLatitude: _dms(lat),
            piexif.GPSIFD.GPSLongitudeRef: "E" if lon >= 0 else "W",
            piexif.GPSIFD.GPSLongitude: _dms(lon),
        }
        piexif.insert(piexif.dump(exif), path)
        return True
    except Exception as e:
        print(f"  ! EXIF write failed for {Path(path).name}: {e}", file=sys.stderr)
        return False


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-deploy", action="store_true")
    args = ap.parse_args()

    rows = placed_geotags()
    if not rows:
        log("no 'placed' geotags to apply.")
        return
    log(f"{len(rows)} geotag(s) to apply")

    usrc = json.loads(UNTAGGED_SRC.read_text(encoding="utf-8")) if UNTAGGED_SRC.exists() else {}
    uc = json.loads(UNTAGGED.read_text(encoding="utf-8")) if UNTAGGED.exists() else {"type": "FeatureCollection", "features": []}
    uprops = {f["properties"]["id"]: f["properties"] for f in uc["features"]}

    fc = json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else {"type": "FeatureCollection", "features": []}
    msrc = json.loads(MANIFEST_SRC.read_text(encoding="utf-8")) if MANIFEST_SRC.exists() else {}
    have = {f["properties"]["id"] for f in fc["features"]}

    applied, exif_ok, exif_fail, no_src = [], 0, 0, 0
    for r in rows:
        pid, lon, lat = str(r["id"]), float(r["lon"]), float(r["lat"])
        src = usrc.get(pid)
        if not src or not Path(src).exists():
            no_src += 1
            print(f"  ! no source file for {pid}", file=sys.stderr)
            applied.append(pid)            # still mark applied so it's not re-shown
            continue
        if args.dry_run:
            print(f"  would tag {Path(src).name} -> {lat:.5f},{lon:.5f}")
            continue
        if write_exif_gps(src, lon, lat):
            exif_ok += 1
        else:
            exif_fail += 1
        # add to the geotagged manifest (thumb/web already on R2 from the catalog)
        if pid not in have:
            p = uprops.get(pid, {})
            fc["features"].append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {"id": pid, "filename": p.get("filename"),
                               "timestamp": p.get("timestamp"), "altitude": None,
                               "direction": None, "is_360": False, "pano_type": None},
            })
            msrc[pid] = src
            have.add(pid)
        applied.append(pid)

    if args.dry_run:
        log(f"dry run: {len(rows)} would be applied")
        return

    # drop applied photos from the untagged catalog
    aset = set(applied)
    uc["features"] = [f for f in uc["features"] if f["properties"]["id"] not in aset]
    for pid in aset:
        usrc.pop(pid, None)

    MANIFEST.write_text(json.dumps(fc), encoding="utf-8")
    MANIFEST_SRC.write_text(json.dumps(msrc), encoding="utf-8")
    UNTAGGED.write_text(json.dumps(uc), encoding="utf-8")
    UNTAGGED_SRC.write_text(json.dumps(usrc), encoding="utf-8")

    # mark them applied in D1 (so the gallery doesn't show them again)
    ids_sql = ",".join("'" + pid.replace("'", "") + "'" for pid in applied)
    d1(f"UPDATE photo_geotags SET status='applied' WHERE id IN ({ids_sql});")

    log(f"applied={len(applied)} exif_ok={exif_ok} exif_fail={exif_fail} no_src={no_src}")

    # publish: rebuild the photo tile + the untagged catalog, upload, deploy
    run([PY, "build_pmtiles.py", "photos"])
    run([PY, "upload_r2.py", "tiles"])
    run([PY, "upload_r2.py", "data"])        # refresh untagged.geojson on R2
    if args.no_deploy:
        log("skipping deploy (--no-deploy)")
    else:
        deploy()
    verify_gate()
    log("apply_geotags done.")


if __name__ == "__main__":
    main()
