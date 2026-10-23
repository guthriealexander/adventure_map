#!/usr/bin/env python3
"""Write a little demo data to out/ so the map has something to show before you've
imported anything of your own: a few places (POIs) and one demo trip. The web app
reads out/ directly when it runs on localhost.

    python make_demo_data.py
    python dev_server.py 8789      # then open http://localhost:8789/web/index.html

The demo is a made-up day in Rocky Mountain National Park, Colorado. Delete out/
(or just run your own pipeline over it) when you start importing real data.
"""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent / "out"

POIS = [
    ("Bear Lake Trailhead", "Trailhead", -105.6453, 40.3127),
    ("Emerald Lake", "Lake", -105.6705, 40.3093),
    ("Mills Lake", "Lake", -105.6413, 40.2885),
    ("Moraine Park Campground", "Camp", -105.6013, 40.3608),
    ("Estes Park", "Town", -105.5217, 40.3772),
]

TRIP_ID = "trip-demo-bear-lake"
AREA = [[
    [-105.705, 40.300], [-105.690, 40.322], [-105.660, 40.328], [-105.630, 40.322],
    [-105.618, 40.305], [-105.622, 40.283], [-105.640, 40.274], [-105.668, 40.279],
    [-105.693, 40.288], [-105.705, 40.300],
]]


def main():
    OUT.mkdir(exist_ok=True)
    (OUT / "trips").mkdir(exist_ok=True)

    pois = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
         "properties": {"id": i + 1, "name": name, "type": typ, "note": "demo"}}
        for i, (name, typ, lon, lat) in enumerate(POIS)]}
    (OUT / "pois.geojson").write_text(json.dumps(pois, indent=1), encoding="utf-8")

    trip = {
        "id": TRIP_ID, "kind": "trip", "name": "Demo: Bear Lake lakes loop",
        "start": "2024-07-20", "end": "2024-07-20", "days": 1,
        "bbox": [-105.68, 40.285, -105.635, 40.315],
        "home": "Example Town", "photos": 0, "hiked_km": 12.4, "high_m": 3080,
        "tracks": [], "hero": None, "area": AREA,
        "place": "Rocky Mountain National Park, Colorado",
        "summary": "A demo day hike past Nymph, Dream and Emerald Lakes and on to Mills Lake. "
                   "Your own trips are built from your photos and tracks by build_trips.py "
                   "and named by name_trips.py.",
    }
    (OUT / "trips.json").write_text(json.dumps({"trips": [trip]}, indent=1), encoding="utf-8")
    (OUT / "trips" / f"{TRIP_ID}.json").write_text(
        json.dumps({"type": "FeatureCollection", "features": []}), encoding="utf-8")
    print(f"wrote {len(POIS)} demo places and 1 demo trip to {OUT}")


if __name__ == "__main__":
    main()
