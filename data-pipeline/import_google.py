#!/usr/bin/env python3
"""One-command Google Timeline import.

Google killed the server-side Timeline export, so location history is now an
on-device file you download manually every so often. This takes that exported
JSON and does the rest in one shot: convert -> rebuild the Google tile -> upload
-> deploy -> verify the privacy gate.

Drop the exported JSON in `google_exports/` (or pass its path) and run:

    python import_google.py                       # newest file in google_exports/
    python import_google.py "C:\\path\\Timeline.json"
    python import_google.py --no-deploy           # build + upload only

Note: a Timeline export is the full on-device history, so re-importing simply
refreshes the `google_paths` layer with the latest snapshot.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# reuse the corrected deploy/verify helpers (deploy runs from web/ with bindings)
from auto_import import deploy, verify_gate, run, log, PY

HERE = Path(__file__).resolve().parent
DROP = HERE / "google_exports"


def newest_json(folder: Path):
    if not folder.exists():
        return None
    cands = sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return cands[0] if cands else None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("json", nargs="?", help="path to the exported Timeline JSON")
    ap.add_argument("--dir", type=Path, default=DROP,
                    help="folder to find the newest export in (default: google_exports/)")
    ap.add_argument("--no-deploy", action="store_true")
    args = ap.parse_args()

    src = Path(args.json) if args.json else newest_json(args.dir)
    if not src or not src.exists():
        args.dir.mkdir(parents=True, exist_ok=True)
        sys.exit(f"No Timeline JSON found.\nPass a path, or drop the export in:\n  {args.dir}")

    log(f"Google Timeline import <- {src}")
    run([PY, "convert_google_timeline.py", str(src), "--outdir", "out", "--raw-signals"])
    run([PY, "build_pmtiles.py", "google_paths"])
    run([PY, "upload.py", "tiles"])
    if args.no_deploy:
        log("skipping deploy (--no-deploy)")
    else:
        deploy()
    verify_gate()
    log("Google import done.")


if __name__ == "__main__":
    main()
