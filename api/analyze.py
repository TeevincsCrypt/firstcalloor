"""Vercel serverless entrypoint: GET /api/analyze?ca=<CONTRACT_ADDRESS>

Returns the same report the CLI writes to JSON. Stdlib only - no build step,
no requirements.txt, nothing to install.
"""

import argparse
import json
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler

from _engine import (
    DEFAULT_RPC,
    DEFAULT_TWEET_CAP,
    FirstCallooorError,
    VERSION,
    run_analysis,
    select_provider,
    validate_contract_address,
)

# Serverless functions have a hard wall clock (10s on Vercel Hobby, 60s on Pro).
# Walking a busy token's signature history is the slow part, so the web path
# uses a tighter page cap than the CLI and says so when it trips.
WEB_SIG_PAGE_CAP = int(os.environ.get("FIRSTCALLOOR_SIG_PAGE_CAP", "12"))
WEB_TWEET_CAP = int(os.environ.get("FIRSTCALLOOR_MAX_TWEETS", str(DEFAULT_TWEET_CAP)))


def web_args(include_retweets: bool, full_archive: bool) -> argparse.Namespace:
    return argparse.Namespace(
        rpc=os.environ.get("SOLANA_RPC_URL", DEFAULT_RPC),
        provider="auto",                    # prefers twitterapi.io when configured
        bearer_token=None,                  # read from env inside select_provider
        twitterapi_key=None,                # read from env inside select_provider
        full_archive=full_archive,
        max_tweets=WEB_TWEET_CAP,
        sig_page_cap=WEB_SIG_PAGE_CAP,
        include_retweets=include_retweets,
        pre_window_hours=int(os.environ.get("FIRSTCALLOOR_PRE_WINDOW_HOURS", "24")),
        no_cross_check=False,
        verbose=False,
        mock=False,
        json_path=None,
    )


def first(query: dict, key: str, default: str = "") -> str:
    """First value for a query key, tolerating a missing or empty value list."""
    values = query.get(key) or []
    return (values[0] if values else default) or default


def analyze_request(query: dict) -> tuple[int, dict]:
    raw = first(query, "ca").strip()
    truthy = {"1", "true", "yes", "on"}

    try:
        ca = validate_contract_address(raw)
    except FirstCallooorError as exc:
        return 400, {"error": "invalid_input", "message": str(exc)}

    args = web_args(
        include_retweets=first(query, "include_retweets", "0").lower() in truthy,
        full_archive=(
            os.environ.get("X_FULL_ARCHIVE", "").lower() in truthy
            or first(query, "full_archive", "0").lower() in truthy
        ),
    )

    try:
        report, _, outcome, _, _ = run_analysis(ca, args)
    except FirstCallooorError as exc:
        return 502, {"error": "upstream", "message": str(exc)}
    except Exception as exc:  # never leak a traceback to the browser
        return 500, {"error": "internal", "message": f"{type(exc).__name__}: {exc}"}

    configured_provider, _ = select_provider(args)
    report["search"]["mentions_configured"] = configured_provider != "none"
    return (200 if outcome.complete else 206), report


class handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        status, body = analyze_request(query)
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")

        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Firstcalloor-Version", VERSION)
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *a):
        pass
