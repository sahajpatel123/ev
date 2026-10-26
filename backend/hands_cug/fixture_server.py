"""Localhost fixture server: pages modelling real UI states, with HTTP Range for media.

Serves tests/fixtures/hands/web on 127.0.0.1 only. Pages are original markup
written for these tests; nothing is copied from any site.
"""

from __future__ import annotations

import http.server
import os
import re
import socketserver
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "hands" / "web"


class _Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map,
                      ".mp4": "video/mp4", ".html": "text/html; charset=utf-8"}

    def log_message(self, *a, **k):  # quiet
        pass

    def translate_path(self, path: str) -> str:
        path = path.split("?", 1)[0].split("#", 1)[0]
        return str(ROOT / path.lstrip("/"))

    def send_head(self):
        rng = self.headers.get("Range")
        path = self.translate_path(self.path)
        if not rng or not os.path.isfile(path):
            return super().send_head()
        m = re.match(r"bytes=(\d+)-(\d*)", rng)
        if not m:
            return super().send_head()
        size = os.path.getsize(path)
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else size - 1
        end = min(end, size - 1)
        f = open(path, "rb")
        f.seek(start)
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self._range_left = end - start + 1
        return f

    def copyfile(self, source, outputfile):
        left = getattr(self, "_range_left", None)
        if left is None:
            return super().copyfile(source, outputfile)
        try:
            while left > 0:
                chunk = source.read(min(65536, left))
                if not chunk:
                    break
                outputfile.write(chunk)
                left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self._range_left = None


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class FixtureServer:
    def __init__(self, port: int = 0):
        self._srv = _Server(("127.0.0.1", port), _Handler)
        self.port = self._srv.server_address[1]
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)

    def start(self) -> FixtureServer:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()

    def url(self, page: str, **params) -> str:
        from urllib.parse import urlencode

        q = f"?{urlencode(params)}" if params else ""
        return f"http://127.0.0.1:{self.port}/{page}{q}"

    def __enter__(self) -> FixtureServer:
        return self.start()

    def __exit__(self, *a) -> None:
        self.stop()
