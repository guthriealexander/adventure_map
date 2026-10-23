"""Recorded tracks kept off the map and out of trips: ones in the data that weren't
actually hiked (a planned route saved as a track, say). Listed in excluded_tracks.json
(gitignored, personal) by source + start time, which survive rebuilds; line numbers in
out/hikes.geojsonl don't, and other steps refer to hikes by line number, so the tracks
stay in the data and are skipped where it's used (build_pmtiles.py, build_trips.py)."""
import json
from pathlib import Path

HERE = Path(__file__).parent
LIST = HERE / "excluded_tracks.json"
HIKES = HERE / "out" / "hikes.geojsonl"


def load():
    """{(source, begin)} to leave out; empty without the list."""
    if not LIST.exists():
        return set()
    return {(t["source"], t["begin"]) for t in json.loads(LIST.read_text(encoding="utf-8"))["tracks"]}


def excluded_lines():
    """Line numbers in out/hikes.geojsonl of the excluded tracks (what hikes_snapped's
    src_idx and the trips refer to)."""
    keys = load()
    if not keys or not HIKES.exists():
        return set()
    out = set()
    with open(HIKES, encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            if '"begin"' not in line:
                continue
            p = json.loads(line).get("properties") or {}
            if (p.get("source"), p.get("begin")) in keys:
                out.add(i)
    return out
