#!/usr/bin/env python3
"""Find trips in the location history, for the map's Trips panel.

Evidence per day (date, lat, lon) comes from photos, Google location history (old
export + new Timeline), Garmin activities and the legacy Garmin points; the undated
hike tracks are dated by matching their start to a legacy Garmin point. A day is
"away" when its evidence reaches more than AWAY_KM from the home in effect that day
(homes.json). Consecutive away days form a trip; up to BRIDGE_DAYS days with no
evidence between them don't split it, a day seen at home does. homes.json's named
trips (a thru-hike, a long road trip) swallow every away day in their span. Hikes and
trail runs on days at home are grouped per home and year ("local hikes").

Writes out/trips_draft.json (detection + stats + photo ids) and out/trips.json (slim,
for the web panel; names and hero photos are filled in by the naming step).

Usage:
    python build_trips.py            # detect -> out/trips_draft.json, prints a per-year summary
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"


def _site_setting(key, default=""):
    """A value from site.env (see site.env.example); a real env var wins."""
    import os
    if key in os.environ:
        return os.environ[key]
    f = HERE / "site.env"
    for line in (f.read_text(encoding="utf-8").splitlines() if f.exists() else []):
        k, _, v = line.strip().partition("=")
        if k.strip() == key and not line.strip().startswith("#"):
            return v.strip()
    return default


# 1 = leave trips with no photos out of the Trips panel (only tracks or location history)
NEED_PHOTOS = _site_setting("AM_TRIPS_NEED_PHOTOS", "0") == "1"
AWAY_KM = 60          # farther than this from home = away
BRIDGE_DAYS = 2       # no-evidence days that don't break a trip
STRAY_KM = 800        # a track this far from the rest of its day's evidence gets its own trip
HOME_TOUCH_KM = 15    # a day with evidence this close to home passed through home
JUMP_KM = 150         # two home-touching days whose away parts are this far apart = two trips
GAP_JUMP_KM = 300     # don't bridge evidence-less days between away places this far apart
FOOT = {"hiking", "trail_running", "cross_country_skiing_ws", "backcountry_skiing", "mountaineering"}


def km(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2)
    return 12742 * math.asin(math.sqrt(a))


def extent(coords):
    xs, ys = [c[0] for c in coords], [c[1] for c in coords]
    return [min(xs), min(ys), max(xs), max(ys)]


def line_km(coords):
    return sum(km(a[1], a[0], b[1], b[0]) for a, b in zip(coords, coords[1:]))


def d(s):
    return date.fromisoformat(s[:10])


def jsonl(name):
    if not (OUT / name).exists():                  # optional sources (no activities yet)
        return
    with open(OUT / name, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def geojson(name):
    if not (OUT / name).exists():                  # e.g. no Google history
        return []
    return json.loads((OUT / name).read_text(encoding="utf-8"))["features"]


# ----- evidence -----------------------------------------------------------------
def gather():
    """day -> list of (lat, lon, kind); plus photos, tracks and hikes keyed by day."""
    ev = defaultdict(list)
    photos = defaultdict(list)
    from private_photos import null_island
    for f in geojson("photos.geojson"):
        p = f["properties"]
        if not p.get("timestamp") or null_island(f):   # 0,0 = zeroed GPS tag, not a place
            continue
        lon, lat = f["geometry"]["coordinates"][:2]
        day = d(p["timestamp"])
        ev[day].append((lat, lon, "photo"))
        photos[day].append({"id": p["id"], "lat": lat, "lon": lon, "alt": p.get("altitude")})
    for f in jsonl("google_old_thinned.geojsonl"):
        t = f["properties"].get("Time")
        if t:
            lon, lat = f["geometry"]["coordinates"][:2]
            ev[d(t)].append((lat, lon, "google"))
    for name in ("google_visits.geojson", "google_activities.geojson"):
        for f in geojson(name):
            g = f["geometry"]
            c = g["coordinates"] if g["type"] == "Point" else g["coordinates"][0]
            ev[d(f["properties"]["start_time"])].append((c[1], c[0], "google"))
    tracks = defaultdict(list)
    for i, f in enumerate(jsonl("activities.geojsonl")):   # import_activities / garmin_sync
        p, cs = f["properties"], f["geometry"]["coordinates"]
        if not p.get("start"):
            continue                               # no timestamps: can't place it in a trip
        day = d(p["start"])
        for c in cs[:: max(1, len(cs) // 20)]:
            ev[day].append((c[1], c[0], "activity"))
        tracks[day].append({"src": "activity", "id": p["activityId"], "name": p["name"], "type": p["type"],
                            "km": p.get("distance_km") or round(line_km(cs), 2),
                            "lat": cs[0][1], "lon": cs[0][0], "bb": extent(cs)})
    # legacy Garmin points (timestamped) -> evidence, and a lookup to date the hikes
    legacy = {}
    for f in jsonl("garmin.geojsonl"):
        t = f["properties"].get("time")
        if not t:
            continue
        lon, lat = f["geometry"]["coordinates"][:2]
        day = d(t)
        ev[day].append((lat, lon, "garmin"))
        legacy[(round(lat, 3), round(lon, 3))] = day
    # hikes.geojsonl mixes walking and driving; hikes_snapped labels each source line
    category = {}
    for f in jsonl("hikes_snapped.geojsonl"):
        q = f["properties"]
        category.setdefault(q["src_idx"], q.get("mode") or q.get("category"))
    # photos by ~300 m cell -> dates, to date hikes no legacy Garmin point matches
    near = defaultdict(list)
    for day, ps in photos.items():
        for ph in ps:
            near[(round(ph["lat"] * 300), round(ph["lon"] * 300))].append(day)
    hikes, undated = defaultdict(list), 0
    fixed = {}                                          # hike_dates.json: manual fixes + Google Fit matches
    if (HERE / "hike_dates.json").exists():
        fixed = {int(k): v["date"] for k, v in
                 json.loads((HERE / "hike_dates.json").read_text(encoding="utf-8"))["dates"].items()}
    from excluded_tracks import excluded_lines
    skip = excluded_lines()                             # recorded but not hiked (excluded_tracks.json)
    for i, f in enumerate(jsonl("hikes.geojsonl")):
        if i in skip:
            continue
        g = f["geometry"]
        lines = [l for l in (g["coordinates"] if g["type"] == "MultiLineString" else [g["coordinates"]]) if l]
        if not lines:
            continue                                    # empty geometry
        first = lines[0][0]
        day = d(fixed[i]) if i in fixed else legacy.get((round(first[1], 3), round(first[0], 3)))
        begin = (f.get("properties") or {}).get("begin")
        if day is None and begin:                       # a line that carries its own date (one-off imports)
            day = d(begin)
        if day is None:                                 # most common photo date along the line
            votes = defaultdict(int)
            for l in lines:
                for c in l[:: max(1, len(l) // 40)]:
                    for dd in near.get((round(c[1] * 300), round(c[0] * 300)), ()):
                        votes[dd] += 1
            day = None
            if votes:                                   # only a clear winner: >=3 photos, >=60% of votes
                best = max(votes, key=votes.get)
                if votes[best] >= 3 and votes[best] >= 0.6 * sum(votes.values()):
                    day = best
        if day is None:
            undated += 1
            continue
        mode = category.get(i, "hike")
        hikes[day].append({"src": "hike", "id": i, "type": "drive" if mode == "drive" else "hike",
                           "km": round(sum(line_km(l) for l in lines), 2), "lat": first[1], "lon": first[0],
                           "bb": extent([c for l in lines for c in l])})
    return ev, photos, tracks, hikes, undated


# ----- homes --------------------------------------------------------------------
def load_homes(ev):
    cfg = json.loads((HERE / "homes.json").read_text(encoding="utf-8"))
    homes = []
    for h in cfg["homes"]:
        h = dict(h, start=date.fromisoformat(h["from"]), end=date.fromisoformat(h["to"]))
        if h.get("infer"):
            # the most common ~5 km cell within 150 km of the given point, over the span
            cells = defaultdict(int)
            for day, pts in ev.items():
                if h["start"] <= day <= h["end"]:
                    for lat, lon, _ in pts:
                        if km(lat, lon, h["lat"], h["lon"]) < 150:
                            cells[(round(lat * 20) / 20, round(lon * 20) / 20)] += 1
            if cells:
                (h["lat"], h["lon"]), _ = max(cells.items(), key=lambda kv: kv[1])
        homes.append(h)
    named = [dict(t, start=date.fromisoformat(t["from"]), end=date.fromisoformat(t["to"]))
             for t in cfg.get("named_trips", [])]
    return homes, named


def home_on(homes, day):
    for h in homes:
        if h["start"] <= day <= h["end"]:
            return h
    return None


# ----- detection ----------------------------------------------------------------
def detect():
    ev, photos, tracks, hikes, undated = gather()
    homes, named = load_homes(ev)
    status = {}                                     # day -> "away" | "home"
    for day, pts in ev.items():
        h = home_on(homes, day)
        if not h:
            continue
        far = max(km(lat, lon, h["lat"], h["lon"]) for lat, lon, _ in pts)
        status[day] = "away" if far > AWAY_KM else "home"

    # Per away day: did it pass through home, and where was the away part (centroid of
    # the evidence beyond AWAY_KM)? Used to split chained trips below.
    touch, where = {}, {}
    for day, pts in ev.items():
        h = home_on(homes, day)
        if not h or status.get(day) != "away":
            continue
        ds = [km(lat, lon, h["lat"], h["lon"]) for lat, lon, _ in pts]
        touch[day] = min(ds) <= HOME_TOUCH_KM
        far = [p for p, x in zip(pts, ds) if x > AWAY_KM]
        where[day] = (sum(p[0] for p in far) / len(far), sum(p[1] for p in far) / len(far))

    def new_trip(prev, day):
        """Consecutive away days that are really two trips: back home in between
        (both days touch home, e.g. a holiday visit -> home on the 30th -> a mountain hike
        on the 31st), or evidence-less days bridged between places far apart (one city,
        two quiet days, then another city across the country)."""
        a, b = where.get(prev), where.get(day)
        if not a or not b:
            return False
        jump = km(a[0], a[1], b[0], b[1])
        if (day - prev).days == 1:
            return touch.get(prev) and touch.get(day) and jump > JUMP_KM
        return jump > GAP_JUMP_KM

    def named_for(day):
        return next((t for t in named if t["start"] <= day <= t["end"]), None)

    trips, cur, gap = [], None, 0
    for day in sorted(status):
        if status[day] != "away":
            if cur and not (named_for(day) and named_for(day) is cur.get("named")):
                trips.append(cur)
                cur = None
            continue
        nt = named_for(day)
        same_named = cur is not None and nt is not None and cur.get("named") is nt
        if cur and (same_named or (not cur.get("named") and not nt and
                                   (day - cur["days"][-1]).days <= BRIDGE_DAYS + 1 and
                                   not new_trip(cur["days"][-1], day))):
            cur["days"].append(day)
        else:
            if cur:
                trips.append(cur)
            cur = {"days": [day], "named": nt}
    if cur:
        trips.append(cur)

    # A track dated to a day whose other evidence is on another continent (e.g. a track
    # recorded by a second phone) would drag that trip's framing across the ocean;
    # it is split off into its own short trip instead.
    strays = []

    def near_only(day, xs, ref):
        if not ref:
            return xs
        rlat = sorted(p[0] for p in ref)[len(ref) // 2]
        rlon = sorted(p[1] for p in ref)[len(ref) // 2]
        keep = []
        for x in xs:
            if km(x["lat"], x["lon"], rlat, rlon) <= STRAY_KM:
                keep.append(x)
            elif "_day" not in x:
                strays.append(dict(x, _day=day))
        return keep

    def day_tracks(day, foot_only=False):
        xs = [x for x in tracks.get(day, []) if not foot_only or x["type"] in FOOT] + hikes.get(day, [])
        ref = [p for p in ev.get(day, [])] + [(ph["lat"], ph["lon"]) for ph in photos.get(day, [])]
        return near_only(day, xs, ref)

    # A trip's content is what happened AWAY: photos and foot tracks within AWAY_KM of
    # home on the departure / return days (the house, the airport, a morning run) would
    # pull its framing, outline and name back home (a trip named after your own town).
    # Drives stay - they start at home by nature.
    def away_from(h):
        far = lambda lat, lon: km(lat, lon, h["lat"], h["lon"]) > AWAY_KM
        def track_ok(x):
            if x["type"] == "drive":
                return True
            bb = x.get("bb")
            mid = ((bb[1] + bb[3]) / 2, (bb[0] + bb[2]) / 2) if bb else (x["lat"], x["lon"])
            return far(x["lat"], x["lon"]) or far(*mid)
        return far, track_ok

    out = []
    for t in trips:
        days = t["days"]
        span = [days[0] + timedelta(n) for n in range((days[-1] - days[0]).days + 1)]
        far, track_ok = away_from(home_on(homes, days[0]))
        pts = [p for dd in span for p in ev.get(dd, []) if status.get(dd) == "away" and far(p[0], p[1])]
        ph = [p for dd in span for p in photos.get(dd, []) if far(p["lat"], p["lon"])]
        tr = [x for dd in span for x in day_tracks(dd) if track_ok(x)]
        out.append(stats(t.get("named") and t["named"]["name"], "named" if t.get("named") else "trip",
                         span, pts, ph, tr, home_on(homes, days[0])))

    # local hikes: foot activities on home days, grouped per home and year
    local = defaultdict(lambda: {"days": set(), "tracks": [], "photos": []})
    for day, st in status.items():
        if st != "home":
            continue
        foot = day_tracks(day, foot_only=True)
        if foot:
            h = home_on(homes, day)
            g = local[(h["name"], day.year)]
            g["days"].add(day)
            g["tracks"] += foot
            g["photos"] += photos.get(day, [])
            g["home"] = h
    for (home, year), g in sorted(local.items()):
        span = sorted(g["days"])
        pts = [(x["lat"], x["lon"], "track") for x in g["tracks"]]
        out.append(stats(f"Local hikes around {home}, {year}", "local", span, pts, g["photos"], g["tracks"],
                         g["home"], spread=True))
    # stray tracks: consecutive days (within BRIDGE_DAYS) form one trip
    groups = []
    for x in sorted(strays, key=lambda x: x["_day"]):
        if groups and (x["_day"] - groups[-1][-1]["_day"]).days <= BRIDGE_DAYS + 1:
            groups[-1].append(x)
        else:
            groups.append([x])
    for g in groups:
        span = [g[0]["_day"] + timedelta(n) for n in range((g[-1]["_day"] - g[0]["_day"]).days + 1)]
        out.append(stats(None, "trip", span, [(x["lat"], x["lon"], "track") for x in g], [],
                         [{k: v for k, v in x.items() if k != "_day"} for x in g], home_on(homes, span[0])))
    out.sort(key=lambda t: t["start"])
    add_drives(out)
    return out, {"undated_hikes": undated, "days_with_evidence": len(status),
                 "homes": [{k: h[k] for k in ("name", "from", "to", "lat", "lon")} for h in homes]}


def stats(name, kind, span, pts, photos, tracks, home, spread=False):
    """Trip record. The bbox frames the trip's CONTENT (photos + full track extents) -
    the day's other location points include the drive from home, which pulled the
    framing back toward home. Falls back to those points when there's no content."""
    cut = 20 if len(span) > 7 else 50                 # long trips: drop 5% each end, else 2%
    trim = lambda v: v[len(v) // cut: len(v) - len(v) // cut] or v
    clat = [p["lat"] for p in photos] + [c for x in tracks if "bb" in x for c in (x["bb"][1], x["bb"][3])]
    clon = [p["lon"] for p in photos] + [c for x in tracks if "bb" in x for c in (x["bb"][0], x["bb"][2])]
    if len(clat) >= 2:
        lats, lons = trim(sorted(clat)), trim(sorted(clon))
    else:
        lats = trim(sorted(p[0] for p in pts) or [home["lat"]])
        lons = trim(sorted(p[1] for p in pts) or [home["lon"]])
    alts = [p["alt"] for p in photos if p.get("alt") is not None]
    start, end = span[0], span[-1]
    foot_km = sum(x["km"] for x in tracks if x["type"] in FOOT or x["type"] == "hike")
    return {
        "id": f"{kind}-{start.isoformat()}-{end.isoformat()}",
        "kind": kind,
        "name": name,
        "start": start.isoformat(), "end": end.isoformat(),
        "days": len(span) if not spread else len(set(span)),
        "bbox": [lons[0], lats[0], lons[-1], lats[-1]],
        "home": home["name"],
        "photos": len(photos), "photo_ids": [p["id"] for p in photos],
        "tracks": [{k: x[k] for k in ("src", "id", "type", "km") if k in x} | ({"name": x["name"]} if "name" in x else {})
                   | ({"bb": [round(v, 5) for v in x["bb"]]} if "bb" in x else {})       # click-to-zoom in the panel
                   for x in tracks],
        "hiked_km": round(foot_km, 1),
        "high_m": round(max(alts)) if alts else None,
    }


# ----- trip area (for styles that colour it in: config.js `tripArea`) -------------------
AREA_MAX_KM = 80      # only trips whose content fits a box this size (corner to corner)


def trip_areas(trips, private=()):
    """id -> outline polygon ([[lon, lat], ...] rings) of where a compact trip happened:
    its photos and tracks, each widened by ~18% of the trip's size, gaps closed, then
    simplified. Long road trips and the yearly local-hike groups get none."""
    from shapely.geometry import LineString, MultiPoint, Point, Polygon
    from shapely.ops import unary_union
    cand = []
    for t in trips:
        w, s, e, n = t["bbox"]
        lat0 = (s + n) / 2
        kx, ky = 111.32 * math.cos(math.radians(lat0)), 110.57
        diag = math.hypot((e - w) * kx, (n - s) * ky)
        if t["kind"] != "local" and diag <= AREA_MAX_KM and (t["photo_ids"] or t["tracks"]):
            cand.append((t, lat0, kx, ky, diag))
    if not cand:
        return {}
    want = {(x["src"], str(x["id"])) for t, *_ in cand for x in t["tracks"] if x.get("type") != "drive"}
    lines = {}
    for i, f in enumerate(jsonl("hikes.geojsonl")):
        if ("hike", str(i)) in want:
            g = f["geometry"]
            lines[("hike", str(i))] = g["coordinates"] if g["type"] == "MultiLineString" else [g["coordinates"]]
    for f in jsonl("activities.geojsonl"):
        k = ("activity", str(f["properties"]["activityId"]))
        if k in want:
            lines[k] = [f["geometry"]["coordinates"]]
    where = {f["properties"]["id"]: f["geometry"]["coordinates"][:2] for f in geojson("photos.geojson")}
    out = {}
    for t, lat0, kx, ky, diag in cand:
        w, s, e, n = t["bbox"]
        mx, my = (e - w) * 0.1 + 0.005, (n - s) * 0.1 + 0.005      # the bbox is trimmed: allow a little past it
        inside = lambda c: w - mx <= c[0] <= e + mx and s - my <= c[1] <= n + my
        xy = lambda c: ((c[0] - w) * kx, (c[1] - s) * ky)
        r = max(1.5, 0.18 * diag)                                    # room for the soft edge outside the content
        parts, segs = [], []
        pts = [xy(where[i]) for i in t["photo_ids"] if i in where and i not in private and inside(where[i])]
        if pts:
            parts.append(MultiPoint(pts).buffer(r, 8))
        for x in t["tracks"]:
            if x.get("type") == "drive":
                continue
            for l in lines.get((x["src"], str(x["id"])), []):
                cs = [xy(c) for c in l if inside(c)]
                if len(cs) >= 2:
                    seg = LineString(cs).simplify(r / 10)
                    segs.append(seg)
                    parts.append(seg.buffer(r, 8))
        if not parts:
            continue
        g = unary_union(parts).buffer(r, 8).buffer(-r, 8)             # close the gaps between pieces
        # keep the pieces where the trip happened: weigh each by its photos (1 each) and
        # tracks (3 per km); a lone photo from a roadside stop made its own little island
        polys = list(getattr(g, "geoms", [g]))
        weight = [sum(1 for q in pts if p.contains(Point(q))) + 3 * sum(p.intersection(l).length for l in segs)
                  for p in polys]
        top = max(weight) or 1
        polys = [p for p, wt in zip(polys, weight) if wt >= 0.2 * top]
        if len(polys) > 1:                  # nearby pieces (trailhead + lake): one smooth shape
            hull = unary_union(polys).convex_hull
            if hull.area <= 3 * sum(p.area for p in polys):
                polys = [hull.buffer(r * 0.3, 8)]
        rings = []
        for p in polys:
            p = Polygon(p.exterior).simplify(r / 8)                  # holes would leave grey islands
            rings.append([[round(w + x / kx, 5), round(s + y / ky, 5)] for x, y in p.exterior.coords])
        out[t["id"]] = rings
    return out


FOCUS_LINK_KM = 40        # photos / track points this close belong to the same place
FOCUS_MIN_SHARE = 0.5     # zoom to the main place only when most of the trip happened there


def trip_focus(trips, private=()):
    """id -> [w, s, e, n] of the place a trip mostly happened (its photos, 1 each, and
    tracks on foot, 3 per km), where that's well inside the trip's full extent: a trip
    that starts at home or an airport, or drives home through three states, zooms to
    where it was spent rather than the whole way there and back."""
    where = {f["properties"]["id"]: f["geometry"]["coordinates"][:2] for f in geojson("photos.geojson")}
    want = {(x["src"], str(x["id"])) for t in trips for x in t["tracks"] if x.get("type") != "drive"}
    lines = {}
    for i, f in enumerate(jsonl("hikes.geojsonl")):
        if ("hike", str(i)) in want:
            g = f["geometry"]
            lines[("hike", str(i))] = g["coordinates"] if g["type"] == "MultiLineString" else [g["coordinates"]]
    for f in jsonl("activities.geojsonl"):
        k = ("activity", str(f["properties"]["activityId"]))
        if k in want:
            lines[k] = [f["geometry"]["coordinates"]]
    out = {}
    for t in trips:
        w0, s0, e0, n0 = t["bbox"]
        lat0 = (s0 + n0) / 2
        kx, ky = 111.32 * math.cos(math.radians(lat0)), 110.57
        pts = [(*where[i], 1.0) for i in t["photo_ids"] if i in where and i not in private]
        for x in t["tracks"]:
            if x.get("type") == "drive":
                continue
            for l in lines.get((x["src"], str(x["id"])), []):
                if len(l) < 2:
                    continue
                km = sum(math.hypot((b[0] - a[0]) * kx, (b[1] - a[1]) * ky) for a, b in zip(l, l[1:]))
                step = l[:: max(1, len(l) // 20)]
                pts += [(c[0], c[1], 3 * km / len(step)) for c in step]
        if len(pts) < 2:
            continue
        # places: points on a grid of FOCUS_LINK_KM cells, neighbouring cells joined
        cell = lambda p: (math.floor(p[0] * kx / FOCUS_LINK_KM), math.floor(p[1] * ky / FOCUS_LINK_KM))
        cells = defaultdict(list)
        for p in pts:
            cells[cell(p)].append(p)
        parent = {c: c for c in cells}
        def root(c):
            while parent[c] != c:
                parent[c] = parent[parent[c]]
                c = parent[c]
            return c
        for (cx, cy) in cells:
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if (cx + dx, cy + dy) in cells:
                        parent[root((cx, cy))] = root((cx + dx, cy + dy))
        places = defaultdict(list)
        for c, ps in cells.items():
            places[root(c)] += ps
        total = sum(p[2] for p in pts)
        main = max(places.values(), key=lambda ps: sum(p[2] for p in ps))
        if sum(p[2] for p in main) < FOCUS_MIN_SHARE * total:
            continue                                    # no one place: show it all
        w, s = min(p[0] for p in main), min(p[1] for p in main)
        e, n = max(p[0] for p in main), max(p[1] for p in main)
        diag = lambda a, b, c, d: math.hypot((c - a) * kx, (d - b) * ky)
        if diag(w, s, e, n) < 0.6 * diag(w0, s0, e0, n0) and diag(w0, s0, e0, n0) > 30:
            out[t["id"]] = [round(w, 5), round(s, 5), round(e, 5), round(n, 5)]
    return out


def write_public(trips, names=None, heroes=None):
    """out/trips.json for the web panel: no per-photo id lists, just the hero photo.
    `names` (id -> {name, summary, place}) and `heroes` (id -> photo id) come from the
    naming step once photos are tagged; until then the first photo stands in."""
    from private_photos import load_private_ids
    private = load_private_ids()                       # never a hero, never counted
    names, heroes = names or {}, heroes or {}
    tn_file = OUT / "track_names.json"                 # name_tracks.py: OSM-based track names
    track_names = json.loads(tn_file.read_text(encoding="utf-8")) if tn_file.exists() else {}
    areas = trip_areas(trips, private)
    focus = trip_focus(trips, private)
    # featured_trips.json (personal, optional): trip ids for the panel's Featured folder, in order
    ff = HERE / "featured_trips.json"
    featured = {tid: i + 1 for i, tid in enumerate(json.loads(ff.read_text(encoding="utf-8"))["trips"])} if ff.exists() else {}
    pub = []
    for t in trips:
        n = names.get(t["id"], {})
        tracks = [x | ({"name": track_names[f"{x['src']}:{x['id']}"]["name"]}
                       if f"{x['src']}:{x['id']}" in track_names else {}) for x in t["tracks"]]
        public_ids = [i for i in t["photo_ids"] if i not in private]
        if NEED_PHOTOS and not public_ids:
            continue                                    # no photos to show: not listed
        hero = heroes.get(t["id"])
        if not hero or hero in private:
            hero = public_ids[0] if public_ids else None
        pub.append({k: v for k, v in t.items() if k not in ("photo_ids", "tracks")} | {"tracks": tracks}
                   # named trips and local-hike groups keep their own names; Claude names the rest
                   | {"name": t["name"] if t["kind"] in ("named", "local") else (n.get("name") or t["name"]),
                      "summary": n.get("summary"), "place": n.get("place"),
                      "photos": len(public_ids), "hero": hero}
                   | ({"area": areas[t["id"]]} if t["id"] in areas else {})
                   | ({"focus": focus[t["id"]]} if t["id"] in focus else {})
                   | ({"featured": featured[t["id"]]} if t["id"] in featured else {}))
    (OUT / "trips.json").write_text(json.dumps({"trips": pub}, separators=(",", ":")), encoding="utf-8")
    write_trip_photos(trips, private)


# ----- roads driven (multi-day trips) --------------------------------------------------
SNAP_GOOGLE = OUT / "_snap" / "match_results_google.json"     # snap_regions.py: Google trips -> road edges
DRIVE_INDEX = OUT / "_trips" / "drives_by_day.json"


def thin(coords, min_m=25):
    out = [coords[0]]
    for c in coords[1:-1]:
        if km(out[-1][1], out[-1][0], c[1], c[0]) * 1000 >= min_m:
            out.append(c)
    out.append(coords[-1])
    return [[round(c[0], 5), round(c[1], 5)] for c in out]


def drives_by_day():
    """date -> {road edge key: coords} driven that day, from the Google history matched
    to the road network (the same matching behind the Roads travelled layer). The
    473 MB source only changes when Google history is re-imported, so the compact
    index is rebuilt only when it's newer."""
    if not SNAP_GOOGLE.exists():
        return {}
    if DRIVE_INDEX.exists() and DRIVE_INDEX.stat().st_mtime >= SNAP_GOOGLE.stat().st_mtime:
        return json.loads(DRIVE_INDEX.read_text(encoding="utf-8"))
    from google_network import DRIVE_MODES, FOOT_MODES, FOOT_CLASSES
    idx = defaultdict(dict)
    for r in json.loads(SNAP_GOOGLE.read_text(encoding="utf-8")):
        p = r["props"]
        mode = p.get("mode")
        if not p.get("date") or mode in FOOT_MODES or (mode not in DRIVE_MODES and r.get("category") == "hike"):
            continue
        for key, e in (r.get("edge_geom") or {}).items():
            if e.get("cls") not in FOOT_CLASSES and len(e.get("coords") or []) > 1:
                idx[p["date"]][key] = thin(e["coords"])
    DRIVE_INDEX.parent.mkdir(parents=True, exist_ok=True)
    DRIVE_INDEX.write_text(json.dumps(idx, separators=(",", ":")), encoding="utf-8")
    return idx


def trip_drive(t, idx):
    """{edge key: coords} driven between the trip's first and last day."""
    a, b = date.fromisoformat(t["start"]), date.fromisoformat(t["end"])
    edges = {}
    for n in range((b - a).days + 1):
        edges.update(idx.get((a + timedelta(n)).isoformat(), {}))
    return edges


def add_drives(trips):
    """Multi-day trips (not local-hike groups, not day trips) get drive_km and a
    has_drive flag. Framing stays on the trip's content: widening it to the whole
    journey (home -> a range 500 km away) shrank the hikes to specks."""
    idx = drives_by_day()
    for t in trips:
        if t["kind"] == "local" or t["days"] < 2:
            continue
        edges = trip_drive(t, idx)
        legacy = [x for x in t["tracks"] if x["type"] == "drive"]       # dated drives from the hikes layer
        if not edges and not legacy:
            continue
        km_e = sum(line_km(c) for c in edges.values())
        t["drive_km"] = round(km_e + sum(x["km"] for x in legacy), 1)
        t["has_drive"] = True


def write_trip_photos(trips, private):
    """out/trips/<trip id>.json: the trip's public photos as points, loaded by the web
    app when the trip is picked. The photo tiles are thinned below z9, so filtering
    them by date left a big trip with almost no dots."""
    from private_photos import null_island
    feats = {f["properties"]["id"]: f for f in geojson("photos.geojson")}
    keep = ("id", "timestamp", "is_360", "filename", "pano_type")
    tags_file = OUT / "photo_tags.json"
    junk = {i for i, t in json.loads(tags_file.read_text(encoding="utf-8")).items()
            if t.get("keep") is False} if tags_file.exists() else set()
    d = OUT / "trips"
    d.mkdir(exist_ok=True)
    idx = drives_by_day()
    for old in d.glob("*.json"):
        old.unlink()                                   # trips can merge/split between runs
    for t in trips:
        pts = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": feats[i]["geometry"]["coordinates"][:2]},
                "properties": {k: feats[i]["properties"].get(k) for k in keep} | ({"junk": 1} if i in junk else {})}
               for i in t["photo_ids"] if i not in private and i in feats and not null_island(feats[i])]
        (d / f"{t['id']}.json").write_text(json.dumps({"type": "FeatureCollection", "features": pts},
                                                      separators=(",", ":")), encoding="utf-8")
        if t.get("has_drive"):                         # roads driven, shown in its own colour
            lines = list(trip_drive(t, idx).values())
            fc = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {},
                  "geometry": {"type": "MultiLineString", "coordinates": lines}}] if lines else []}
            (d / f"{t['id']}.drive.json").write_text(json.dumps(fc, separators=(",", ":")), encoding="utf-8")


def main():
    trips, info = detect()
    (OUT / "trips_draft.json").write_text(json.dumps({"info": info, "trips": trips}, indent=1), encoding="utf-8")
    write_public(trips)
    by_year = defaultdict(lambda: defaultdict(int))
    for t in trips:
        y = by_year[t["start"][:4]]
        y[t["kind"]] += 1
        y["days"] += t["days"] if t["kind"] != "local" else 0
        y["photos"] += t["photos"]
        y["km"] += t["hiked_km"]
    print(f"{len(trips)} trips; {info['days_with_evidence']:,} days with location evidence; "
          f"{info['undated_hikes']:,} hike tracks could not be dated")
    for h in info["homes"]:
        print(f"  home {h['name']:18s} {h['from']}..{h['to']}  at {h['lat']:.2f},{h['lon']:.2f}")
    for y in sorted(by_year):
        v = by_year[y]
        print(f"  {y}: {v['trip']:3d} trips, {v['named']} named, {v['local']} local groups, "
              f"{v['days']:4d} days away, {v['photos']:5,} photos, {v['km']:6.0f} km on foot")


if __name__ == "__main__":
    main()
