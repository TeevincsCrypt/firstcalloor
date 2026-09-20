"""Vercel serverless entrypoint: GET /api/analyze?ca=<CONTRACT_ADDRESS>

Returns the same report the CLI writes to JSON. Stdlib only - no build step,
no requirements.txt, nothing to install.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler

# Vercel's Python runtime does not guarantee this file's own directory is on
# sys.path when it imports this module as the function entrypoint - it works
# locally (dev_server.py puts api/ on sys.path itself) but silently fails on
# Vercel with a bare `from _engine import ...`, crashing at import time
# before do_GET ever runs. That produces Vercel's own generic platform error
# page instead of anything from this file's error handling, which is exactly
# the "Unexpected token 'A', "A server e"..." a browser sees when it tries to
# JSON.parse that HTML. Making the import self-sufficient fixes it outright.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _engine import (  # noqa: E402
    DEFAULT_RPC,
    DEFAULT_TWEET_CAP,
    FirstCallooorError,
    HttpBudget,
    VERSION,
    run_analysis,
    select_provider,
    validate_contract_address,
)

# Serverless functions have a hard wall clock (10s on Vercel Hobby, matching
# vercel.json's maxDuration). _engine.http_json's defaults (30s timeout, up
# to 4 retries with exponential backoff - up to ~30s of sleeping alone) exist
# for the CLI, where a human can afford to wait out a slow endpoint. Under
# those same defaults here, a single slow or rate-limited call - the public
# Solana RPC throttling a Vercel IP, a busy signature history, twitterapi.io
# briefly lagging - can burn the ENTIRE function budget by itself, and
# Vercel then kills the process before any of this file's own error
# handling gets a chance to run: the browser sees the platform's own opaque
# crash page instead of the JSON this file always tries to return. Setting
# this tight budget at import time (before any request is handled) is what
# makes a bad call fail fast with a clear, in-budget JSON note instead.
HttpBudget.timeout = int(os.environ.get("FIRSTCALLOOR_HTTP_TIMEOUT", "6"))
HttpBudget.retries = int(os.environ.get("FIRSTCALLOOR_HTTP_RETRIES", "1"))
HttpBudget.max_wait = 6.0
# A 429 specifically is never worth retrying here: a real rate-limit reset
# runs tens of seconds, this function gets ~10s total, so a retry can't
# possibly land inside a still-limited window - it only spends a second
# request hammering an already-tripped limiter for a wait that was doomed
# from the start. Skipping it also halves worst-case request volume for a
# quiet token that trips a provider's rate limit on every search width.
HttpBudget.retry_on_429 = os.environ.get("FIRSTCALLOOR_RETRY_ON_429", "0").lower() in (
    "1", "true", "yes", "on"
)

# Walking a busy token's signature history is the slow part, so the web path
# uses a tighter page cap than the CLI and says so when it trips.
WEB_SIG_PAGE_CAP = int(os.environ.get("FIRSTCALLOOR_SIG_PAGE_CAP", "12"))
WEB_TWEET_CAP = int(os.environ.get("FIRSTCALLOOR_MAX_TWEETS", str(DEFAULT_TWEET_CAP)))
# The pre-launch recycled-CA probe is a nice-to-have extra request, not the
# primary answer - it costs a full network round trip the tight budget above
# often can't spare, so the web path skips it by default. The CLI has no
# such constraint and keeps probing 24h back unless told otherwise.
WEB_PRE_WINDOW_HOURS = int(os.environ.get("FIRSTCALLOOR_PRE_WINDOW_HOURS", "0"))

# On-chain extras (first buyers, dev fingerprinting, rug signal) each cost
# several more RPC round trips on top of an already-tight budget. The rug
# signal is cheap (3 calls total) and high-value, so it stays on. First
# buyers and the dev-wallet scan get much smaller caps than the CLI's
# defaults - a quick, bounded check rather than the CLI's deeper one. All
# three are individually disableable if a deployment finds it needs to.
WEB_FIRST_BUYERS_LIMIT = int(os.environ.get("FIRSTCALLOOR_FIRST_BUYERS_LIMIT", "5"))
WEB_FIRST_BUYERS_SCAN_CAP = int(os.environ.get("FIRSTCALLOOR_FIRST_BUYERS_SCAN_CAP", "6"))
WEB_DEV_SCAN_CAP = int(os.environ.get("FIRSTCALLOOR_DEV_SCAN_CAP", "8"))
WEB_DEV_SCAN_MAX_LAUNCHES = int(os.environ.get("FIRSTCALLOOR_DEV_SCAN_MAX_LAUNCHES", "2"))
WEB_NO_FIRST_BUYERS = os.environ.get("FIRSTCALLOOR_NO_FIRST_BUYERS", "0").lower() in ("1", "true", "yes", "on")
# The dev-wallet scan is on by default now that dev history is part of what
# the page is expected to show. It is paid for by dropping the pump.fun
# cross-check below, which cost strictly more (up to a 6s timeout) and gave
# strictly less: its only unique value was the token name, and that now
# comes off-chain-free from the mint account itself.
WEB_NO_DEV_SCAN = os.environ.get("FIRSTCALLOOR_NO_DEV_SCAN", "0").lower() in ("1", "true", "yes", "on")
WEB_NO_RISK_CHECK = os.environ.get("FIRSTCALLOOR_NO_RISK_CHECK", "0").lower() in ("1", "true", "yes", "on")
WEB_NO_NAME_CHECK = os.environ.get("FIRSTCALLOOR_NO_NAME_CHECK", "0").lower() in ("1", "true", "yes", "on")
# pump.fun's API has returned 530 on every single call this project has ever
# made to it, and a hanging attempt can eat 6s of a ~10s budget for an
# advisory timestamp cross-check. The token name - the only thing it gave
# that mattered - is now read from the chain, so the web path skips it.
WEB_NO_CROSS_CHECK = os.environ.get("FIRSTCALLOOR_NO_CROSS_CHECK", "1").lower() in ("1", "true", "yes", "on")


# The whole run has to finish inside vercel.json's maxDuration (10s on
# Hobby), and overrunning it is not a slow page - Vercel kills the process
# and the browser gets the platform's opaque crash page instead of this
# file's JSON. HttpBudget above bounds any single request; this bounds the
# run, which is the different failure of many individually-fine calls adding
# up. The default leaves ~2.5s of headroom for process start, JSON encoding
# and the response itself.
WEB_TIME_BUDGET = float(os.environ.get("FIRSTCALLOOR_TIME_BUDGET", "7.5"))


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
        pre_window_hours=WEB_PRE_WINDOW_HOURS,
        no_cross_check=WEB_NO_CROSS_CHECK,
        no_first_buyers=WEB_NO_FIRST_BUYERS,
        no_dev_scan=WEB_NO_DEV_SCAN,
        no_risk_check=WEB_NO_RISK_CHECK,
        no_name_check=WEB_NO_NAME_CHECK,
        time_budget=WEB_TIME_BUDGET,
        first_buyers_limit=WEB_FIRST_BUYERS_LIMIT,
        first_buyers_scan_cap=WEB_FIRST_BUYERS_SCAN_CAP,
        dev_scan_cap=WEB_DEV_SCAN_CAP,
        dev_scan_max_launches=WEB_DEV_SCAN_MAX_LAUNCHES,
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
        # analyze_request() already catches everything from validation and
        # run_analysis, but this outer guard is the backstop for anything
        # else unanticipated (a bad query string, a bug in this dispatch
        # code itself) - the one thing this handler must never do is let an
        # exception escape and produce something other than valid JSON.
        try:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            status, body = analyze_request(query)
        except Exception as exc:  # noqa: BLE001 - last-resort, see above
            status, body = 500, {"error": "internal", "message": f"{type(exc).__name__}: {exc}"}

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
