#!/usr/bin/env python3
"""Stamp a content hash onto the app's own JS/CSS links in web/index.html.

    <script src="app.js">  ->  <script src="app.js?v=3f9c1a2b7d">

A file whose contents changed gets a new URL, so browsers always fetch the new
code on a normal reload (no more Ctrl+Shift+R after a deploy); unchanged files keep
their URL and stay cached. Idempotent — re-running only changes hashes of files
that actually changed. External (CDN) links are left alone.

Run before every deploy (auto_import.deploy() does this automatically):
    python stamp_assets.py
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
# local .js/.css in a src= or href= (no scheme, so CDN links don't match),
# with or without an existing ?v= stamp
PAT = re.compile(
    r'(<(?:script|link)\b[^>]*?\b(?:src|href)=")([^"?#:]+\.(?:js|css))(?:\?v=[0-9a-f]*)?(")')


def stamp(index: Path = WEB / "index.html") -> list[str]:
    with open(index, encoding="utf-8", newline="") as fh:   # keep line endings as-is
        html = fh.read()
    changed = []

    def sub(m):
        rel = m.group(2)
        f = WEB / rel
        if not f.is_file():
            return m.group(0)
        h = hashlib.sha1(f.read_bytes()).hexdigest()[:10]
        new = f"{m.group(1)}{rel}?v={h}{m.group(3)}"
        if new != m.group(0):
            changed.append(f"{rel}?v={h}")
        return new

    out = PAT.sub(sub, html)
    if out != html:
        with open(index, "w", encoding="utf-8", newline="") as fh:
            fh.write(out)
    return changed


if __name__ == "__main__":
    changed = stamp()
    print("stamped: " + (", ".join(changed) if changed else "no changes"))
