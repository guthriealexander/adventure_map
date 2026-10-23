#!/usr/bin/env python3
"""Classify hike/trip tracks as drive vs hike and map-match them onto the
Overture (OSM-derived) road + trail network.

Pipeline per track:
  1. corridor   pull network segments within ~150 m of the track from a local
                Overture segments GeoParquet (fetched once per region with DuckDB)
  2. classify   share of resampled track points lying on drivable roads vs
                foot/cycle paths -> drive | hike | uncertain
  3. match      HMM map matching (Viterbi): candidates = projections onto nearby
                network edges; emission = GPS distance; transition = how well the
                network route distance agrees with the straight-line distance.
                Consecutive matches are joined with shortest paths on the graph.
  4. gaps       stretches with no network nearby (cross-country, trail missing from
                OSM, GPS jumps) keep the original GPS geometry.

Nearest-point snapping is deliberately NOT used: it zigzags between parallel
trails and cuts corners at junctions; matching produces a connected route.

    python snap_tracks.py --raw out/_snap/co_raw.geojson \
        --segments out/_snap/co_segments.parquet --out out/_snap/co_snapped.geojson
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import defaultdict
from pathlib import Path

import duckdb
import networkx as nx
import numpy as np
import shapely
from shapely.geometry import LineString, Point
from shapely.ops import substring

DRIVE = {"motorway", "trunk", "primary", "secondary", "tertiary", "residential",
         "unclassified", "living_street"}
SHARED = {"service", "track", "unknown"}            # driven and walked
FOOT = {"footway", "path", "steps", "cycleway", "bridleway", "pedestrian"}

PROFILES = {
    #        sample every, candidate radius, GPS sigma, transition beta, allowed classes
    # detour_x/detour_m: a step's network route may be at most gc*x + m long
    "hike":  dict(step=20.0, radius=30.0, sigma=8.0, beta=25.0, detour_x=1.0, detour_m=50.0,
                  max_skip=2, classes=DRIVE | SHARED | FOOT),
    "drive": dict(step=40.0, radius=45.0, sigma=12.0, beta=50.0, detour_x=1.0, detour_m=120.0,
                  max_skip=2, classes=DRIVE | SHARED),
}
CORRIDOR_M = 150.0


# ---------------------------------------------------------------------------
# local metric frame (equirectangular around the track; plenty for <100 km)
# ---------------------------------------------------------------------------
class Frame:
    def __init__(self, lon0, lat0):
        self.lon0, self.lat0 = lon0, lat0
        self.kx = 111320.0 * math.cos(math.radians(lat0))
        self.ky = 110540.0

    def fwd(self, coords):
        a = np.asarray(coords, dtype=float)[:, :2]
        return np.column_stack(((a[:, 0] - self.lon0) * self.kx, (a[:, 1] - self.lat0) * self.ky))

    def inv(self, xy):
        a = np.asarray(xy, dtype=float)
        return np.column_stack((a[:, 0] / self.kx + self.lon0, a[:, 1] / self.ky + self.lat0))


def resample(xy, step):
    """Points every `step` metres along a polyline, plus each point's distance along it."""
    seg = np.hypot(*np.diff(xy, axis=0).T)
    cum = np.concatenate(([0.0], np.cumsum(seg)))
    if cum[-1] == 0:
        return xy[:1], np.array([0.0])
    d = np.arange(0.0, cum[-1], step)
    d = np.append(d, cum[-1])
    x = np.interp(d, cum, xy[:, 0]); y = np.interp(d, cum, xy[:, 1])
    return np.column_stack((x, y)), d


# ---------------------------------------------------------------------------
# network corridor -> graph of connector-to-connector edges
# ---------------------------------------------------------------------------
def load_corridor(con, parquet, lines_ll, corridor_m=CORRIDOR_M):
    xs = [p[0] for l in lines_ll for p in l]; ys = [p[1] for l in lines_ll for p in l]
    pad = 0.003 + corridor_m / 90000.0
    x0, y0, x1, y1 = min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad
    simp = shapely.MultiLineString([l for l in lines_ll]).simplify(0.0002)
    wkt = shapely.to_wkt(simp)
    rows = con.execute(f"""
        SELECT id, class, subclass, connectors, wkb FROM read_parquet(?)
        WHERE xmin < ? AND xmax > ? AND ymin < ? AND ymax > ?
          AND ST_Intersects(ST_GeomFromWKB(wkb), ST_Buffer(ST_GeomFromText(?), ?))
    """, [str(parquet), x1, x0, y1, y0, wkt, corridor_m / 90000.0]).fetchall()
    return rows


