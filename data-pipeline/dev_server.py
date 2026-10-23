#!/usr/bin/env python3
"""Local dev server for previewing the web app against the pipeline's out/ data.

Two things `python -m http.server` can't do, which this does:
  * HTTP Range requests (206) — PMTiles needs them, so tile layers (photos, tracks,
    isochrones…) actually render locally instead of erroring.
  * `Cache-Control: no-store` — the browser never serves stale JS/CSS while editing.

Serves the repo root, so the app is at
    http://localhost:8789/web/index.html
and config.js's local paths (../data-pipeline/out, …/derivatives) resolve.

Usage:
    python dev_server.py [port]
"""
from __future__ import annotations

import functools
import http.server
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # the repo root (web/ + data-pipeline/)
RANGE = re.compile(r"bytes=(\d+)-(\d*)$")


class _Limited:
    """File wrapper that stops after n bytes (for 206 partial bodies)."""

    def __init__(self, f, n):
        self.f, self.n = f, n

    def read(self, size=-1):
        if self.n <= 0:
            return b""
        size = self.n if size is None or size < 0 or size > self.n else size
        b = self.f.read(size)
        self.n -= len(b)
        return b

    def close(self):
        self.f.close()


class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def send_head(self):
        rng = self.headers.get("Range")
        path = self.translate_path(self.path)
        if not rng or not os.path.isfile(path):
            return super().send_head()
        m = RANGE.match(rng.strip())
        if not m:
            return super().send_head()
        size = os.path.getsize(path)
        start = int(m.group(1))
        end = min(int(m.group(2)) if m.group(2) else size - 1, size - 1)
        if start >= size or start > end:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return None
        f = open(path, "rb")
        f.seek(start)
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        return _Limited(f, end - start + 1)

    def log_message(self, fmt, *args):   # quieter: skip the per-request spam
        pass


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8789
    handler = functools.partial(Handler, directory=str(ROOT))
    with http.server.ThreadingHTTPServer(("127.0.0.1", port), handler) as srv:
        print(f"serving {ROOT} with Range + no-store on http://localhost:{port}/web/index.html")
        srv.serve_forever()


if __name__ == "__main__":
    main()
