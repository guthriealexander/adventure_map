#!/usr/bin/env python3
"""Publish the map's data: photo copies, tiles and JSON, to wherever the site reads
them from. Settings come from storage.env (see storage.env.example) or the environment:

  STORAGE=s3       any S3-compatible bucket: Cloudflare R2, AWS S3, Backblaze B2,
                   Wasabi, MinIO, ...
      S3_ENDPOINT=https://<account>.r2.cloudflarestorage.com   (leave out for AWS)
      S3_REGION=auto
      S3_BUCKET=photos
      S3_ACCESS_KEY_ID=...
      S3_SECRET_ACCESS_KEY=...
  STORAGE=folder   a plain folder, normally web/media so the files are published
                   with the site (GitHub Pages, Netlify, Vercel, any web server)
      FOLDER=../web/media

Either way the layout is the same: photos/<id>/{thumb,web,pano}.webp, tiles/ and data/,
and web/site.js `mediaBase` points the app at it.

Usage:
    python upload.py data        # out/*.geojson                -> data/<name>.geojson
    python upload.py photos      # derivatives/<id>/*           -> photos/<id>/*
    python upload.py tiles       # out/*.pmtiles                -> tiles/<name>.<hash>.pmtiles + data/tiles.json
    python upload.py trips       # out/trips.json + trips/      -> data/
    python upload.py suggestions # geotag gallery: untagged.min.json + geotag_ai_suggestions.json
    python upload.py places      # out/pois.geojson             -> data/pois.geojson (read-only sites)
    python upload.py all
    python upload.py purge-private  # delete published copies of photos in private_photos.txt
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
    for name in ("storage.env", ".r2.env"):          # .r2.env: older setups
        envf = Path(name)
        if envf.exists():
            for line in envf.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


# ----- where the files go ---------------------------------------------------------
# Two backends with the same five operations: list keys (with modified times), read,
# write bytes, upload a file, delete.

class S3Store:
    def __init__(self):
        import boto3
        env = os.environ.get
        endpoint = env("S3_ENDPOINT") or (f"https://{env('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com"
                                          if env("R2_ACCOUNT_ID") else None)
        self.bucket = env("S3_BUCKET") or env("R2_BUCKET") or "photos"
        self.s3 = boto3.client("s3", endpoint_url=endpoint, region_name=env("S3_REGION") or "auto",
                               aws_access_key_id=env("S3_ACCESS_KEY_ID") or env("R2_ACCESS_KEY_ID"),
                               aws_secret_access_key=env("S3_SECRET_ACCESS_KEY") or env("R2_SECRET_ACCESS_KEY"))
        self.label = f"s3://{self.bucket}"

    def list(self, prefix):
        out = {}
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            for o in page.get("Contents", []):
                out[o["Key"]] = o["LastModified"].timestamp()
        return out

    def get(self, key):
        try:
            return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except Exception:
            return None

    def put_bytes(self, key, data, ctype):
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=ctype)

    def upload(self, path, key, ctype):
        self.s3.upload_file(str(path), self.bucket, key, ExtraArgs={"ContentType": ctype})

    def delete(self, key):
        self.s3.delete_object(Bucket=self.bucket, Key=key)

    def presign(self, key, ttl):
        """A temporary link to a private object (tag_photos.py sends these to Claude)."""
        return self.s3.generate_presigned_url("get_object", ExpiresIn=ttl,
                                              Params={"Bucket": self.bucket, "Key": key})


class FolderStore:
    def __init__(self):
        self.root = Path(os.environ.get("FOLDER") or "../web/media").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.label = str(self.root)

    def list(self, prefix):
        base = self.root / prefix
        if not base.exists():
            return {}
        return {p.relative_to(self.root).as_posix(): p.stat().st_mtime for p in base.rglob("*") if p.is_file()}

    def get(self, key):
        p = self.root / key
        return p.read_bytes() if p.exists() else None

    def put_bytes(self, key, data, ctype):
        p = self.root / key
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def upload(self, path, key, ctype):
        import shutil
        p = self.root / key
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, p)

    def delete(self, key):
        (self.root / key).unlink(missing_ok=True)

    def presign(self, key, ttl):
        return None                                # no link: callers send the file itself


def store():
    kind = (os.environ.get("STORAGE") or ("s3" if os.environ.get("R2_ACCOUNT_ID") else "folder")).lower()
    return FolderStore() if kind == "folder" else S3Store()


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


def finish_tiles(st, versions):
    """After the archives are up: publish data/tiles.json (merged with what's there, so
    layers not rebuilt keep theirs), then drop all but the newest TILE_KEEP versions."""
    man = {}
    try:
        man = json.loads(st.get("data/tiles.json") or b"{}")
    except Exception:
        pass
    man.update(versions)
    (OUT / "tiles.json").write_text(json.dumps(man, indent=1), encoding="utf-8")
    st.put_bytes("data/tiles.json", json.dumps(man).encode(), "application/json")
    print(f"  data/tiles.json: {len(man)} layer(s)")
    objs = st.list("tiles/")
    for name, cur in man.items():
        mine = [k for k in objs if k.startswith(f"tiles/{name}.") and k.count(".") == 2]
        mine.sort(key=lambda k: objs[k], reverse=True)
        keep = {f"tiles/{name}.{cur}.pmtiles"} | set(mine[:TILE_KEEP])
        for k in mine:
            if k not in keep:
                st.delete(k)
                print(f"  pruned old {k}")


def write_untagged_min():
    """untagged.geojson -> untagged.min.json for the web app's filmstrip.

    The full catalog (13k features with filenames, nulls, GeoJSON boilerplate) is
    ~2.2 MB to parse on every page load, but the app only needs id / timestamp /
    is_360. Generated HERE because every publish path runs `upload.py data`, so
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
    """Yield (local_path, key) pairs."""
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
    if kind == "places":
        f = OUT / "pois.geojson"
        if f.exists():
            yield f, "data/pois.geojson"
    if kind in ("tiles", "all"):
        for f in OUT.glob("*.pmtiles"):
            yield f, f"tiles/{f.stem}.{tile_version(f)}.pmtiles"
    if kind in ("photos", "all"):
        from private_photos import load_private_ids
        private = load_private_ids()      # private photos are never uploaded
        for f in DERIV.glob("*/*.webp"):
            if f.parent.name not in private:
                yield f, f"photos/{f.parent.name}/{f.name}"