NOT_ROADS = {"driveway", "parking_aisle", "drive_through", "sidewalk", "crosswalk"}
SEEN_M = 40.0            # a fix within this distance counts as evidence for an edge

# "roads" profile, for location history that is overwhelmingly vehicle travel: match
# on real roads only, with a search radius that fits accurate (<30 m) fixes
ROADS_OPTS = {"classes": DRIVE | {"service", "track", "unknown"}, "exclude_subclasses": NOT_ROADS,
              "radius": 50.0, "sigma": 15.0, "adaptive": True}


def build_graph(rows, frame, classes, exclude_subclasses=frozenset()):
    G = nx.Graph()
    edges = []           # (u, v, LineString xy, class, segment id)
    for sid, cls, subcls, connectors, wkb in rows:
        if cls not in classes or subcls in exclude_subclasses:
            continue
        geom = shapely.from_wkb(bytes(wkb))
        if geom.geom_type != "LineString" or geom.is_empty:
            continue
        line = LineString(frame.fwd(geom.coords))
        if line.length == 0:
            continue
        cons = sorted(((c["at"], c["connector_id"]) for c in (connectors or [])), key=lambda t: t[0])
        if not cons or cons[0][0] > 1e-9:
            cons.insert(0, (0.0, f"{sid}:start"))
        if cons[-1][0] < 1 - 1e-9:
            cons.append((1.0, f"{sid}:end"))
        for (a0, c0), (a1, c1) in zip(cons, cons[1:]):
            if a1 - a0 <= 1e-9:
                continue
            part = substring(line, a0, a1, normalized=True)
            if part.length < 0.01 or part.geom_type != "LineString":
                continue
            k = len(edges)
            edges.append((c0, c1, part, cls if subcls != "sidewalk" else "footway", sid))
            # parallel edges between the same connectors: keep the shortest in the graph
            if not G.has_edge(c0, c1) or G[c0][c1]["length"] > part.length:
                G.add_edge(c0, c1, length=part.length, k=k)
    return G, edges


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------
def classify(samples, edges, tree, total_len, name, tol=20.0):
    if "Directions from" in (name or ""):
        return "drive", 1.0, {}
    near = {"drive": 0, "shared": 0, "foot": 0, "none": 0}
    for pt in samples:
        idx = tree.query(Point(pt), predicate="dwithin", distance=tol)
        if len(idx) == 0:
            near["none"] += 1; continue
        d = [(edges[i][2].distance(Point(pt)), edges[i][3]) for i in idx]
        cls = min(d)[1]
        near["drive" if cls in DRIVE else "foot" if cls in FOOT else "shared"] += 1
    n = max(1, len(samples))
    fr = {k: v / n for k, v in near.items()}
    km = total_len / 1000
    if fr["drive"] >= 0.7 and fr["foot"] <= 0.1 and km >= 3:
        return "drive", round(fr["drive"], 2), fr
    if fr["drive"] >= 0.45 and fr["foot"] <= 0.2 and km >= 3:
        return "uncertain", round(fr["drive"], 2), fr
    return "hike", round(1 - fr["drive"], 2), fr


# ---------------------------------------------------------------------------
# HMM map matching
# ---------------------------------------------------------------------------
class Router:
    def __init__(self, G):
        self.G = G; self.cache = {}

    def dist(self, u, cutoff):
        hit = self.cache.get(u)
        if hit and hit[0] >= cutoff:
            return hit[1]
        d = nx.single_source_dijkstra_path_length(self.G, u, cutoff=cutoff, weight="length")
        self.cache[u] = (cutoff, d)
        return d


def candidates(pt, edges, tree, G, prof):
    P = Point(pt)
    idx = tree.query(P, predicate="dwithin", distance=prof["radius"])
    out = []
    for i in idx:
        u, v, line, cls, _ = edges[i]
        if G[u][v]["k"] != i:           # parallel duplicate not in graph
            continue
        off = line.project(P); d = line.distance(P)
        out.append((d, i, off))
    out.sort()
    return out[:6]


