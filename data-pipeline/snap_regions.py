#!/usr/bin/env python3
"""Run snap_tracks.py over every track in the Hikes layer, worldwide.

Never modifies the originals (out/hikes.geojson[l], hikes.pmtiles, the source
GeoPackage); everything is written under out/_snap/ (originals backed up to
out/_snap/originals/).

  plan    tracks -> 0.05° cells they pass through (+1 cell margin) -> 2° blocks
  fetch   per block, pull Overture road segments inside those cells from S3 with
          DuckDB (cells touched only by Google "Directions from" routes get
          drivable classes only), then combine into one sorted local parquet
  match   map-match every track in parallel (long routes split into ~40 km chunks)
  merge   one snapped GeoJSON + per-track report for all tracks

    python snap_regions.py plan|fetch|match|merge|all [--workers 6]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
SNAP = OUT / "_snap"
NET = SNAP / "net"
SRC = SNAP / "originals" / "hikes.geojson"
RELEASE = "2026-08-19.0"
S3 = f"s3://overturemaps-us-west-2/release/{RELEASE}/theme=transportation/type=segment/*"
CELL = 0.05
BLOCK = 2.0
CHUNK_M = 40000.0
DRIVABLE_SQL = ("'motorway','trunk','primary','secondary','tertiary','residential',"
                "'unclassified','living_street','service','track','unknown'")


TAG = "hikes"          # set from --tag; keeps each dataset's files apart


def paths():
    t = "" if TAG == "hikes" else f"_{TAG}"
    return {"plan": SNAP / f"plan{t}.json", "net": SNAP / f"net{t}", "results": SNAP / f"match_results{t}.json",
            "segments": SNAP / f"segments{t}.parquet", "snapped": SNAP / f"{'all' if TAG == 'hikes' else TAG}_snapped.geojson",
            "report": SNAP / f"{'all' if TAG == 'hikes' else TAG}_snapped.report.json",
            "raw": SNAP / f"{'all' if TAG == 'hikes' else TAG}_raw.geojson"}


def load_tracks(src=None):
    fs = json.loads(Path(src or SRC).read_text(encoding="utf-8"))["features"]
    out = []
    for i, f in enumerate(fs):
        ls = [l for l in f["geometry"]["coordinates"] if len(l) > 1]
        if not ls:
            continue
        p = dict(f["properties"]); p.setdefault("src_idx", i)
        out.append({"type": "Feature", "properties": p,
                    "geometry": {"type": "MultiLineString", "coordinates": ls}})
    return out


def is_directions(f):
    return "Directions from" in (f["properties"].get("name") or "")


# ---------------------------------------------------------------------------
def cmd_plan(args):
    tracks = load_tracks(args.src)
    cells = {}                                   # (cx, cy) -> needs foot classes?
    for f in tracks:
        foot = not is_directions(f)
        for l in f["geometry"]["coordinates"]:
            prev = None
            for x, y, *_ in l:
                # densify long gaps so sparse tracks don't skip cells
                pts = [(x, y)]
                if prev:
                    n = int(max(abs(x - prev[0]), abs(y - prev[1])) / (CELL / 2))
                    pts = [(prev[0] + (x - prev[0]) * k / (n + 1), prev[1] + (y - prev[1]) * k / (n + 1))
                           for k in range(1, n + 1)] + pts
                for px, py in pts:
                    cx, cy = math.floor(px / CELL), math.floor(py / CELL)
                    for dx in (-1, 0, 1):
                        for dy in (-1, 0, 1):
                            k = (cx + dx, cy + dy)
                            cells[k] = cells.get(k, False) or foot
                prev = (x, y)
    blocks = defaultdict(list)
    for (cx, cy), foot in cells.items():
        bx, by = math.floor(cx * CELL / BLOCK), math.floor(cy * CELL / BLOCK)
        blocks[(bx, by)].append((cx, cy, foot))
    if args.skip_cells_from:                      # cells already downloaded for another set
        have = {(c[0], c[1]) for b in json.loads(Path(args.skip_cells_from).read_text())["blocks"]
                for c in b["cells"]}
        before = len(cells)
        cells = {k: v for k, v in cells.items() if k not in have}
        blocks = defaultdict(list)
        for (cx, cy), foot in cells.items():
            blocks[(math.floor(cx * CELL / BLOCK), math.floor(cy * CELL / BLOCK))].append((cx, cy, foot))
        print(f"  {before - len(cells):,} cells already downloaded, {len(cells):,} new")
    plan = {"cell": CELL, "block": BLOCK, "tracks": len(tracks),
            "blocks": [{"bx": bx, "by": by, "cells": c} for (bx, by), c in sorted(blocks.items())]}
    paths()["plan"].write_text(json.dumps(plan), encoding="utf-8")
    nfoot = sum(1 for v in cells.values() if v)
    print(f"tracks {len(tracks):,} | cells {len(cells):,} ({nfoot:,} with trails, "
          f"{len(cells) - nfoot:,} drive-only) | blocks {len(blocks)}")


# ---------------------------------------------------------------------------
def fetch_block(b):
    import duckdb
    global TAG
    TAG = b.get("_tag", TAG)
    dst = paths()["net"] / f"block_{b['bx']}_{b['by']}.parquet"
    if dst.exists():
        return dst.name, "cached", 0
    t = time.time()
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; INSTALL spatial; LOAD spatial; SET s3_region='us-west-2';")
    con.execute("CREATE TABLE cells(cx INTEGER, cy INTEGER, foot BOOLEAN)")
    con.executemany("INSERT INTO cells VALUES (?, ?, ?)", [tuple(c) for c in b["cells"]])
    x0 = min(c[0] for c in b["cells"]) * CELL; x1 = (max(c[0] for c in b["cells"]) + 1) * CELL
    y0 = min(c[1] for c in b["cells"]) * CELL; y1 = (max(c[1] for c in b["cells"]) + 1) * CELL
    tmp = dst.with_suffix(".tmp")
    con.execute(f"""
        COPY (
          SELECT s.id, s.class, s.subclass, s.names.primary AS name, s.connectors,
                 ST_AsWKB(s.geometry) AS wkb, s.bbox.xmin AS xmin, s.bbox.ymin AS ymin,
                 s.bbox.xmax AS xmax, s.bbox.ymax AS ymax
          FROM read_parquet('{S3}', hive_partitioning=1) s
          JOIN cells c
            ON c.cx = CAST(floor((s.bbox.xmin + s.bbox.xmax) / 2 / {CELL}) AS INTEGER)
           AND c.cy = CAST(floor((s.bbox.ymin + s.bbox.ymax) / 2 / {CELL}) AS INTEGER)
          WHERE s.subtype = 'road'
            AND s.bbox.xmin < {x1} AND s.bbox.xmax > {x0} AND s.bbox.ymin < {y1} AND s.bbox.ymax > {y0}
            AND (c.foot OR s.class IN ({DRIVABLE_SQL}))
        ) TO '{tmp.as_posix()}' (FORMAT parquet)""")
    n = con.execute(f"SELECT count(*) FROM read_parquet('{tmp.as_posix()}')").fetchone()[0]
    con.close()
    tmp.replace(dst)
    return dst.name, n, round(time.time() - t)


def cmd_fetch(args):
    P = paths()
    P["net"].mkdir(parents=True, exist_ok=True)
    plan = json.loads(P["plan"].read_text(encoding="utf-8"))
    blocks = [dict(b, _tag=TAG) for b in plan["blocks"]]
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=args.fetch_workers) as ex:
        futs = {ex.submit(fetch_block, b): b for b in blocks}
        for k, fut in enumerate(as_completed(futs), 1):
            try:
                name, n, secs = fut.result()
                print(f"  [{k}/{len(blocks)}] {name}: {n} segments ({secs}s)", flush=True)
            except Exception as e:
                b = futs[fut]
                print(f"  ! block {b['bx']},{b['by']}: {e!r}", flush=True)
    # one local file, sorted so row-group stats prune bbox lookups well
    import duckdb
    con = duckdb.connect()
    allf = P["segments"]
    con.execute(f"""COPY (
        SELECT DISTINCT ON (id) * FROM read_parquet('{P["net"].as_posix()}/block_*.parquet')
        ORDER BY floor(ymin * 4), xmin
      ) TO '{allf.as_posix()}' (FORMAT parquet, ROW_GROUP_SIZE 20000)""")
    n = con.execute(f"SELECT count(*) FROM '{allf.as_posix()}'").fetchone()[0]
    print(f"fetch done in {time.time() - t0:.0f}s: {n:,} segments -> {allf} "
          f"({allf.stat().st_size / 1e6:.0f} MB)")


# ---------------------------------------------------------------------------
def split_track(f):
    """Long routes -> ~40 km chunks (sharing a vertex) so corridors stay small."""
    sys.path.insert(0, str(HERE))
    from snap_tracks import Frame
    import numpy as np
    lines = f["geometry"]["coordinates"]
    allc = [p for l in lines for p in l]
    fr = Frame(sum(p[0] for p in allc) / len(allc), sum(p[1] for p in allc) / len(allc))
    total = sum(float(np.hypot(*np.diff(fr.fwd(l), axis=0).T).sum()) for l in lines)
    if total <= CHUNK_M * 1.5:
        return [f]
    chunks, cur, cur_len = [], [], 0.0
    for l in lines:
        seg = [l[0]]
        for a, b in zip(l, l[1:]):
            local = Frame(a[0], a[1])
            d = float(np.hypot(*(local.fwd([b])[0] - local.fwd([a])[0])))
            seg.append(b); cur_len += d
            if cur_len >= CHUNK_M:
                cur.append(seg); chunks.append(cur)
                cur, cur_len, seg = [], 0.0, [b]
        if len(seg) > 1:
            cur.append(seg)
    if cur:
        chunks.append(cur)
    return [{"type": "Feature", "properties": dict(f["properties"], chunk=k),
             "geometry": {"type": "MultiLineString", "coordinates": c}} for k, c in enumerate(chunks)]


def match_worker(batch, segments, keep_edges=False, profile=None):
    sys.path.insert(0, str(HERE))
    import duckdb
    from snap_tracks import match_track, ROADS_OPTS
    opts = ROADS_OPTS if profile == "roads" else None
    con = duckdb.connect()
    con.execute("LOAD spatial")
    res = []
    for f in batch:
        t = time.time()
        try:
            cat, conf, fr, parts, pct, edges = match_track(f, con, segments, opts)
            err = None
        except Exception as e:
            cat, conf, fr, pct, err = "error", 0, {}, 0, repr(e)
            parts = {("raw", "hike"): f["geometry"]["coordinates"]}
            edges = {}
        res.append({"props": f["properties"], "category": cat, "fr": fr, "pct": pct, "err": err,
                    **({"edges": sorted(edges)} if keep_edges else {}),
                    **({"edge_geom": {k: v for k, v in edges.items()}} if keep_edges else {}),
                    "parts": {f"{k[0]}|{k[1]}": v for k, v in parts.items()},
                    "npts": sum(len(l) for l in f["geometry"]["coordinates"]),
                    "secs": round(time.time() - t, 1)})
    return res


def cmd_match(args):
    P = paths()
    segments = args.segments or str(P["segments"])
    tracks = load_tracks(args.src)
    if args.only:
        tracks = [f for f in tracks if f["properties"]["src_idx"] in set(args.only)]
    work = [c for f in tracks for c in split_track(f)]
    # biggest first so long routes don't all land at the end; small batches
    work.sort(key=lambda f: -sum(len(l) for l in f["geometry"]["coordinates"]))
    batches = [work[i:i + 8] for i in range(0, len(work), 8)]
    print(f"{len(tracks):,} tracks -> {len(work):,} pieces, {len(batches)} batches, {args.workers} workers")
    results, t0, done = [], time.time(), 0
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(match_worker, b, segments, args.keep_edges, args.profile) for b in batches]
        for fut in as_completed(futs):
            r = fut.result(); results.extend(r); done += len(r)
            if done % 200 < len(r) or done == len(work):
                print(f"  {done:,}/{len(work):,} pieces, {time.time() - t0:.0f}s", flush=True)
    errs = [r for r in results if r["err"]]
    for r in errs[:10]:
        print("  !", r["props"]["src_idx"], r["err"])
    P["results"].write_text(json.dumps(results), encoding="utf-8")
    print(f"match done in {time.time() - t0:.0f}s, errors: {len(errs)}")


def cmd_merge(args):
    P = paths()
    results = json.loads(P["results"].read_text(encoding="utf-8"))
    by = defaultdict(list)
    for r in results:
        by[r["props"]["src_idx"]].append(r)
    feats, report = [], []
    for idx, rs in sorted(by.items()):
        rs.sort(key=lambda r: r["props"].get("chunk", 0))
        p = rs[0]["props"]
        parts = defaultdict(list)
        for r in rs:
            for k, v in r["parts"].items():
                parts[k].extend(v)
        kd = sum(r["fr"].get("km_drive", 0) for r in rs); kh = sum(r["fr"].get("km_hike", 0) for r in rs)
        tot = kd + kh
        cat = ("error" if any(r["err"] for r in rs) else
               "hike" if tot == 0 else                      # nothing near any network
               "drive" if kd / tot >= 0.8 else "hike" if kh / tot >= 0.8 else "mixed")
        npts = sum(r["npts"] for r in rs) or 1
        pct = sum(r["pct"] * r["npts"] for r in rs) / npts
        base = {"src_idx": idx, "name": p.get("name"), "category": cat, "matched_pct": round(pct * 100, 1)}
        if p.get("type"):
            base["type"] = p["type"]
        for k, lines in parts.items():
            kind, mode = k.split("|")
            feats.append({"type": "Feature",
                          "properties": {**base, "part": "snapped" if kind == "snap" else "raw", "mode": mode},
                          "geometry": {"type": "MultiLineString", "coordinates": lines}})
        near = defaultdict(float)
        for r in rs:
            for k2, v in r["fr"].items():
                if not k2.startswith("km_"):
                    near[k2] += v * r["npts"] / npts
        report.append({**base, "near_km_drive": round(kd, 1), "near_km_hike": round(kh, 1),
                       **{f"near_{k2}": round(v, 2) for k2, v in near.items()},
                       "chunks": len(rs), "secs": round(sum(r["secs"] for r in rs), 1)})
    P["snapped"].write_text(json.dumps({"type": "FeatureCollection", "features": feats}), encoding="utf-8")
    P["report"].write_text(json.dumps(report), encoding="utf-8")
    # light copy of the originals for the review page (display only; ~1 m simplify)
    import shapely
    raw = []
    for f in load_tracks(args.src):
        g = shapely.MultiLineString(f["geometry"]["coordinates"]).simplify(0.00001)
        lines = [[[round(x, 6), round(y, 6)] for x, y, *_ in l.coords] for l in getattr(g, "geoms", [g])]
        raw.append({"type": "Feature", "properties": {"src_idx": f["properties"]["src_idx"]},
                    "geometry": {"type": "MultiLineString", "coordinates": lines}})
    P["raw"].write_text(json.dumps({"type": "FeatureCollection", "features": raw}), encoding="utf-8")
    cats = defaultdict(int)
    for r in report:
        cats[r["category"]] += 1
    km = defaultdict(float)
    for r in report:
        km["drive"] += r["near_km_drive"]; km["hike"] += r["near_km_hike"]
    print(f"merged {len(report):,} tracks: {dict(cats)} | drive {km['drive']:,.0f} km, hike {km['hike']:,.0f} km")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["plan", "fetch", "match", "merge", "all"])
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--fetch-workers", type=int, default=4)
    ap.add_argument("--only", type=int, nargs="*")
    ap.add_argument("--src", help="track GeoJSON to process (default: the backed-up hikes layer)")
    ap.add_argument("--tag", default="hikes", help="dataset name; keeps plan/net/output files apart")
    ap.add_argument("--segments", help="segments parquet or glob to match against")
    ap.add_argument("--skip-cells-from", help="plan.json whose cells are already downloaded")
    ap.add_argument("--profile", choices=["roads"],
                    help="roads = match on real roads only (location history)")
    ap.add_argument("--keep-edges", action="store_true",
                    help="also record which network edges each track used (for traversal counts)")
    args = ap.parse_args()
    global TAG
    TAG = args.tag
    steps = ["plan", "fetch", "match", "merge"] if args.cmd == "all" else [args.cmd]
    for s in steps:
        print(f"== {s}", flush=True)
        globals()[f"cmd_{s}"](args)


if __name__ == "__main__":
    main()
