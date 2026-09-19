#!/usr/bin/env python3
"""Local dev server mirroring the Vercel routing, so the site can be run
without deploying.

    python dev_server.py           # http://localhost:8000
"""

import os
import sys
import urllib.parse
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "api"))

import json  # noqa: E402
from analyze import VERSION, analyze_request  # noqa: E402


class Router(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.rstrip("/") != "/api/analyze":
            return super().do_GET()

        status, body = analyze_request(urllib.parse.parse_qs(parsed.query))
        payload = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Firstcalloor-Version", VERSION)
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    print(f"FIRSTCALLOOR dev server -> http://localhost:{port}")
    if not (os.environ.get("X_BEARER_TOKEN") or os.environ.get("TWITTER_BEARER_TOKEN")):
        print("note: X_BEARER_TOKEN unset - on-chain data only, no mention search")
    ThreadingHTTPServer(("0.0.0.0", port), Router).serve_forever()