def route_len(a, b, edges, router, cutoff):
    (_, ia, oa), (_, ib, ob) = a, b
    ua, va, la, *_ = edges[ia]; ub, vb, lb, *_ = edges[ib]
    if ia == ib:
        return abs(ob - oa)
    best = math.inf
    for na, da in ((ua, oa), (va, la.length - oa)):
        if da > cutoff:
            continue
        dist = router.dist(na, cutoff)
        for nb, db in ((ub, ob), (vb, lb.length - ob)):
            if nb in dist and dist[nb] <= cutoff:   # cache may hold a longer search
                best = min(best, da + dist[nb] + db)
    return best


def viterbi(samples, cands, edges, router, prof):
    """Returns list of chains; each chain = list of (sample_index, candidate)."""
    chains, cur = [], None           # cur: (scores list, back pointers list, sample idx list)
    def close():
        if not cur:
            return
        scores, backs, sidx = cur
        j = int(np.argmin(scores[-1]))
        path = []
        for t in range(len(sidx) - 1, -1, -1):
            path.append((sidx[t], cands[sidx[t]][j]))
            if t:
                j = backs[t][j]
        chains.append(path[::-1])
    misses = 0
    for t, cs in enumerate(cands):
        if not cs:
            # an isolated stray fix (overlook, GPS spike) shouldn't end the route;
            # a longer run of off-network samples is a real gap
            if cur is not None and misses < prof["max_skip"]:
                misses += 1; continue
            close(); cur = None; continue
        misses = 0
        emis = [0.5 * (c[0] / prof["sigma"]) ** 2 for c in cs]
        if cur is None:
            cur = ([emis], [None], [t]); continue
        scores, backs, sidx = cur
        prev_t = sidx[-1]; prev_c = cands[prev_t]
        gc = float(np.hypot(*(samples[t] - samples[prev_t])))
        cutoff = max(gc * 3 + 150, gc * prof["detour_x"] + prof["detour_m"])
        new_s, new_b = [], []
        for c, e in zip(cs, emis):
            best, arg = math.inf, -1
            for j, p in enumerate(prev_c):
                if scores[-1][j] == math.inf:
                    continue
                r = route_len(p, c, edges, router, cutoff)
                if r > gc * prof["detour_x"] + prof["detour_m"]:
                    continue    # implausible loop between fixes (parallel street/sidewalk hop)
                s = scores[-1][j] + abs(r - gc) / prof["beta"]
                if s < best:
                    best, arg = s, j
            new_s.append(best + e if arg >= 0 else math.inf); new_b.append(arg)
        if all(s == math.inf for s in new_s):     # network break: finish chain, restart here
            close(); cur = ([emis], [None], [t]); continue
        scores.append(new_s); backs.append(new_b); sidx.append(t)
    close()
    return chains


def chain_geometry(chain, edges, G):
    """Stitch matched candidates into a route along the network.
    Returns ([(coords, class)] pieces in travel order, [edge indexes used]), or None."""
    pieces, used = [], []

    def add(line, cls):
        cs = list(line.coords)
        if len(cs) < 2 or line.length < 0.01:
            return
        if pieces and math.dist(pieces[-1][0][-1], cs[0]) < 0.5:
            if pieces[-1][1] == cls:          # extend the same-class piece
                pieces[-1][0].extend(cs[1:])
                return
            cs = [pieces[-1][0][-1]] + cs[1:]
        pieces.append((cs, cls))

    for (_, a), (_, b) in zip(chain, chain[1:]):
        _, ia, oa = a; _, ib, ob = b
        ua, va, la, ca, sa = edges[ia]; ub, vb, lb, cb, sb = edges[ib]
        if ia == ib:
            if abs(ob - oa) > 0.01:
                add(substring(la, oa, ob), ca); used.append(ia)
            continue
        best = None
        for na, pa in ((ua, substring(la, oa, 0)), (va, substring(la, oa, la.length))):
            for nb, pb in ((ub, substring(lb, 0, ob)), (vb, substring(lb, lb.length, ob))):
                try:
                    nodes = nx.shortest_path(G, na, nb, weight="length")
                except nx.NetworkXNoPath:
                    continue
                L = pa.length + pb.length + sum(G[x][y]["length"] for x, y in zip(nodes, nodes[1:]))
                if best is None or L < best[0]:
                    best = (L, pa, nodes, pb)
        if best is None:
            continue
        _, pa, nodes, pb = best
        add(pa, ca); used.append(ia)
        for x, y in zip(nodes, nodes[1:]):
            k = G[x][y]["k"]
            u, _, line, cls, _sid = edges[k]
            add(line if u == x else LineString(line.coords[::-1]), cls); used.append(k)
        add(pb, cb); used.append(ib)
    return (pieces, used) if pieces else None