def purge_private(st):
    """Delete published copies (photos/<id>/*) of every photo in private_photos.txt."""
    from private_photos import load_private_ids
    keys = []
    for pid in sorted(load_private_ids()):
        keys += list(st.list(f"photos/{pid}/"))
    for k in keys:
        st.delete(k)
        print(f"  deleted {st.label}/{k}")
    print(f"purge-private: {len(keys)} object(s) removed")


def main():
    kind = sys.argv[1] if len(sys.argv) > 1 else "all"
    if kind in ("data", "all"):
        write_untagged_min()
    load_env()
    st = store()
    if kind == "purge-private":
        return purge_private(st)
    items = list(collect(kind))

    # Photo derivatives and tile versions are content-hashed (immutable) -> skip ones
    # already published so re-runs only send what's missing. Data changes in place, so
    # it is always overwritten.
    versions = {Path(k).name.split(".")[0]: Path(k).name.split(".")[1] for _, k in items if k.startswith("tiles/")}
    if kind in ("photos", "tiles"):
        existing = set(st.list(f"{kind}/"))
        before = len(items)
        items = [(p, k) for (p, k) in items if k not in existing]
        print(f"  {before - len(items):,} already published, {len(items):,} to upload")

    print(f"uploading {len(items):,} objects to {st.label} ...")

    def put(item):
        path, key = item
        st.upload(path, key, CT.get(path.suffix.lower(), "application/octet-stream"))
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
        finish_tiles(st, versions)
    print("done.")


if __name__ == "__main__":
    main()
