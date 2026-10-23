#!/usr/bin/env python3
"""Photo ingest pipeline for the Adventure Map.

Two decoupled sub-commands:

  manifest   Read the existing photo GeoPackages (or, later, scan folders for
             new geotagged photos) and emit a GeoJSON manifest of photo points
             with STABLE ids, computed R2 keys, and an is_360 flag. No image
             files are opened, so this is fast and works before R2 exists.

  derivs     Given the manifest, generate web-friendly derivatives for each
             photo whose source file is present:
                 thumb (~320px)  -> hover preview
                 web   (~1600px) -> click popup
                 pano  (~4096px) -> 360 viewer (is_360 only)
             Idempotent: skips ids whose derivatives already exist.

Stable id = sha1("<filename>|<timestamp>")[:16]  -> survives file moves and
lets the same photo from different folders dedupe.

Usage:
    python photos_ingest.py manifest --gpkg ../../photos_archive_1.gpkg \
        ../../photos_archive_2.gpkg --gpkg-360 ../../360_photos_archive.gpkg \
        --out out/photos.geojson
    python photos_ingest.py derivs --manifest out/photos.geojson \
        --outdir derivatives [--limit N]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import struct
import sys
from pathlib import Path

def _register_heif():
    """Prep Pillow: enable .heic/.heif (if pillow-heif present) and allow loading
    slightly-truncated JPEGs (recovers partial files instead of erroring out)."""
    try:
        from PIL import ImageFile
        ImageFile.LOAD_TRUNCATED_IMAGES = True
    except Exception:
        pass
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception:
        pass


R2_PREFIX = "photos"
RE_360 = re.compile(r"(photosphere|pano(?!ramio)|equirect|_360\b|\b360)", re.IGNORECASE)
RE_SPHERE_NAME = re.compile(r"(photosphere|equirect|_360\b|\b360)", re.IGNORECASE)


def classify_pano(src_path, filename):
    """Return 'sphere' (true 360 equirectangular -> photo-sphere-viewer) or
    'wide' (stitched wide panorama -> large lightbox image).

    The authoritative signal is GPano XMP ProjectionType=equirectangular, which
    real photo spheres carry even when the image isn't a perfect 2:1 (partial
    spheres). Falls back to the filename, then to aspect ratio."""
    name_sphere = bool(RE_SPHERE_NAME.search(filename or ""))
    try:
        from PIL import Image
        if src_path and Path(src_path).exists():
            with Image.open(src_path) as im:
                w, h = im.size
                try:
                    xmp = str(im.getxmp())
                except Exception:
                    xmp = ""
            if "equirectangular" in xmp.lower() or "gpano" in xmp.lower():
                return "sphere"
            if name_sphere:
                return "sphere"
            if h and 1.9 <= (w / h) <= 2.1:
                return "sphere"
            return "wide"
    except Exception:
        pass
    return "sphere" if name_sphere else "wide"


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------
def stable_id(filename: str, timestamp) -> str:
    key = f"{filename}|{timestamp}".encode("utf-8", "replace")
    return hashlib.sha1(key).hexdigest()[:16]


def _read_gpkg_geom_xy(blob):
    """Decode a GeoPackage POINT blob -> (lon, lat). Minimal GPB parser."""
    if blob is None:
        return None
    # GPB header: 'GP', version, flags, srs_id(4). flags bit0=little-endian envelope
    flags = blob[3]
    envelope_codes = (flags >> 1) & 0x07
    env_sizes = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}
    offset = 8 + env_sizes.get(envelope_codes, 0)
    wkb = blob[offset:]
    byte_order = wkb[0]
    endian = "<" if byte_order == 1 else ">"
    geom_type = struct.unpack(endian + "I", wkb[1:5])[0]
    # Accept Point in any dimensionality: ISO 1/1001/2001/3001 (XY/XYZ/XYM/XYZM)
    # or EWKB (high bits flag Z/M/SRID). Base type 1 == Point. X/Y are always
    # the first two doubles, so we ignore any Z/M.
    base = (geom_type & 0xFF) if (geom_type & 0x80000000) else (geom_type % 1000)
    if base != 1:  # not a Point
        return None
    x, y = struct.unpack(endian + "dd", wkb[5:21])
    return [x, y]


def read_points(gpkg: Path, table: str, is_360: bool):
    con = sqlite3.connect(str(gpkg))
    cur = con.cursor()
    cols = {c[1] for c in cur.execute(f'PRAGMA table_info("{table}")')}
    geomcol = "geom" if "geom" in cols else "geometry"
    sel = [geomcol, "filename", "photo", "timestamp", "altitude", "direction"]
    sel = [c for c in sel if c in cols or c == geomcol]
    rows = cur.execute(f'SELECT {",".join(chr(34)+c+chr(34) for c in sel)} FROM "{table}"')
    out = []
    for row in rows:
        rec = dict(zip(sel, row))
        xy = _read_gpkg_geom_xy(rec.get(geomcol))
        if not xy:
            continue
        filename = rec.get("filename") or ""
        ts = rec.get("timestamp")
        pid = stable_id(filename, ts)
        flag = is_360 or bool(RE_360.search(filename))
        out.append({"id": pid, "xy": xy, "filename": filename,
                    "src_path": rec.get("photo"), "timestamp": ts,
                    "altitude": rec.get("altitude"),
                    "direction": rec.get("direction"), "is_360": flag})
    con.close()
    return out


def cmd_manifest(args):
    records, seen = [], {}
    sources = [(p, False) for p in args.gpkg] + [(p, True) for p in args.gpkg_360]
    for gpkg, is360 in sources:
        # discover the single features table
        con = sqlite3.connect(str(gpkg))
        tbl = con.execute(
            "SELECT table_name FROM gpkg_contents WHERE data_type='features' LIMIT 1"
        ).fetchone()[0]
        con.close()
        pts = read_points(Path(gpkg), tbl, is360)
        print(f"  {Path(gpkg).name}: {len(pts):,} points")
        records.extend(pts)

    features, dupes, src_map = [], 0, {}
    for r in records:
        if r["id"] in seen:
            # keep 360 flag if any source said 360
            if r["is_360"]:
                seen[r["id"]]["properties"]["is_360"] = True
            dupes += 1
            continue
        pano_type = classify_pano(r["src_path"], r["filename"]) if r["is_360"] else None
        # Served properties only — URLs are derived from id by the web app, and
        # src_path is kept out of the manifest (local path; goes to a sidecar).
        feat = {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": r["xy"]},
            "properties": {
                "id": r["id"], "filename": r["filename"],
                "timestamp": r["timestamp"], "altitude": r["altitude"],
                "direction": r["direction"], "is_360": r["is_360"],
                "pano_type": pano_type,  # 'sphere' | 'wide' | None
            },
        }
        seen[r["id"]] = feat
        src_map[r["id"]] = r["src_path"]
        features.append(feat)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps({"type": "FeatureCollection", "features": features}),
        encoding="utf-8",
    )
    # sidecar: id -> local source path, used only by the derivs step
    sidecar = args.out.with_suffix(".src.json")
    sidecar.write_text(json.dumps(src_map), encoding="utf-8")
    n360 = sum(1 for f in features if f["properties"]["is_360"])
    print(f"  deduped {dupes:,} | wrote {len(features):,} photos "
          f"({n360:,} are 360) -> {args.out}")
    print(f"  src sidecar -> {sidecar}")


# ---------------------------------------------------------------------------
# derivatives  (requires Pillow)
# ---------------------------------------------------------------------------
SIZES = {"thumb": 320, "web": 1600, "pano": 4096}


def make_derivatives(im, outdir, wanted):
    """Write the requested WebP derivatives from an open PIL image."""
    from PIL import ImageOps
    im = ImageOps.exif_transpose(im)   # apply EXIF orientation (fix rotated/upside-down)
    rgb = im.convert("RGB")
    outdir.mkdir(parents=True, exist_ok=True)
    for w in wanted:
        target = outdir / f"{w}.webp"
        if target.exists():
            continue
        edge = SIZES[w]
        c = rgb.copy()
        if w == "pano":
            if c.width > edge:
                c = c.resize((edge, max(1, c.height * edge // c.width)))
        else:
            c.thumbnail((edge, edge))
        c.save(target, "WEBP", quality=82, method=4)


# --- EXIF GPS / time --------------------------------------------------------
def _ratio(v):
    try:
        return float(v)
    except Exception:
        try:
            return v[0] / v[1]
        except Exception:
            return None


def parse_exif(im):
    """Return (lon, lat, iso_timestamp) from an open PIL image, or None GPS."""
    lon = lat = ts = None
    try:
        exif = im.getexif()
    except Exception:
        return None, None, None
    # timestamp: DateTimeOriginal (36867) preferred, else DateTime (306)
    raw = exif.get(36867) or exif.get(306)
    if isinstance(raw, str) and len(raw) >= 19:
        d, t = raw[:10].replace(":", "-"), raw[11:19]
        ts = f"{d}T{t}"
    # GPS IFD (0x8825)
    try:
        gps = exif.get_ifd(0x8825)
    except Exception:
        gps = None
    if gps:
        latv, lonv = gps.get(2), gps.get(4)
        latref, lonref = gps.get(1), gps.get(3)
        if latv and lonv and len(latv) == 3 and len(lonv) == 3:
            try:
                la = _ratio(latv[0]) + _ratio(latv[1]) / 60 + _ratio(latv[2]) / 3600
                lo = _ratio(lonv[0]) + _ratio(lonv[1]) / 60 + _ratio(lonv[2]) / 3600
                if latref == "S":
                    la = -la
                if lonref == "W":
                    lo = -lo
                if -90 <= la <= 90 and -180 <= lo <= 180 and (la or lo):
                    lat, lon = la, lo
            except (TypeError, ValueError, ZeroDivisionError):
                pass  # malformed GPS rationals -> treat as no GPS
    return lon, lat, ts


# ---------------------------------------------------------------------------
# Google Takeout: <image>.supplemental-metadata.json sidecars
# ---------------------------------------------------------------------------
RE_TAKEOUT_EDITED = re.compile(r"-edited(\(\d+\))?$", re.IGNORECASE)
RE_DUP_SUFFIX = re.compile(r"\((\d+)\)$")
RE_NAME_TIME = re.compile(r"(?<!\d)((?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01]))_((?:[01]\d|2[0-3])[0-5]\d[0-5]\d)")


def takeout_sidecar(path: Path):
    """Return the parsed Takeout JSON for an image, or None. Takeout names it
    '<name>.<ext>.supplemental-metadata.json', truncating the middle part on long
    names, and moves a '(n)' duplicate counter to just before '.json'."""
    import glob
    stem = path.stem
    m = RE_DUP_SUFFIX.search(stem)
    if m:  # IMG(1).jpg -> IMG.jpg.supplemental-metadata(1).json
        base = f"{stem[:m.start()]}{path.suffix}"
        pat = f"{glob.escape(base)}.*({m.group(1)}).json"
    else:
        pat = f"{glob.escape(path.name)}.*.json"
    for j in sorted(path.parent.glob(pat)):
        if not m and RE_DUP_SUFFIX.search(j.stem):
            continue  # belongs to IMG(n).jpg, not IMG.jpg
        try:
            return json.loads(j.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def takeout_geo(meta):
    """(lon, lat, iso_utc_timestamp) from a Takeout sidecar. geoData includes
    locations Google inferred or the user set, not just the file's EXIF."""
    lon = lat = ts = None
    g = (meta or {}).get("geoData") or {}
    la, lo = g.get("latitude"), g.get("longitude")
    if la is not None and lo is not None and (la or lo) and -90 <= la <= 90 and -180 <= lo <= 180:
        lat, lon = la, lo
    t = ((meta or {}).get("photoTakenTime") or {}).get("timestamp")
    if t:
        # sidecar time is UTC; manifest timestamps are EXIF local time. Approximate
        # local from longitude (solar offset, +-1h vs real zones/DST).
        from datetime import datetime, timezone, timedelta
        off = timedelta(hours=round(lon / 15)) if lon is not None else timedelta(0)
        ts = (datetime.fromtimestamp(int(t), tz=timezone.utc) + off).strftime("%Y-%m-%dT%H:%M:%S")
    return lon, lat, ts