MIN_DRIVE_RUN = {"sparse": 1500.0,   # road stretches shorter than this = walking a road
                 "dense": 5000.0}    # dense = watch/phone activity: road km are usually on foot
MIN_HIKE_RUN = {"sparse": 1000.0,    # sparse fixes drift onto footways beside the road
                "dense": 200.0}      # dense: a short foot stretch is a parking lot / pullout
DRIVE_HINTS = {"IN_PASSENGER_VEHICLE", "IN_BUS", "MOTORCYCLING", "IN_TRAIN", "IN_FERRY", "drive"}
FOOT_HINTS = {"WALKING", "RUNNING", "HIKING", "CYCLING", "IN_SUBWAY", "hike", "foot"}
FOOT_TYPES = ("hik", "walk", "run", "trail", "ski", "snowshoe", "climb", "mountaineer")


def assign_modes(items, default, min_drive, min_hike=200.0):
    """items: ordered [{kind: snap|raw, coords, cls, len}] -> sets item['mode'].
    Snapped pieces take the mode of the network they run on (shared service/track
    roads inherit from neighbours), short runs are absorbed into their
    surroundings, and raw gaps between two drive stretches are drives."""
    snaps = [it for it in items if it["kind"] == "snap"]
    for it in snaps:
        it["mode"] = "drive" if it["cls"] in DRIVE else "hike" if it["cls"] in FOOT else None
    known = [i for i, it in enumerate(snaps) if it["mode"]]
    for i, it in enumerate(snaps):
        if it["mode"] is None:
            prev = [j for j in known if j < i]; nxt = [j for j in known if j > i]
            it["mode"] = (snaps[prev[-1]]["mode"] if prev else
                          snaps[nxt[0]]["mode"] if nxt else default)
    for mode, limit, other in (("drive", min_drive, "hike"), ("hike", min_hike, "drive")):
        runs, i = [], 0
        while i < len(snaps):
            j = i
            while j < len(snaps) and snaps[j]["mode"] == snaps[i]["mode"]:
                j += 1
            runs.append((i, j))
            i = j
        # a lone run is kept, except a short "drive" in a track that isn't a drive
        if len(runs) < 2 and not (mode == "drive" and default != "drive"):
            continue
        for i, j in runs:
            if snaps[i]["mode"] == mode and sum(it["len"] for it in snaps[i:j]) < limit:
                for it in snaps[i:j]:
                    it["mode"] = other
    for k, it in enumerate(items):
        if it["kind"] != "raw":
            continue
        before = next((x["mode"] for x in reversed(items[:k]) if x["kind"] == "snap"), None)
        after = next((x["mode"] for x in items[k + 1:] if x["kind"] == "snap"), None)
        it["mode"] = "drive" if (before or default) == "drive" and (after or default) == "drive" else "hike"


