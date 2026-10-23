#!/usr/bin/env python3
"""Build PMTiles archives from the exported layers using GDAL's ogr2ogr.

GDAL 3.8+ has a native PMTiles writer, and QGIS ships GDAL on Windows, so no
Docker/tippecanoe/WSL is needed. Each layer becomes its own .pmtiles in ./out,
which the web app loads from R2 via the pmtiles:// protocol.

Usage:
    python build_pmtiles.py            # build all layers present
    python build_pmtiles.py photos     # build a single layer
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

OUT = Path("out")

# Find ogr2ogr: PATH first, then a QGIS install (OSGeo4W per-user / root, then
# the standalone Program Files installer).
def find_ogr():
    from shutil import which
    exe = which("ogr2ogr")
    if exe:
        return exe
    import glob
    local = os.environ.get("LOCALAPPDATA", "")
    cands = (glob.glob(os.path.join(local, r"Programs\OSGeo4W\bin\ogr2ogr.exe"))
             + glob.glob(r"C:\OSGeo4W\bin\ogr2ogr.exe")
             + sorted(glob.glob(r"C:\Program Files\QGIS *\bin\ogr2ogr.exe"), reverse=True))
    if cands:
        return cands[0]
    sys.exit("ogr2ogr not found (install GDAL or QGIS).")


OGR = find_ogr()

# OSGeo4W's bare ogr2ogr.exe (outside its own shell) doesn't know where its data
# lives; point it there so the EPSG:3857 reprojection for PMTiles can find proj.db.
_o4w = Path(OGR).parent.parent
if (_o4w / "share" / "proj" / "proj.db").exists():
    os.environ.setdefault("PROJ_DATA", str(_o4w / "share" / "proj"))
    os.environ.setdefault("GDAL_DATA", str(_o4w / "apps" / "gdal" / "share" / "gdal"))

# layer name -> (input file, geometry layer name, maxzoom)
LAYERS = {
    "hikes": ("hikes.geojsonl", "hikes", 13),
    # map-matched hikes/drives (snap_regions.py) + the roads the Google history covered
    "hikes_snapped": ("hikes_snapped.geojsonl", "hikes_snapped", 13),
    "roads_travelled": ("roads_travelled.geojsonl", "roads_travelled", 13),
    "garmin": ("garmin.geojson", "garmin", 12),
    # GPS activity tracks pulled from Garmin Connect (LineString per activity)
    "garmin_tracks": ("garmin_tracks.geojsonl", "garmin_tracks", 13),
    "google_paths": ("google_path_points.geojson", "google_paths", 12),
    "google_old": ("google_old_full.geojsonl", "google_old", 11),
    # photos: prefer the pre-scan snapshot if the live manifest is being written
    "photos": ("photos.geojson", "photos", 12),
    # drive-time isochrones (MultiPolygon per time band; regional -> low maxzoom)
    "isochrones": ("isochrones.geojson", "isochrones", 10),
}


# Photos: zoomed-out tiles used to carry ALL ~52k points (z0 was one 1.6 MB tile),
# so a continent view downloaded megabytes just to draw dots. Below FULL_FROM we
# ship a thinned copy instead: one photo per THIN_CELL_PX grid cell at the band's
# top zoom (so the dots still read as full coverage). All bands are written under
# the SAME layer name ("photos") via GDAL's CONF target_name, so the web app needs
# no changes. (band top zoom, band bottom zoom)
PHOTO_BANDS = [(4, 0), (6, 5), (8, 7)]
FULL_FROM = 9
THIN_CELL_PX = 4


def _merc(lon, lat):
    """lon/lat -> normalized web-mercator x,y in [0,1]."""
    import math
    lat = max(min(lat, 85.05112878), -85.05112878)
    s = math.sin(math.radians(lat))
    return (lon + 180.0) / 360.0, 0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)


def _thin(features, top_zoom):
    """Keep one feature per THIN_CELL_PX cell at top_zoom. The smallest id wins,
    so the choice is stable across rebuilds (dots don't jump around)."""
    n = 256 * 2 ** top_zoom / THIN_CELL_PX
    winners = {}
    for f in features:
        g = f.get("geometry") or {}
        if g.get("type") != "Point":
            continue
        x, y = _merc(*g["coordinates"][:2])
        key = (int(x * n), int(y * n))
        cur = winners.get(key)
        if cur is None or str(f["properties"].get("id", "")) < str(cur["properties"].get("id", "")):
            winners[key] = f
    return list(winners.values())


def build_photos(src: Path, maxz: int):
    """photos.geojson -> photos.pmtiles with zoom-banded thinning (see PHOTO_BANDS)."""
    import json
    from private_photos import load_private_ids
    from private_photos import null_island
    feats = json.loads(src.read_text(encoding="utf-8"))["features"]
    private = load_private_ids()          # never on the map (see private_photos.py)
    n = len(feats)
    feats = [f for f in feats if f["properties"].get("id") not in private and not null_island(f)]
    print(f"  left out {n - len(feats)} private or 0,0-located photo(s)")
    # junk (tag_photos.py keep=false: screenshots, receipts, blurry shots) is flagged
    # so the map hides it by default, and never picked as a zoomed-out representative
    tags_file = OUT / "photo_tags.json"
    junk = {i for i, t in json.loads(tags_file.read_text(encoding="utf-8")).items()
            if t.get("keep") is False} if tags_file.exists() else set()
    for f in feats:                      # every feature carries the field, so every band's schema has it
        f["properties"]["junk"] = 1 if f["properties"].get("id") in junk else 0
    shown = [f for f in feats if not f["properties"].get("junk")]
    print(f"  {len(feats) - len(shown):,} junk photo(s) flagged")
    work = OUT / "_photo_bands"
    work.mkdir(exist_ok=True)
    gpkg = work / "photo_bands.gpkg"
    if gpkg.exists():
        gpkg.unlink()

    conf = {}
    layers = [(f"band_z{top}", bottom, top, _thin(shown, top)) for top, bottom in PHOTO_BANDS]
    layers.append(("full", FULL_FROM, maxz, feats))
    for i, (lname, zmin, zmax, lfeats) in enumerate(layers):
        gj = work / f"{lname}.geojson"
        gj.write_text(json.dumps({"type": "FeatureCollection", "features": lfeats}), encoding="utf-8")
        cmd = [OGR, "-f", "GPKG", str(gpkg), str(gj), "-nln", lname]
        if i:
            cmd[1:3] = ["-update", "-append"]
        if subprocess.run(cmd).returncode != 0:
            print(f"  FAILED writing band {lname}")
            return False
        conf[lname] = {"target_name": "photos", "minzoom": zmin, "maxzoom": zmax}
        print(f"  band {lname:8s} z{zmin}-{zmax}: {len(lfeats):,} photos")

    dst = OUT / "photos.pmtiles"
    tmp = OUT / "photos.pmtiles.tmp"
    if tmp.exists():
        tmp.unlink()
    cmd = [
        OGR, "-f", "PMTiles", str(tmp), str(gpkg),
        "-dsco", "MINZOOM=0", "-dsco", f"MAXZOOM={maxz}",
        "-dsco", "MAX_SIZE=5000000",
        "-dsco", "CONF=" + json.dumps(conf),
        "-skipfailures",
    ]
    r = subprocess.run(cmd)
    if r.returncode == 0 and tmp.exists():
        if dst.exists():
            dst.unlink()
        tmp.rename(dst)
        import shutil
        shutil.rmtree(work, ignore_errors=True)   # band files are ~photos.geojson-sized scratch
        print(f"  ok -> out/photos.pmtiles ({dst.stat().st_size/1e6:.1f} MB)")
        return True
    print(f"  FAILED photos (exit {r.returncode})")
    return False


def without_excluded(name: str, src: Path) -> Path:
    """The hikes (or their snapped copy) minus excluded_tracks.json's tracks, as a
    temporary file; `src` itself when nothing is excluded."""
    from excluded_tracks import excluded_lines
    gone = excluded_lines()
    if not gone:
        return src
    tmp = OUT / f"_{name}.kept.geojsonl"
    dropped = 0
    with open(src, encoding="utf-8") as fin, open(tmp, "w", encoding="utf-8") as fout:
        for i, line in enumerate(fin):
            if name == "hikes":
                skip = i in gone
            else:
                skip = '"src_idx"' in line and json.loads(line)["properties"].get("src_idx") in gone
            if skip:
                dropped += 1
            else:
                fout.write(line)
    print(f"  left out {dropped} feature(s) of {len(gone)} excluded track(s)")
    return tmp


def build(name: str):
    infile, layer, maxz = LAYERS[name]
    src = OUT / infile
    # fall back to the snapshot for photos / full-export for google_old
    if name == "photos" and not src.exists():
        src = OUT / "photos.geojson"
    if name == "google_old" and not src.exists():
        src = OUT / "google_old.geojson"  # thinned fallback
    if not src.exists():
        print(f"  skip {name}: {src} not found")
        return False
    if name == "photos":
        print(f"  building photos <- {src.name} (maxzoom {maxz}, thinned below z{FULL_FROM})", flush=True)
        return build_photos(src, maxz)

    if name in ("hikes", "hikes_snapped"):
        src = without_excluded(name, src)
    dst = OUT / f"{name}.pmtiles"
    tmp = OUT / f"{name}.pmtiles.tmp"
    if tmp.exists():
        tmp.unlink()
    cmd = [
        OGR, "-f", "PMTiles", str(tmp), str(src),
        "-nln", layer,
        "-dsco", "MINZOOM=0", "-dsco", f"MAXZOOM={maxz}",
        "-dsco", "MAX_SIZE=5000000",
        "-skipfailures",
    ]
    print(f"  building {name} <- {src.name} (maxzoom {maxz})", flush=True)
    r = subprocess.run(cmd)
    if r.returncode == 0 and tmp.exists():
        if dst.exists():
            dst.unlink()
        tmp.rename(dst)
        print(f"  ok -> out/{name}.pmtiles ({dst.stat().st_size/1e6:.1f} MB)")
        return True
    print(f"  FAILED {name} (exit {r.returncode})")
    return False


def main(argv=None):
    argv = argv or sys.argv[1:]
    names = argv or list(LAYERS)
    ok = sum(build(n) for n in names if n in LAYERS)
    print(f"done: {ok}/{len(names)} layers built")


if __name__ == "__main__":
    main()