def _derivs_worker(task):
    """Top-level worker (picklable) for the process pool."""
    pid, src, wanted, outdir = task
    from PIL import Image
    _register_heif()
    try:
        with Image.open(src) as im:
            make_derivatives(im, Path(outdir) / pid, wanted)
        return ("made", pid)
    except Exception as e:
        return ("error", f"{pid} {Path(src).name}: {e}")


def cmd_derivs(args):
    try:
        from PIL import Image  # noqa: F401 (ensures Pillow present before forking workers)
    except ImportError:
        sys.exit("Pillow required:  pip install Pillow")
    from concurrent.futures import ProcessPoolExecutor

    fc = json.loads(args.manifest.read_text(encoding="utf-8"))
    feats = fc["features"]
    # source paths live in the sidecar (manifest no longer carries src_path)
    sidecar = args.manifest.with_suffix(".src.json")
    src_map = json.loads(sidecar.read_text(encoding="utf-8")) if sidecar.exists() else {}
    if args.since:
        feats = [f for f in feats if (f["properties"].get("timestamp") or "") >= args.since]
    if args.limit:
        feats = feats[: args.limit]

    # plan work: skip already-done and missing-source up front
    tasks, skipped, missing = [], 0, 0
    for f in feats:
        p = f["properties"]
        pid = p["id"]
        src = src_map.get(pid)
        outdir = args.outdir / pid
        wanted = ["thumb", "web"] + (["pano"] if p["is_360"] else [])
        if all((outdir / f"{w}.webp").exists() for w in wanted):
            skipped += 1
            continue
        if not src or not Path(src).exists():
            missing += 1
            continue
        tasks.append((pid, src, wanted, str(args.outdir)))

    print(f"derivs: {len(tasks):,} to make, {skipped:,} done, {missing:,} no-src "
          f"| {args.workers} workers", flush=True)

    made = errors = 0
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        for i, (status, info) in enumerate(ex.map(_derivs_worker, tasks, chunksize=8), 1):
            if status == "made":
                made += 1
            else:
                errors += 1
                if errors <= 30:
                    print(f"  ! {info}", file=sys.stderr)
            if i % 1000 == 0:
                print(f"  {i:,}/{len(tasks):,} made", flush=True)

    print(f"derivs: made={made} skipped={skipped} missing_src={missing} errors={errors}")


