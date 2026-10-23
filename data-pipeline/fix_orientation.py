#!/usr/bin/env python3
"""Re-generate + re-upload derivatives for photos with a non-normal EXIF
orientation (so the rotated/upside-down ones display correctly).

`make_derivatives` now applies EXIF orientation, but derivatives generated before
that fix are still wrong. This finds every photo whose source has Orientation != 1,
regenerates its thumb/web/(pano) WebP, and overwrites them on R2.

Usage (from data-pipeline/):
    python fix_orientation.py              # all manifests, regenerate + upload
    python fix_orientation.py --no-upload  # regenerate locally only
    python fix_orientation.py --limit 200  # cap (testing)
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from pathlib import Path

from photos_ingest import make_derivatives  # now applies exif_transpose

HERE = Path(__file__).resolve().parent
DERIV = HERE / "derivatives"
MANIFESTS = [HERE / "out" / "photos.geojson", HERE / "out" / "untagged.geojson"]


def load_json(p):
    return json.loads(Path(p).read_text(encoding="utf-8")) if Path(p).exists() else {}


def gather():
    """Return {id: (src_path, is_360)} across both manifests."""
    out = {}
    for mf in MANIFESTS:
        if not mf.exists():
            continue
        fc = json.loads(mf.read_text(encoding="utf-8"))
        src = load_json(mf.with_suffix(".src.json"))
        for f in fc["features"]:
            p = f["properties"]
            sp = src.get(p["id"])
            if sp:
                out[p["id"]] = (sp, bool(p.get("is_360")))
    return out


def _worker(task):
    """If the source has a non-normal orientation, regenerate its derivatives."""
    pid, src, is360, outdir = task
    from PIL import Image
    try:
        with Image.open(src) as im:
            o = im.getexif().get(0x0112)
            if not o or o == 1:
                return ("ok", pid, [])           # already upright
            d = Path(outdir) / pid
            wanted = ["thumb", "web"] + (["pano"] if is360 else [])
            for w in wanted:                     # delete so make_derivatives rewrites
                t = d / f"{w}.webp"
                if t.exists():
                    t.unlink()
            make_derivatives(im, d, wanted)      # applies exif_transpose now
            files = [str(d / f"{w}.webp") for w in wanted if (d / f"{w}.webp").exists()]
            return ("fixed", pid, files)
    except Exception as e:
        return ("err", pid, str(e))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-upload", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    import os
    ap.add_argument("--workers", type=int, default=min(12, (os.cpu_count() or 4)))
    args = ap.parse_args()

    items = list(gather().items())
    if args.limit:
        items = items[: args.limit]
    print(f"checking {len(items):,} photos for non-normal orientation ...", flush=True)
    tasks = [(pid, sp, is360, str(DERIV)) for pid, (sp, is360) in items]

    fixed_files, fixed, errors, checked = [], 0, 0, 0
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        for status, pid, payload in ex.map(_worker, tasks, chunksize=16):
            checked += 1
            if status == "fixed":
                fixed += 1
                fixed_files.extend(payload)
            elif status == "err":
                errors += 1
                if errors <= 20:
                    print(f"  ! {pid}: {payload}", file=sys.stderr)
            if checked % 5000 == 0:
                print(f"  checked {checked:,}/{len(tasks):,}, fixed {fixed:,}", flush=True)
    print(f"regenerated {fixed:,} photos ({len(fixed_files):,} files), errors {errors}")

    if args.no_upload or not fixed_files:
        print("done (no upload)." if args.no_upload else "nothing to upload.")
        return

    # overwrite the corrected derivatives on R2
    from upload_r2 import load_env, client
    import os
    load_env()
    s3 = client()
    bucket = os.environ.get("R2_BUCKET", "photos")
    print(f"uploading {len(fixed_files):,} corrected derivatives to r2://{bucket} ...", flush=True)

    def put(path):
        p = Path(path)
        key = f"photos/{p.parent.name}/{p.name}"
        s3.upload_file(str(p), bucket, key, ExtraArgs={"ContentType": "image/webp"})
        return key

    done = 0
    with ThreadPoolExecutor(max_workers=16) as tp:
        futs = [tp.submit(put, f) for f in fixed_files]
        for fut in as_completed(futs):
            fut.result()
            done += 1
            if done % 500 == 0 or done == len(fixed_files):
                print(f"  {done:,}/{len(fixed_files):,}", flush=True)
    print("done. (browser cache: hard-refresh to see corrected images)")


if __name__ == "__main__":
    main()