def match_track(feat, con, parquet, opts=None):
    lines_ll = feat["geometry"]["coordinates"]
    name = feat["properties"].get("name")
    allc = [p for l in lines_ll for p in l]
    frame = Frame(np.mean([p[0] for p in allc]), np.mean([p[1] for p in allc]))
    xy_lines = [frame.fwd(l) for l in lines_ll]
    spacing = np.concatenate([np.hypot(*np.diff(xy, axis=0).T) for xy in xy_lines])
    total = float(spacing.sum())
    # Sparse tracks (points hundreds of metres apart): the straight chords between
    # fixes stray far from the real road, so match only the GPS fixes themselves,
    # with a wider search and a corridor that scales with the gaps.
    sparse = float(np.median(spacing)) > 60.0
    # the real road can bow far from the straight chord between distant fixes, so the
    # corridor scales with the gaps (capped so city queries stay small)
    corridor = min(6000.0, max(CORRIDOR_M, 0.5 * float(np.percentile(spacing, 90)))) if sparse else CORRIDOR_M
    rows = load_corridor(con, parquet, lines_ll, corridor)

    # classify with the full network (or only what the profile allows)
    opts = opts or {}
    G_all, E_all = build_graph(rows, frame, opts.get("classes", DRIVE | SHARED | FOOT),
                               opts.get("exclude_subclasses", frozenset()))
    tree_all = shapely.STRtree([e[2] for e in E_all]) if E_all else None
    if sparse:
        probe = np.vstack(xy_lines)
    else:
        probe = np.vstack([resample(xy, 50.0)[0] for xy in xy_lines])
    if tree_all is None:
        return "hike", 0.0, {}, {("raw", "hike"): [l for l in lines_ll]}, 0.0, {}
    hint = feat["properties"].get("mode")
    hint = "drive" if hint in DRIVE_HINTS else "hike" if hint in FOOT_HINTS else None
    if hint:
        category, conf, fr = hint, 1.0, {"hint": 1.0}
    else:
        category, conf, fr = classify(probe, E_all, tree_all, total, name, tol=60.0 if sparse else 20.0)
    prof = dict(PROFILES["drive" if category in ("drive", "uncertain") and not sparse else "hike"])
    if sparse:
        # sparse trips are often drive-to-trailhead + hike, so match on the full
        # network. Fixes km apart on switchback roads can be 3-4x the chord; the
        # drawn route is always the shortest path between matches, so allow that.
        prof.update(radius=90.0, sigma=30.0, beta=300.0, detour_x=4.0, detour_m=1500.0, max_skip=1)
    if opts.get("classes"):
        prof["classes"] = opts["classes"]
    for k in ("radius", "sigma"):
        if k in opts:
            prof[k] = opts[k]
    if opts.get("adaptive") and sparse:
        # detour tolerance follows how far apart the fixes actually are: legacy history
        # is ~170 m apart, so the km-scale allowances for very sparse tracks let routes
        # wander into side streets and back between fixes
        med = float(np.median(spacing))
        prof.update(beta=min(300.0, max(40.0, 0.5 * med)),
                    detour_x=2.0 if med < 500 else 4.0,
                    detour_m=min(1500.0, max(150.0, 1.5 * med)))

    if prof["classes"] == PROFILES["hike"]["classes"] or opts.get("classes"):
        G, E, tree = G_all, E_all, tree_all
    else:
        G, E = build_graph(rows, frame, prof["classes"])
        tree = shapely.STRtree([e[2] for e in E]) if E else None
    router = Router(G)
    items, matched, nsamp, edges_used = [], 0, 0, {}
    for xy, ll in zip(xy_lines, lines_ll):
        if sparse:
            samples = xy
            dist = np.concatenate(([0.0], np.cumsum(np.hypot(*np.diff(xy, axis=0).T))))
        else:
            samples, dist = resample(xy, prof["step"])
        nsamp += len(samples)
        cands = [candidates(p, E, tree, G, prof) if tree is not None else [] for p in samples]
        chains = viterbi(samples, cands, E, router, prof)
        spans = []                       # (first sample, last sample, pieces)
        for ch in chains:
            if len(ch) < 2:
                continue
            got = chain_geometry(ch, E, G)
            if got:
                pieces, used = got
                spans.append((ch[0][0], ch[-1][0], pieces))
                fixes = shapely.MultiPoint(samples[ch[0][0]:ch[-1][0] + 1])
                for k in used:
                    u, v, line, cls, sid = E[k]
                    key = f"{sid}|{u}|{v}"
                    # "seen": a GPS fix actually landed near this edge, vs. the edge only
                    # being inferred by routing between two fixes
                    seen = bool(line.distance(fixes) <= SEEN_M)
                    if key not in edges_used:
                        edges_used[key] = {"cls": cls, "seen": seen,
                                           "coords": frame.inv(line.coords).round(6).tolist()}
                    elif seen:
                        edges_used[key]["seen"] = True
        spans.sort(key=lambda t: t[0])
        covered = np.zeros(len(samples), bool)
        for a, b, _ in spans:
            covered[a:b + 1] = True
        matched += int(covered.sum())
        # walk the line in order: snapped spans, and raw GPS for the stretches between
        # them (cut by distance along the track so a long GPS jump isn't repeated)
        raw_line = LineString(xy) if len(xy) > 1 else None

        def raw(s0, t0):
            if raw_line is None:
                return
            piece = substring(raw_line, float(dist[max(s0 - 1, 0)]), float(dist[min(t0, len(samples) - 1)]))
            if piece.geom_type == "LineString" and piece.length > 0.5:
                items.append({"kind": "raw", "coords": list(piece.coords), "len": piece.length})
        t = 0
        for a, b, pieces in spans:
            if a > t:
                raw(t, a)
            for cs, cls in pieces:
                items.append({"kind": "snap", "coords": cs, "cls": cls,
                              "len": float(LineString(cs).length)})
            t = max(t, b + 1)
        if t < len(samples):
            raw(t, len(samples))
    atype = (feat["properties"].get("type") or "").lower()
    if hint == "hike" or any(k in atype for k in FOOT_TYPES):   # recorded as a foot activity
        category = "hike"
        for it in items:
            if it["kind"] == "snap":
                it["cls"] = "path"
    elif hint == "drive":                                       # recorded as vehicle travel
        for it in items:
            if it["kind"] == "snap" and it["cls"] in FOOT:
                it["cls"] = "service"                           # parallel path beside the road
    band = "sparse" if sparse else "dense"
    assign_modes(items, "drive" if category == "drive" else "hike",
                 MIN_DRIVE_RUN[band], MIN_HIKE_RUN[band])
    parts = defaultdict(list)
    km = defaultdict(float)
    for it in items:
        parts[(it["kind"], it["mode"])].append(frame.inv(it["coords"]).round(7).tolist())
        km[it["mode"]] += it["len"] / 1000
    tot = sum(km.values()) or 1.0
    category = ("drive" if km["drive"] / tot >= 0.8 else
                "hike" if km["hike"] / tot >= 0.8 else "mixed")
    fr = dict(fr, km_drive=round(km["drive"], 3), km_hike=round(km["hike"], 3))
    return category, conf, fr, parts, matched / max(1, nsamp), edges_used


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", type=Path, required=True)
    ap.add_argument("--segments", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", type=int, nargs="*", help="src_idx values to run")
    args = ap.parse_args()

    feats = json.loads(args.raw.read_text(encoding="utf-8"))["features"]
    if args.only:
        feats = [f for f in feats if f["properties"]["src_idx"] in set(args.only)]
    if args.limit:
        feats = feats[:args.limit]
    con = duckdb.connect()
    con.execute("LOAD spatial")
    out, report = [], []
    t0 = time.time()
    for n, f in enumerate(feats, 1):
        p = f["properties"]; t1 = time.time()
        try:
            cat, conf, fr, parts, pct, _edges = match_track(f, con, args.segments)
        except Exception as e:  # keep going; report the failure
            print(f"  ! {p['src_idx']}: {e!r}", file=sys.stderr)
            cat, conf, fr, pct = "error", 0, {}, 0
            parts = {("raw", "hike"): f["geometry"]["coordinates"]}
        base = {"src_idx": p["src_idx"], "name": p.get("name"), "category": cat,
                "confidence": conf, "matched_pct": round(pct * 100, 1)}
        for (kind, mode), lines in parts.items():
            out.append({"type": "Feature", "properties": {**base, "part": "snapped" if kind == "snap" else "raw",
                                                          "mode": mode},
                        "geometry": {"type": "MultiLineString", "coordinates": lines}})
        report.append({**base, **{f"near_{k}": round(v, 2) for k, v in fr.items()},
                       "secs": round(time.time() - t1, 1)})
        if n % 25 == 0 or n == len(feats):
            print(f"  {n}/{len(feats)} tracks, {time.time() - t0:.0f}s", flush=True)
    args.out.write_text(json.dumps({"type": "FeatureCollection", "features": out}), encoding="utf-8")
    args.out.with_suffix(".report.json").write_text(json.dumps(report, indent=0), encoding="utf-8")
    cats = defaultdict(int)
    for r in report:
        cats[r["category"]] += 1
    pcts = sorted(r["matched_pct"] for r in report)
    print(f"done: {dict(cats)} | matched% p10/p50/p90: "
          f"{pcts[len(pcts)//10]}/{pcts[len(pcts)//2]}/{pcts[len(pcts)*9//10]} -> {args.out}")


if __name__ == "__main__":
    main()