# ---------------------------------------------------------------------------
# scan a folder of geotagged photos (EXIF GPS) -> manifest + derivatives
# ---------------------------------------------------------------------------
IMG_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".webp"}


def cmd_scan(args):
    try:
        from PIL import Image
    except ImportError:
        sys.exit("Pillow required:  pip install Pillow")
    _register_heif()

    # load (or start) the manifest + sidecar so scan APPENDS, deduped by id
    if args.manifest.exists():
        fc = json.loads(args.manifest.read_text(encoding="utf-8"))
    else:
        fc = {"type": "FeatureCollection", "features": []}
    sidecar = args.manifest.with_suffix(".src.json")
    src_map = json.loads(sidecar.read_text(encoding="utf-8")) if sidecar.exists() else {}
    have = {f["properties"]["id"] for f in fc["features"]}
    # filename (minus a "(n)" copy suffix) -> [(lon, lat, ts)]. Ids are
    # filename|timestamp, but copies of one photo can disagree on both time and
    # place: Google-made -PANO/-EFFECTS files lack an EXIF date, and Takeout copies
    # can carry a location edited in Google Photos. Same name at the same spot, or
    # within ~a day, is the same photo.
    def name_key(name):
        return RE_DUP_SUFFIX.sub("", name).lower()
    name_idx = {}
    for f in fc["features"]:
        if f.get("geometry"):
            p = f["properties"]
            name_idx.setdefault(name_key(p["filename"]), []).append(
                (*f["geometry"]["coordinates"][:2], p.get("timestamp")))

    def _secs(ts):
        from datetime import datetime
        try:
            return datetime.fromisoformat(ts).timestamp()
        except (TypeError, ValueError):
            return None

    def known_copy(name, lon, lat, ts):
        # date-only names ("2019-09-29(1)", Google collages) can be different
        # images, so those need the same spot within 2h; a camera name
        # (IMG_20140426_094934, or its "(1)" copy) only needs to be within ~a day.
        t = _secs(ts)
        for x, y, ots in name_idx.get(name_key(name), ()):
            ot = _secs(ots)
            dt = abs(t - ot) if t is not None and ot is not None else None
            spot = abs(x - lon) < 2e-4 and abs(y - lat) < 2e-4
            unique_name = not re.fullmatch(r"\d{4}-\d{2}-\d{2}", name_key(name))
            if spot and (dt is None or dt <= 7200):
                return True
            if unique_name and (spot or (dt is not None and dt <= 26 * 3600)):
                return True
        return False
    exclude = set(args.exclude or [])

    seen_files = added = nogps = dup = errors = edited = from_sidecar = 0
    new_ids = []
    for path in args.dir.rglob("*"):
        if path.suffix.lower() not in IMG_EXTS:
            continue
        if exclude & set(p.lower() for p in path.relative_to(args.dir).parts[:-1]):
            continue
        if args.takeout and RE_TAKEOUT_EDITED.search(path.stem):
            edited += 1  # Google's edited copy; the original sits alongside it
            continue
        if args.takeout and path.stem.lower().startswith("panoramio-"):
            # re-downloads of photos already uploaded to Panoramio: same pixels +
            # location as an original, but dated by upload, so they'd duplicate
            dup += 1
            continue
        seen_files += 1
        if seen_files % 2000 == 0:
            print(f"  scanned {seen_files:,} files: +{added} added, {nogps} no-gps, {dup} dup",
                  flush=True)
            # checkpoint so a long run survives interruption
            args.manifest.write_text(json.dumps(fc), encoding="utf-8")
            sidecar.write_text(json.dumps(src_map), encoding="utf-8")
        try:
            with Image.open(path) as im:
                lon, lat, ts = parse_exif(im)
                if args.takeout and (lon is None or lat is None or not ts):
                    slon, slat, sts = takeout_geo(takeout_sidecar(path))
                    if (lon is None or lat is None) and slon is not None:
                        lon, lat = slon, slat
                        from_sidecar += 1
                    ts = ts or sts
                if lon is None or lat is None:
                    nogps += 1
                    continue
                if not ts:  # e.g. IMG_20140122_073607 / PXL_20210813_091734865
                    fm = RE_NAME_TIME.search(path.stem)
                    dm = re.match(r"((?:19|20)\d\d-[01]\d-[0-3]\d)(?!\d)", path.stem)
                    if fm:
                        d, t = fm.group(1), fm.group(2)
                        ts = f"{d[:4]}-{d[4:6]}-{d[6:]}T{t[:2]}:{t[2:4]}:{t[4:6]}"
                    elif dm:  # Google collage/animation named by date
                        ts = f"{dm.group(1)}T00:00:00"
                filename = path.stem
                pid = stable_id(filename, ts)
                if pid in have or known_copy(filename, lon, lat, ts):
                    dup += 1
                    continue
                is_360 = bool(RE_360.search(filename)) or bool(RE_360.search(str(path.parent)))
                ptype = classify_pano(str(path), filename) if is_360 else None
                # Skip image resizing here when --no-derivs: a fast EXIF-only pass
                # that just grows the manifest + src sidecar, leaving the heavy
                # thumbnail work to the parallel `derivs` command (12 workers).
                if not args.no_derivs:
                    wanted = ["thumb", "web"] + (["pano"] if is_360 else [])
                    make_derivatives(im, args.outdir / pid, wanted)
            fc["features"].append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {"id": pid, "filename": filename, "timestamp": ts,
                               "altitude": None, "direction": None,
                               "is_360": is_360, "pano_type": ptype},
            })
            have.add(pid)
            name_idx.setdefault(name_key(filename), []).append((lon, lat, ts))
            src_map[pid] = str(path)
            new_ids.append((pid, filename, ts))
            added += 1
        except Exception as e:
            errors += 1
            if errors <= 20:
                print(f"  ! {path.name}: {e}", file=sys.stderr)
        if args.limit and added >= args.limit:
            break

    args.manifest.write_text(json.dumps(fc), encoding="utf-8")
    sidecar.write_text(json.dumps(src_map), encoding="utf-8")
    placed = _drop_from_untagged(args.untagged, new_ids) if new_ids else 0
    print(f"scan done: files={seen_files:,} added={added:,} (sidecar gps={from_sidecar:,}) "
          f"no_gps={nogps:,} dup={dup:,} edited_skipped={edited:,} errors={errors:,} "
          f"| manifest now {len(fc['features']):,}; removed {placed} from untagged")


