#!/usr/bin/env python3
"""Remove duplicate records of the same photo from out/photos.geojson (+ .src.json).

Ids are sha1(filename|timestamp), so copies of one photo whose timestamp differs
(edited re-saves, Google-made -PANO/-EFFECTS without an EXIF date, "(1)" copies)
got separate records stacked on the same point. Same rule as `photos_ingest.py
scan`: same filename (minus a "(n)" suffix) at the same spot within 2h, or — for
camera-style names — at the same spot or within ~26h. Date-only names
("2019-09-29(1)", Google collages) need the same spot within 2h.

Which record survives: hidden in the app (so it stays hidden) > timestamp closest
to the time in the filename > has a timestamp > 360 flag > no "(n)" suffix >
source file present > first in the manifest. Only manifest records are removed;
photo files are never touched.

    python dedupe_photos.py            # dry run: counts + samples
    python dedupe_photos.py --apply    # write manifests (then build/upload/deploy)
"""
import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
sys.path.insert(0, str(HERE))
from photos_ingest import RE_DUP_SUFFIX, RE_NAME_TIME  # noqa: E402


def secs(ts):
    try:
        return datetime.fromisoformat(ts).timestamp()
    except (TypeError, ValueError):
        return None


def same_photo(a, b):
    pa, pb = a["properties"], b["properties"]
    (x1, y1), (x2, y2) = a["geometry"]["coordinates"][:2], b["geometry"]["coordinates"][:2]
    spot = abs(x1 - x2) < 2e-4 and abs(y1 - y2) < 2e-4
    ta, tb = secs(pa.get("timestamp")), secs(pb.get("timestamp"))
    dt = abs(ta - tb) if ta is not None and tb is not None else None
    if spot and (dt is None or dt <= 7200):
        return True
    base = RE_DUP_SUFFIX.sub("", pa["filename"])
    unique_name = not re.fullmatch(r"\d{4}-\d{2}-\d{2}", base)
    return unique_name and (spot or (dt is not None and dt <= 26 * 3600))


def name_time_gap(p):
    """Hours between the record's timestamp and the time in its filename
    (IMG_20150313_121205); 1e9 when either is missing."""
    m = RE_NAME_TIME.search(p["filename"])
    t = secs(p.get("timestamp"))
    if not m or t is None:
        return 1e9
    d, hms = m.group(1), m.group(2)
    n = secs(f"{d[:4]}-{d[4:6]}-{d[6:]}T{hms[:2]}:{hms[2:4]}:{hms[4:]}")
    return round(abs(t - n) / 3600, 2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--hidden", type=Path, help="JSON list of hidden photo ids (from D1)")
    args = ap.parse_args()

    fc = json.loads((OUT / "photos.geojson").read_text(encoding="utf-8"))
    src = json.loads((OUT / "photos.src.json").read_text(encoding="utf-8"))
    hidden = set(json.loads(args.hidden.read_text())) if args.hidden else set()
    feats = fc["features"]

    groups_by_name = defaultdict(list)
    for i, f in enumerate(feats):
        groups_by_name[RE_DUP_SUFFIX.sub("", f["properties"]["filename"]).lower()].append(i)

    parent = list(range(len(feats)))
    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for idx in groups_by_name.values():
        for a in range(len(idx)):
            for b in range(a + 1, len(idx)):
                if same_photo(feats[idx[a]], feats[idx[b]]):
                    parent[find(idx[a])] = find(idx[b])

    clusters = defaultdict(list)
    for i in range(len(feats)):
        clusters[find(i)].append(i)
    clusters = [c for c in clusters.values() if len(c) > 1]

    def rank(i):
        p = feats[i]["properties"]
        return (p["id"] not in hidden, name_time_gap(p), not p.get("timestamp"),
                not p.get("is_360"), bool(RE_DUP_SUFFIX.search(p["filename"])),
                not os.path.exists(src.get(p["id"], "") or "\0"), i)

    drop = set(); moved = []; hidden_conflict = 0
    for c in clusters:
        keep = min(c, key=rank)
        ids = {feats[i]["properties"]["id"] for i in c}
        if len(ids & hidden) not in (0, len(ids)):
            hidden_conflict += 1
        for i in c:
            if i == keep:
                continue
            drop.add(i)
            (x1, y1), (x2, y2) = feats[i]["geometry"]["coordinates"][:2], feats[keep]["geometry"]["coordinates"][:2]
            if abs(x1 - x2) >= 2e-4 or abs(y1 - y2) >= 2e-4:
                moved.append((feats[i]["properties"], feats[keep]["properties"]))

    print(f"records: {len(feats):,} | duplicate groups: {len(clusters):,} | "
          f"records to remove: {len(drop):,} | groups with mixed hidden state: {hidden_conflict}")
    print(f"removed copies at a different spot than the kept one: {len(moved)}")
    for a, b in moved[:15]:
        print(f"   drop {a['filename']} {a['timestamp']}  keep {b['filename']} {b['timestamp']}")
    for c in clusters[:6]:
        keep = min(c, key=rank)
        print("  group:", [(feats[i]["properties"]["filename"], feats[i]["properties"]["timestamp"],
                            "KEEP" if i == keep else "drop") for i in c])

    if not args.apply:
        print("dry run — re-run with --apply to write")
        return
    drop_ids = {feats[i]["properties"]["id"] for i in drop}
    fc["features"] = [f for i, f in enumerate(feats) if i not in drop]
    for pid in drop_ids:
        src.pop(pid, None)
    (OUT / "photos.geojson").write_text(json.dumps(fc), encoding="utf-8")
    (OUT / "photos.src.json").write_text(json.dumps(src), encoding="utf-8")
    (OUT / "dedupe_removed.json").write_text(json.dumps(sorted(drop_ids)), encoding="utf-8")
    print(f"wrote manifest: {len(fc['features']):,} records; removed ids -> out/dedupe_removed.json")


if __name__ == "__main__":
    main()
