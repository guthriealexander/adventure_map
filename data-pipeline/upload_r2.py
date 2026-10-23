#!/usr/bin/env python3
"""Upload data + photo derivatives to the R2 bucket via the S3 API (parallel).

Reads R2 credentials from environment or a local `.r2.env` file (gitignored):

    R2_ACCOUNT_ID=...
    R2_ACCESS_KEY_ID=...
    R2_SECRET_ACCESS_KEY=...
    R2_BUCKET=photos

Create the keys in the Cloudflare dashboard:
    R2 -> "Manage R2 API Tokens" -> Create API Token -> Object Read & Write.

Usage:
    pip install boto3
    python upload_r2.py data        # uploads out/*.geojson  -> data/<name>.geojson
    python upload_r2.py photos      # uploads derivatives/<id>/* -> photos/<id>/*
    python upload_r2.py tiles       # uploads out/*.pmtiles   -> tiles/<name>.<hash>.pmtiles + data/tiles.json
    python upload_r2.py trips       # uploads out/trips.json  -> data/trips.json
    python upload_r2.py suggestions # geotag gallery: untagged.min.json + geotag_ai_suggestions.json
    python upload_r2.py all
    python upload_r2.py purge-private  # delete R2 copies of photos in private_photos.txt
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

OUT = Path("out")
DERIV = Path("derivatives")
CT = {".webp": "image/webp", ".geojson": "application/json",
      ".json": "application/json", ".pmtiles": "application/octet-stream"}


def load_env():
    envf = Path(".r2.env")
    if envf.exists():
        for line in envf.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def client():
    import boto3
    acct = os.environ["R2_ACCOUNT_ID"]
    return boto3.client(
        "s3",
        endpoint_url=f"https://{acct}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


# ----- versioned tiles -----------------------------------------------------------
# Tile archives go up under a content hash (tiles/<name>.<hash>.pmtiles) and
# data/tiles.json names the current one for each layer; the web app reads that once
# at start-up. A page that stays open keeps reading the version it started with -
# overwriting tiles/<name>.pmtiles in place let an open page (or a cached range)
# mix the old header with the new file, and the layer silently drew nothing.
TILE_KEEP = 3          # versions kept per layer (current + 2 older, for open pages)


def tile_version(f: Path) -> str:
    h = hashlib.sha1()
    with f.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:10]


def finish_tiles(s3, bucket, versions):
    """After the archives are up: publish data/tiles.json (merged with what's there, so
    layers not rebuilt keep theirs), then drop all but the newest TILE_KEEP versions."""
    man = {}
    try:
        man = json.loads(s3.get_object(Bucket=bucket, Key="data/tiles.json")["Body"].read())
    except Exception:
        pass
    man.update(versions)
    (OUT / "tiles.json").write_text(json.dumps(man, indent=1), encoding="utf-8")
    s3.put_object(Bucket=bucket, Key="data/tiles.json", Body=json.dumps(man).encode(),
                  ContentType="application/json")
    print(f"  data/tiles.json: {len(man)} layer(s)")
    objs = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="tiles/"):
        objs += page.get("Contents", [])
    for name, cur in man.items():
        mine = [o for o in objs if o["Key"].startswith(f"tiles/{name}.") and o["Key"].count(".") == 2]
        mine.sort(key=lambda o: o["LastModified"], reverse=True)
        keep = {f"tiles/{name}.{cur}.pmtiles"} | {o["Key"] for o in mine[:TILE_KEEP]}
        for o in mine:
            if o["Key"] not in keep:
                s3.delete_object(Bucket=bucket, Key=o["Key"])
                print(f"  pruned old {o['Key']}")


def write_untagged_min():
    """untagged.geojson -> untagged.min.json for the web app's filmstrip.

    The full catalog (13k features with filenames, nulls, GeoJSON boilerplate) is
    ~2.2 MB to parse on every page load, but the app only needs id / timestamp /
    is_360. Generated HERE because every publish path runs `upload_r2.py data`, so
    the slim copy can never drift from the full catalog that ships alongside it."""
    import json
    src = OUT / "untagged.geojson"
    if not src.exists():
        return
    from private_photos import load_private_ids
    private = load_private_ids()          # private photos never reach the geotag gallery
    fc = json.loads(src.read_text(encoding="utf-8"))
    rows = []
    for f in fc.get("features", []):
        p = f.get("properties") or {}
        if p.get("id") and p["id"] not in private:
            rows.append([p["id"], p.get("timestamp") or "", 1 if p.get("is_360") else 0])
    dst = OUT / "untagged.min.json"
    dst.write_text(json.dumps({"fields": ["id", "ts", "is_360"], "rows": rows},
                              separators=(",", ":")), encoding="utf-8")
    print(f"  untagged.min.json: {len(rows):,} photos, "
          f"{src.stat().st_size/1e6:.2f} MB -> {dst.stat().st_size/1e6:.2f} MB")


def public_catalog(f):
    """photos.geojson minus private photos (private_photos.py), as a publish copy."""
    import json
    from private_photos import load_private_ids
    private = load_private_ids()
    if f.name != "photos.geojson" or not private:
        return f
    fc = json.loads(f.read_text(encoding="utf-8"))
    from private_photos import null_island
    fc["features"] = [x for x in fc["features"]
                      if (x.get("properties") or {}).get("id") not in private and not null_island(x)]
    dst = OUT / "_publish" / f.name
    dst.parent.mkdir(exist_ok=True)
    dst.write_text(json.dumps(fc), encoding="utf-8")
    return dst


def collect(kind):
    """Yield (local_path, r2_key) pairs."""
    if kind in ("data", "all"):
        for f in OUT.glob("*.geojson"):
            yield public_catalog(f), f"data/{f.name}"
        slim = OUT / "untagged.min.json"
        if slim.exists():
            yield slim, f"data/{slim.name}"
    if kind in ("suggestions", "data", "all"):    # geotag gallery: no-GPS catalog + AI place guesses
        if kind == "suggestions":
            write_untagged_min()
        for name in ("untagged.min.json", "geotag_ai_suggestions.json"):
            f = OUT / name
            if f.exists() and kind == "suggestions":
                yield f, f"data/{name}"
        ai = OUT / "geotag_ai_suggestions.json"
        if ai.exists() and kind != "suggestions":
            yield ai, f"data/{ai.name}"
    if kind in ("trips", "data", "all"):
        trips = OUT / "trips.json"                # build_trips.py / name_trips.py -> Trips panel
        if trips.exists():
            yield trips, "data/trips.json"
        for f in (OUT / "trips").glob("*.json"):   # per-trip photo points
            yield f, f"data/trips/{f.name}"
    if kind in ("tiles", "all"):
        for f in OUT.glob("*.pmtiles"):
            yield f, f"tiles/{f.stem}.{tile_version(f)}.pmtiles"
    if kind in ("photos", "all"):
        from private_photos import load_private_ids
        private = load_private_ids()      # private photos are never uploaded
        for f in DERIV.glob("*/*.webp"):
            if f.parent.name not in private:
                yield f, f"photos/{f.parent.name}/{f.name}"


def purge_private(s3, bucket):
    """Delete R2 copies (photos/<id>/*) of every photo in private_photos.txt."""
    from private_photos import load_private_ids
    keys = []
    for pid in sorted(load_private_ids()):
        for o in s3.list_objects_v2(Bucket=bucket, Prefix=f"photos/{pid}/").get("Contents", []):
            keys.append(o["Key"])
    for k in keys:
        s3.delete_object(Bucket=bucket, Key=k)
        print(f"  deleted r2://{bucket}/{k}")
    print(f"purge-private: {len(keys)} object(s) removed")


def main():
    kind = sys.argv[1] if len(sys.argv) > 1 else "all"
    if kind in ("data", "all"):
        write_untagged_min()
    load_env()
    bucket = os.environ.get("R2_BUCKET", "photos")
    s3 = client()
    if kind == "purge-private":
        return purge_private(s3, bucket)
    items = list(collect(kind))

    # Photo derivatives and tile versions are content-hashed (immutable) -> skip ones
    # already on R2 so re-runs only send what's missing. Data changes in place, so
    # it is always overwritten.
    versions = {Path(k).name.split(".")[0]: Path(k).name.split(".")[1] for _, k in items if k.startswith("tiles/")}
    if kind in ("photos", "tiles"):
        existing = set()
        pag = s3.get_paginator("list_objects_v2")
        for page in pag.paginate(Bucket=bucket, Prefix=f"{kind}/"):
            for o in page.get("Contents", []):
                existing.add(o["Key"])
        before = len(items)
        items = [(p, k) for (p, k) in items if k not in existing]
        print(f"  {before - len(items):,} already on R2, {len(items):,} to upload")

    print(f"uploading {len(items):,} objects to r2://{bucket} ...")

    def put(item):
        path, key = item
        ext = path.suffix.lower()
        s3.upload_file(str(path), bucket, key,
                       ExtraArgs={"ContentType": CT.get(ext, "application/octet-stream")})
        return key

    done = 0
    with ThreadPoolExecutor(max_workers=16) as ex:
        futs = [ex.submit(put, it) for it in items]
        for fut in as_completed(futs):
            fut.result()
            done += 1
            if done % 200 == 0 or done == len(items):
                print(f"  {done:,}/{len(items):,}")
    if versions:                                  # only once every archive is up
        finish_tiles(s3, bucket, versions)
    print("done.")


if __name__ == "__main__":
    main()