def _drop_from_untagged(untagged: Path, new_ids):
    """Photos a scan just located no longer belong in the 'to place' catalog.
    Match by id, or by filename when the untagged entry has the same day or only
    a year-folder placeholder date (so its id was computed from a different ts)."""
    if not untagged.exists():
        return 0
    uc = json.loads(untagged.read_text(encoding="utf-8"))
    ids = {pid for pid, _, _ in new_ids}
    by_name = {}
    for _, fn, ts in new_ids:
        by_name.setdefault(fn.lower(), set()).add((ts or "")[:10])
    def located(p):
        if p["id"] in ids:
            return True
        days = by_name.get((p.get("filename") or "").lower())
        if not days:
            return False
        uts = p.get("timestamp") or ""
        return uts[:10] in days or uts.endswith("-01-01T00:00:00") or not uts
    drop = {f["properties"]["id"] for f in uc["features"] if located(f["properties"])}
    if not drop:
        return 0
    uc["features"] = [f for f in uc["features"] if f["properties"]["id"] not in drop]
    untagged.write_text(json.dumps(uc), encoding="utf-8")
    usrc_path = untagged.with_suffix(".src.json")
    if usrc_path.exists():
        usrc = json.loads(usrc_path.read_text(encoding="utf-8"))
        for pid in drop:
            usrc.pop(pid, None)
        usrc_path.write_text(json.dumps(usrc), encoding="utf-8")
    return len(drop)


