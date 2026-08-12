#!/usr/bin/env python3
"""Deterministic local mock for the EV voiceprint HTTP encoder.

SUIT's smoke harness runs this tiny stdlib server so the backend's
``EV_VOICEPRINT_PROVIDER=http`` path can exercise the client wiring end to end
without downloading SpeechBrain/CAM++ weights. It returns the same fixed
192-dim embedding for every sample, so verification will always match.

This is NOT a security control. It exists only so the smoke test can prove
the client → API → lifecycle plumbing; real owner verification must use a real
encoder (campp/speechbrain) with real enrollment audio.
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

EMBEDDING = [0.1 + (i % 7) * 0.01 for i in range(192)]


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if self.path != "/v1/embed":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        try:
            payload = json.loads(body or b"{}")
            audio_b64 = payload.get("audio_b64", "")
            sample_rate = payload.get("sample_rate", 16000)
        except json.JSONDecodeError:
            self.send_error(400)
            return
        response = {
            "embedding": EMBEDDING,
            "audio_bytes": len(audio_b64),
            "sample_rate": sample_rate,
            "provider": "smoke-mock",
        }
        encoded = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        sys.stderr.write("[mock-embed] %s\n" % (format % args))


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"mock voiceprint encoder on http://127.0.0.1:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
