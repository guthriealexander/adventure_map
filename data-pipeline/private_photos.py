"""Photos that must never be published (private family photos, e.g. a child without
clothes). Listed by id in private_photos.txt, one per line, '#' comments allowed.

They stay in out/photos.geojson on purpose - the Camera Roll scan dedupes against
that manifest, so dropping them there would just re-import them. Instead every
publish step filters them out: build_pmtiles.py (map tiles), upload_r2.py (derivative
images + the catalog copy on R2), and `upload_r2.py purge-private` deletes copies
already uploaded. tag_photos.py appends photos the model flags as sensitive.
"""
from __future__ import annotations

from pathlib import Path

LIST = Path(__file__).resolve().parent / "private_photos.txt"


def null_island(feature) -> bool:
    """True for a photo located at exactly 0, 0 ("Null Island") - a zeroed GPS tag
    (182 Pixel edited copies had one), not a real place. Treated as no location:
    left off the map and out of trips, whose framing it dragged into the Atlantic."""
    c = ((feature or {}).get("geometry") or {}).get("coordinates") or [0, 0]
    return abs(c[0]) < 1e-6 and abs(c[1]) < 1e-6


def load_private_ids() -> set[str]:
    if not LIST.exists():
        return set()
    ids = set()
    for line in LIST.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            ids.add(line)
    return ids


def add_private_ids(ids, note: str = "") -> list[str]:
    """Append ids not already listed; returns the ones added."""
    have = load_private_ids()
    new = [i for i in ids if i not in have]
    if new:
        with LIST.open("a", encoding="utf-8") as f:
            for i in new:
                f.write(f"{i}{('  # ' + note) if note else ''}\n")
    return new