# ---------------------------------------------------------------------------
# catalog the NO-GPS photos so they can be geotagged in the web app
# ---------------------------------------------------------------------------
def cmd_untagged(args):
    """Walk folders, collect images that have NO EXIF GPS, and append them to an
    'untagged' manifest (FeatureCollection with null geometry) + a src sidecar.
    Fast EXIF-only pass; run `derivs --manifest out/untagged.geojson` afterward to
    generate thumbnails for the gallery."""
    try:
        from PIL import Image
    except ImportError:
        sys.exit("Pillow required:  pip install Pillow")
    _register_heif()

    # photos already geotagged (in the main manifest) are not candidates
    tagged_ids = set()
    if args.manifest.exists():
        fc = json.loads(args.manifest.read_text(encoding="utf-8"))
        tagged_ids = {f["properties"]["id"] for f in fc["features"]}

    if args.untagged.exists():
        uc = json.loads(args.untagged.read_text(encoding="utf-8"))
    else:
        uc = {"type": "FeatureCollection", "features": []}
    sidecar = args.untagged.with_suffix(".src.json")
    src_map = json.loads(sidecar.read_text(encoding="utf-8")) if sidecar.exists() else {}
    have = {f["properties"]["id"] for f in uc["features"]}
    exclude = set(args.exclude or [])

    seen = added = hasgps = dup = errors = 0
    for path in args.dir.rglob("*"):
        if path.suffix.lower() not in IMG_EXTS:
            continue
        rel_parts = path.relative_to(args.dir).parts
        if exclude & set(p.lower() for p in rel_parts[:-1]):
            continue
        # --year-folders: only images under a top-level year folder (2002, 2003, ...)
        if args.year_folders and not (rel_parts and re.match(r"(19|20)\d\d", rel_parts[0])):
            continue
        seen += 1
        if seen % 2000 == 0:
            print(f"  scanned {seen:,}: +{added} untagged, {hasgps} have-gps, {dup} dup",
                  flush=True)
            args.untagged.write_text(json.dumps(uc), encoding="utf-8")
            sidecar.write_text(json.dumps(src_map), encoding="utf-8")
        try:
            with Image.open(path) as im:
                lon, lat, ts = parse_exif(im)
            if lon is not None and lat is not None:
                hasgps += 1
                continue  # already geotagged -> handled by `scan`
            # no EXIF date (common for scans) -> fall back to the year folder
            # (2002, 2003, ...) so the gallery can still group by year.
            if not ts and rel_parts:
                ym = re.match(r"(19|20)\d\d", rel_parts[0])
                if ym:
                    ts = f"{ym.group(0)}-01-01T00:00:00"
            filename = path.stem
            pid = stable_id(filename, ts)
            if pid in tagged_ids or pid in have:
                dup += 1
                continue
            uc["features"].append({
                "type": "Feature", "geometry": None,
                "properties": {"id": pid, "filename": filename,
                               "timestamp": ts, "is_360": False},
            })
            have.add(pid)
            src_map[pid] = str(path)
            added += 1
        except Exception as e:
            errors += 1
            if errors <= 20:
                print(f"  ! {path.name}: {e}", file=sys.stderr)
        if args.limit and added >= args.limit:
            break

    args.untagged.write_text(json.dumps(uc), encoding="utf-8")
    sidecar.write_text(json.dumps(src_map), encoding="utf-8")
    print(f"untagged: seen={seen:,} added={added:,} have_gps={hasgps:,} dup={dup:,} "
          f"errors={errors:,} | untagged manifest now {len(uc['features']):,} -> {args.untagged}")
    print("next:  python photos_ingest.py derivs --manifest", args.untagged,
          "&& python upload_r2.py photos && python upload_r2.py data")


# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("manifest")
    m.add_argument("--gpkg", nargs="*", type=Path, default=[])
    m.add_argument("--gpkg-360", nargs="*", type=Path, default=[])
    m.add_argument("--out", type=Path, default=Path("out/photos.geojson"))
    m.set_defaults(func=cmd_manifest)

    d = sub.add_parser("derivs")
    d.add_argument("--manifest", type=Path, default=Path("out/photos.geojson"))
    d.add_argument("--outdir", type=Path, default=Path("derivatives"))
    d.add_argument("--limit", type=int, default=0)
    d.add_argument("--since", default=None,
                   help="only photos with timestamp >= this (e.g. 2024-01-01)")
    import os as _os
    d.add_argument("--workers", type=int, default=min(12, (_os.cpu_count() or 4)),
                   help="parallel image workers (default min(12, cpu count))")
    d.set_defaults(func=cmd_derivs)

    s = sub.add_parser("scan", help="scan a folder of geotagged photos (EXIF GPS)")
    s.add_argument("--dir", type=Path, required=True)
    s.add_argument("--manifest", type=Path, default=Path("out/photos.geojson"))
    s.add_argument("--outdir", type=Path, default=Path("derivatives"))
    s.add_argument("--exclude", nargs="*", default=["small"],
                   help="subfolder names to skip (default: small)")
    s.add_argument("--limit", type=int, default=0)
    s.add_argument("--no-derivs", action="store_true",
                   help="fast EXIF-only pass: grow manifest+sidecar, skip thumbnails "
                        "(run `derivs` afterward for parallel image generation)")
    s.add_argument("--takeout", action="store_true",
                   help="Google Takeout folder: skip '-edited' copies, fall back to the "
                        ".supplemental-metadata.json location/time, dedupe 'IMG(1)' names")
    s.add_argument("--untagged", type=Path, default=Path("out/untagged.geojson"),
                   help="'to place' catalog; newly located photos are removed from it")
    s.set_defaults(func=cmd_scan)

    u = sub.add_parser("untagged", help="catalog NO-GPS photos for in-app geotagging")
    u.add_argument("--dir", type=Path, required=True)
    u.add_argument("--manifest", type=Path, default=Path("out/photos.geojson"),
                   help="the geotagged manifest (its ids are excluded as candidates)")
    u.add_argument("--untagged", type=Path, default=Path("out/untagged.geojson"))
    u.add_argument("--exclude", nargs="*", default=["small"])
    u.add_argument("--year-folders", action="store_true",
                   help="only photos under a top-level year folder (2002, 2003, ...)")
    u.add_argument("--limit", type=int, default=0)
    u.set_defaults(func=cmd_untagged)

    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
