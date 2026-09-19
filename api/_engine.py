#!/usr/bin/env python3
"""
FIRSTCALLOOR - "First Mentioned By" for Solana memecoins.

Given a token's contract address (mint), establish a trustworthy "zero point"
from the token's on-chain creation timestamp, search X for mentions of that
exact CA string, filter out the noise, and rank who called it first.

Usage:
    python firstcalloor.py <CONTRACT_ADDRESS>
    python firstcalloor.py <CONTRACT_ADDRESS> --mock        # no X key needed
    python firstcalloor.py <CONTRACT_ADDRESS> --json out.json

Stdlib only - no pip install required.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

VERSION = "1.0.0"

DEFAULT_RPC = "https://api.mainnet-beta.solana.com"
PUMPFUN_API = "https://frontend-api.pump.fun/coins/{mint}"
X_SEARCH_RECENT = "https://api.x.com/2/tweets/search/recent"
X_SEARCH_ALL = "https://api.x.com/2/tweets/search/all"

B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
B58_INDEX = {c: i for i, c in enumerate(B58_ALPHABET)}

# A mint account is 32 bytes; base58 of 32 bytes is 43 or 44 characters.
MIN_CA_LEN = 32
MAX_CA_LEN = 44

CROSS_CHECK_TOLERANCE_S = 60
DEFAULT_TWEET_CAP = 200
DEFAULT_SIG_PAGE_CAP = 50          # 50 pages x 1000 sigs = 50k transactions
SIG_PAGE_SIZE = 1000
NEW_ACCOUNT_BOT_WINDOW_H = 24
TIMELINE_SIZE = 10
HTTP_TIMEOUT = 30
MAX_RETRIES = 4

USER_AGENT = f"firstcalloor/{VERSION}"


# --------------------------------------------------------------------------
# small utilities
# --------------------------------------------------------------------------

class FirstCallooorError(Exception):
    """Fatal, user-facing error. Printed without a traceback."""


class HttpError(Exception):
    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"HTTP {status} from {url}")
        self.status = status
        self.body = body
        self.url = url


def b58_decode(s: str) -> bytes:
    """Decode base58. Raises ValueError on a bad character."""
    num = 0
    for ch in s:
        if ch not in B58_INDEX:
            raise ValueError(f"invalid base58 character {ch!r}")
        num = num * 58 + B58_INDEX[ch]
    raw = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\0" * pad + raw


def utc_from_unix(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_x_time(value: str) -> datetime:
    """X returns RFC3339 like 2026-09-19T18:04:11.000Z."""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def fmt_delta(seconds: float) -> str:
    """4m12s / 1h03m / 2d 4h / -18m (negative = before launch)."""
    sign = "-" if seconds < 0 else ""
    s = int(abs(seconds))
    if s < 60:
        return f"{sign}{s}s"
    if s < 3600:
        return f"{sign}{s // 60}m{s % 60:02d}s"
    if s < 86400:
        return f"{sign}{s // 3600}h{(s % 3600) // 60:02d}m"
    return f"{sign}{s // 86400}d {(s % 86400) // 3600}h"


def fmt_count(n: Optional[int]) -> str:
    if n is None:
        return "?"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def squash(text: str, limit: int = 96) -> str:
    """Collapse whitespace and truncate for single-line display."""
    flat = re.sub(r"\s+", " ", text or "").strip()
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def http_json(
    url: str,
    *,
    method: str = "GET",
    payload: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout: int = HTTP_TIMEOUT,
    retries: int = MAX_RETRIES,
) -> Any:
    """JSON request with exponential backoff on 429/5xx and network errors.

    Raises HttpError for a non-retryable status, or after exhausting retries.
    """
    body = json.dumps(payload).encode() if payload is not None else None
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if body is not None:
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})

    delay = 2.0
    last: Optional[Exception] = None

    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace") or "null")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            err = HttpError(exc.code, raw, url)
            # 429 and 5xx are worth retrying; everything else is terminal.
            if exc.code != 429 and exc.code < 500:
                raise err
            last = err
            wait = delay
            retry_after = exc.headers.get("retry-after") if exc.headers else None
            if retry_after and retry_after.isdigit():
                wait = max(wait, float(retry_after))
            # X sends an epoch-seconds reset header on rate limit.
            reset = exc.headers.get("x-rate-limit-reset") if exc.headers else None
            if exc.code == 429 and reset and reset.isdigit():
                wait = max(wait, min(float(reset) - time.time(), 90.0))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            last = exc
            wait = delay

        if attempt == retries:
            break
        time.sleep(max(wait, 1.0))
        delay *= 2

    if isinstance(last, HttpError):
        raise last
    raise HttpError(0, str(last), url)


# --------------------------------------------------------------------------
# input validation - CA vs ticker
# --------------------------------------------------------------------------

TICKER_RE = re.compile(r"^\$?[A-Za-z0-9_]{1,15}$")


def validate_contract_address(raw: str) -> str:
    """Return a clean mint address, or raise with an actionable message.

    Tickers are rejected outright rather than guessed at: two tokens sharing a
    ticker is the single most common way this kind of tool reports the wrong
    'first call', and the wrong answer is worse than no answer.
    """
    ca = (raw or "").strip()
    if not ca:
        raise FirstCallooorError("No contract address supplied.")

    if ca.startswith("$") or (len(ca) < MIN_CA_LEN and TICKER_RE.match(ca)):
        raise FirstCallooorError(
            f"{ca!r} looks like a ticker, not a contract address.\n\n"
            "  FIRSTCALLOOR needs the CA because tickers collide - dozens of tokens\n"
            "  reuse the same name and guessing wrong gives you a confidently wrong\n"
            "  'first call'.\n\n"
            "  Grab the CA from the pump.fun token page (the long string under the\n"
            "  name) or from DexScreener, then run:\n"
            "      python firstcalloor.py <CONTRACT_ADDRESS>"
        )

    if not (MIN_CA_LEN <= len(ca) <= MAX_CA_LEN):
        raise FirstCallooorError(
            f"{ca!r} is {len(ca)} characters; a Solana address is "
            f"{MIN_CA_LEN}-{MAX_CA_LEN} base58 characters (pump.fun mints are "
            "usually 43-44 and end in 'pump')."
        )

    try:
        decoded = b58_decode(ca)
    except ValueError as exc:
        raise FirstCallooorError(
            f"{ca!r} is not valid base58 ({exc}).\n"
            "  Base58 excludes 0, O, I and l - a lookalike character is the usual "
            "cause when a CA is copied out of a screenshot."
        ) from exc

    if len(decoded) != 32:
        raise FirstCallooorError(
            f"{ca!r} decodes to {len(decoded)} bytes; a Solana account address "
            "is 32 bytes. Check for a truncated or padded copy-paste."
        )

    return ca


# --------------------------------------------------------------------------
# on-chain: the zero point
# --------------------------------------------------------------------------

@dataclass
class OnChainOrigin:
    mint: str
    created_unix: int
    created_iso: str
    genesis_signature: str
    signatures_scanned: int
    pages_scanned: int
    exhausted: bool                 # did we reach the true beginning of history?
    blocktime_source: str           # "getTransaction" | "getSignaturesForAddress"
    warnings: list[str] = field(default_factory=list)


class SolanaRPC:
    def __init__(self, endpoint: str, verbose: bool = False):
        self.endpoint = endpoint
        self.verbose = verbose
        self._id = 0

    def call(self, method: str, params: list) -> Any:
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        try:
            data = http_json(self.endpoint, method="POST", payload=payload)
        except HttpError as exc:
            if exc.status == 429:
                raise FirstCallooorError(
                    f"Solana RPC rate-limited us ({self.endpoint}).\n"
                    "  The public endpoint throttles hard. Set SOLANA_RPC_URL to a\n"
                    "  Helius/QuickNode free-tier endpoint and re-run."
                ) from exc
            raise FirstCallooorError(
                f"Solana RPC call {method} failed: HTTP {exc.status} {squash(exc.body, 200)}"
            ) from exc

        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            raise FirstCallooorError(
                f"Solana RPC error on {method}: "
                f"{err.get('message', err)} (code {err.get('code')})"
            )
        return (data or {}).get("result")

    def earliest_signature(
        self, mint: str, page_cap: int = DEFAULT_SIG_PAGE_CAP
    ) -> tuple[Optional[dict], int, int, bool]:
        """Walk signature history backwards to the mint's first transaction.

        getSignaturesForAddress returns newest-first, so we page with `before`
        until a short page tells us we've hit the beginning of history. The
        last entry of the last page is the mint's genesis transaction.

        Returns (earliest_sig_info, total_scanned, pages, exhausted).
        """
        before: Optional[str] = None
        earliest: Optional[dict] = None
        total = 0
        pages = 0

        while pages < page_cap:
            params: list[Any] = [mint, {"limit": SIG_PAGE_SIZE}]
            if before:
                params[1]["before"] = before
            batch = self.call("getSignaturesForAddress", params) or []
            pages += 1
            if not batch:
                return earliest, total, pages, True

            total += len(batch)
            earliest = batch[-1]
            before = earliest["signature"]
            if self.verbose:
                print(
                    f"  [rpc] page {pages}: {len(batch)} sigs "
                    f"(oldest so far {iso(utc_from_unix(earliest['blockTime']))})"
                    if earliest.get("blockTime")
                    else f"  [rpc] page {pages}: {len(batch)} sigs",
                    file=sys.stderr,
                )

            # A short page means there is nothing older left to fetch.
            if len(batch) < SIG_PAGE_SIZE:
                return earliest, total, pages, True

        return earliest, total, pages, False

    def block_time(self, signature: str) -> Optional[int]:
        """Authoritative block time for a signature, or None if unavailable.

        Never fatal: the signature listing already carries a blockTime, so a
        node that refuses this call costs us corroboration, not the answer.
        Nodes reject a transaction whose version exceeds the one we advertise,
        and they name the version they need in the error, so retry on that.
        """
        for max_version in (0, None):
            opts: dict[str, Any] = {"encoding": "json"}
            if max_version is not None:
                opts["maxSupportedTransactionVersion"] = max_version
            try:
                result = self.call("getTransaction", [signature, opts])
            except FirstCallooorError as exc:
                match = re.search(r"maxSupportedTransactionVersion\": (\d+)", str(exc))
                if match:
                    try:
                        result = self.call(
                            "getTransaction",
                            [signature, {
                                "encoding": "json",
                                "maxSupportedTransactionVersion": int(match.group(1)),
                            }],
                        )
                    except FirstCallooorError:
                        continue
                else:
                    continue
            return (result or {}).get("blockTime")
        return None


def resolve_origin(rpc: SolanaRPC, mint: str, page_cap: int) -> OnChainOrigin:
    warnings: list[str] = []
    earliest, scanned, pages, exhausted = rpc.earliest_signature(mint, page_cap)

    if earliest is None:
        raise FirstCallooorError(
            f"No transactions found for {mint}.\n"
            "  Either the address is not a token mint, the token does not exist on\n"
            "  mainnet, or the RPC has no history for it."
        )

    if not exhausted:
        warnings.append(
            f"Signature history was capped at {pages} pages ({scanned} transactions); "
            "the reported creation time may not be the true genesis. Re-run with "
            f"--sig-page-cap {page_cap * 4} for a deeper walk."
        )

    signature = earliest["signature"]

    # Prefer getTransaction for the authoritative block time, per the spec;
    # fall back to the blockTime the signature listing already carried.
    source = "getTransaction"
    block_time = rpc.block_time(signature)
    if block_time is None:
        block_time = earliest.get("blockTime")
        source = "getSignaturesForAddress"
        warnings.append(
            "getTransaction gave no blockTime for the genesis signature (pruned "
            "slot, or a transaction version this node will not serve); fell back "
            "to the block time carried by the signature listing."
        )

    if block_time is None:
        raise FirstCallooorError(
            f"Found the genesis transaction {signature} but no block time is "
            "available from this RPC. Try an archival endpoint (Helius free tier)."
        )

    return OnChainOrigin(
        mint=mint,
        created_unix=int(block_time),
        created_iso=iso(utc_from_unix(int(block_time))),
        genesis_signature=signature,
        signatures_scanned=scanned,
        pages_scanned=pages,
        exhausted=exhausted,
        blocktime_source=source,
        warnings=warnings,
    )


# --------------------------------------------------------------------------
# pump.fun cross-check (best effort - never fatal)
# --------------------------------------------------------------------------

@dataclass
class CrossCheck:
    status: str                      # agree | disagree | unavailable
    source: str = "pump.fun"
    reported_unix: Optional[int] = None
    reported_iso: Optional[str] = None
    delta_seconds: Optional[int] = None
    name: Optional[str] = None
    symbol: Optional[str] = None
    detail: Optional[str] = None


def cross_check_pumpfun(mint: str, onchain_unix: int) -> CrossCheck:
    try:
        data = http_json(PUMPFUN_API.format(mint=mint), retries=1)
    except HttpError as exc:
        return CrossCheck(
            status="unavailable",
            detail=f"pump.fun API returned HTTP {exc.status} "
                   "(its public endpoint is frequently behind Cloudflare).",
        )
    except Exception as exc:  # network stack, DNS, TLS
        return CrossCheck(status="unavailable", detail=f"pump.fun API unreachable: {exc}")

    if not isinstance(data, dict):
        return CrossCheck(status="unavailable", detail="pump.fun returned an unexpected payload.")

    raw = data.get("created_timestamp")
    name, symbol = data.get("name"), data.get("symbol")
    if raw is None:
        return CrossCheck(
            status="unavailable",
            name=name,
            symbol=symbol,
            detail="pump.fun response carried no created_timestamp field.",
        )

    reported = int(raw) // 1000 if int(raw) > 10**12 else int(raw)   # ms or s
    delta = reported - onchain_unix
    return CrossCheck(
        status="disagree" if abs(delta) > CROSS_CHECK_TOLERANCE_S else "agree",
        reported_unix=reported,
        reported_iso=iso(utc_from_unix(reported)),
        delta_seconds=delta,
        name=name,
        symbol=symbol,
    )


# --------------------------------------------------------------------------
# X (Twitter) search
# --------------------------------------------------------------------------

@dataclass
class Mention:
    tweet_id: str
    handle: str
    display_name: str
    author_id: str
    followers: Optional[int]
    account_created_iso: Optional[str]
    account_age_hours: Optional[float]
    created_iso: str
    created_unix: int
    seconds_since_creation: int
    url: str
    text: str
    is_retweet: bool = False
    is_quote: bool = False
    is_reply: bool = False
    flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "tweet_id": self.tweet_id,
            "handle": self.handle,
            "display_name": self.display_name,
            "author_id": self.author_id,
            "followers_at_query_time": self.followers,
            "account_created_at": self.account_created_iso,
            "account_age_hours": self.account_age_hours,
            "tweeted_at": self.created_iso,
            "tweeted_at_unix": self.created_unix,
            "seconds_since_token_creation": self.seconds_since_creation,
            "time_since_creation": fmt_delta(self.seconds_since_creation),
            "url": self.url,
            "text": self.text,
            "is_retweet": self.is_retweet,
            "is_quote": self.is_quote,
            "is_reply": self.is_reply,
            "flags": self.flags,
        }


@dataclass
class SearchOutcome:
    status: str                       # ok | partial | no_credentials | forbidden | error | mock
    mentions: list[Mention] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    complete: bool = True
    requests_made: int = 0
    window_start_iso: Optional[str] = None
    window_end_iso: Optional[str] = None
    query: Optional[str] = None
    endpoint: Optional[str] = None
    provider: Optional[str] = None    # "x" | "twitterapi.io" | "mock"


# Windows tried outward from launch until mentions appear. The first call for a
# pump.fun token lands within minutes, so starting tight keeps quota spend low.
EXPAND_WINDOWS_H = [1, 6, 24, 72, 168]
MIN_WINDOW_MINUTES = 5


class BaseMentionSearchClient:
    """Earliest-first search strategy, shared by every mention provider.

    X API v2 has no ascending sort, and neither does twitterapi.io's search -
    both return newest-first. Paginating a busy token that way would burn the
    whole tweet cap on late mentions and never reach the first call. So instead:

    1. Anchor a window at the launch timestamp.
    2. Expand it outward (1h -> 6h -> 24h -> 72h -> 7d) only until mentions
       appear - a fresh pump.fun token is usually called within minutes, so
       this keeps quota spend low.
    3. If that window overflows the tweet cap, contract it back toward launch
       until the earliest tweets fit inside it.

    Subclasses implement only `_fetch_window`, which knows one provider's
    request shape and response fields; this class never sees either.
    """

    provider_name = "base"
    cap = DEFAULT_TWEET_CAP
    verbose = False
    requests_made = 0
    max_lookback_days: Optional[int] = None   # None = provider has no lookback wall

    def _fetch_window(
        self, ca: str, start: datetime, end: datetime, cap: int, origin_dt: datetime
    ) -> tuple[list[Mention], bool, Optional[str]]:
        """Collect every mention in [start, end), newest-first, up to `cap`.

        `origin_dt` is the token's true creation time - NOT necessarily equal
        to `start`, which may have been clamped forward by a lookback wall.
        Every Mention's seconds_since_creation must be computed against
        `origin_dt`, never against `start`, or a clamped search silently
        mis-reports "seconds since launch" for every result it returns.

        Returns (mentions, hit_cap, rate_limit_note). Must be implemented by
        each provider subclass.
        """
        raise NotImplementedError

    def search(self, ca: str, origin_dt: datetime) -> SearchOutcome:
        now = datetime.now(timezone.utc)
        notes: list[str] = []
        start = origin_dt
        endpoint = getattr(self, "endpoint", self.provider_name)

        if self.max_lookback_days is not None:
            earliest_allowed = now - timedelta(days=self.max_lookback_days)
            if origin_dt < earliest_allowed:
                start = earliest_allowed + timedelta(seconds=30)
                notes.append(
                    f"This token launched more than {self.max_lookback_days} days ago, "
                    "which is outside this provider's lookback window. The search "
                    "window was clamped, so the true first call is almost certainly "
                    "NOT in these results."
                )

        window_h = EXPAND_WINDOWS_H[-1]
        mentions: list[Mention] = []
        hit_cap = False
        rate_note: Optional[str] = None

        # Expand outward from launch until we see something.
        for hours in EXPAND_WINDOWS_H:
            end = min(start + timedelta(hours=hours), now)
            if end <= start:
                continue
            window_h = hours
            mentions, hit_cap, rate_note = self._fetch_window(
                ca, start, end, self.cap, origin_dt
            )
            if mentions or rate_note:
                break

        # Contract back toward launch if the window overflowed our cap, so the
        # mentions we keep are the earliest rather than an arbitrary recent slice.
        while hit_cap and not rate_note and window_h * 60 > MIN_WINDOW_MINUTES:
            window_h = window_h / 4
            end = min(start + timedelta(hours=window_h), now)
            narrower, hit_cap, rate_note = self._fetch_window(
                ca, start, end, self.cap, origin_dt
            )
            if not narrower:
                # Nothing that close to launch; the wider set is the best we have.
                hit_cap = True
                notes.append(
                    f"Mention volume exceeded the {self.cap}-tweet cap and no mentions "
                    f"exist within {fmt_delta(window_h * 3600)} of launch, so the "
                    "earliest mentions shown may not be the absolute earliest."
                )
                break
            mentions = narrower

        if rate_note:
            notes.append(rate_note)

        end_dt = min(start + timedelta(hours=window_h), now)
        complete = not hit_cap and rate_note is None

        if hit_cap and not rate_note:
            notes.append(
                f"The {self.cap}-tweet cap was reached; raise it with --max-tweets "
                "if you want a deeper sweep."
            )

        return SearchOutcome(
            status="ok" if complete else "partial",
            mentions=mentions,
            notes=notes,
            complete=complete,
            requests_made=self.requests_made,
            window_start_iso=iso(start),
            window_end_iso=iso(end_dt),
            query=f'"{ca}"',
            endpoint=endpoint,
            provider=self.provider_name,
        )

    def probe_before_launch(
        self, ca: str, origin_dt: datetime, hours: int
    ) -> tuple[list[Mention], list[str]]:
        """Look for mentions that predate the token, to expose recycled CAs."""
        if hours <= 0:
            return [], []
        now = datetime.now(timezone.utc)
        start = origin_dt - timedelta(hours=hours)
        if self.max_lookback_days is not None:
            floor = now - timedelta(days=self.max_lookback_days) + timedelta(seconds=30)
            start = max(start, floor)
        if start >= origin_dt:
            return [], [
                "Pre-launch probe skipped: the window before launch falls outside "
                "this provider's lookback horizon."
            ]
        try:
            mentions, _, rate_note = self._fetch_window(
                ca, start, origin_dt, min(50, self.cap), origin_dt
            )
        except HttpError as exc:
            return [], [f"Pre-launch probe failed (HTTP {exc.status}); skipped."]
        return mentions, ([rate_note] if rate_note else [])


class XSearchClient(BaseMentionSearchClient):
    provider_name = "x"

    def __init__(
        self,
        bearer_token: str,
        *,
        full_archive: bool = False,
        cap: int = DEFAULT_TWEET_CAP,
        verbose: bool = False,
    ):
        self.token = bearer_token
        self.endpoint = X_SEARCH_ALL if full_archive else X_SEARCH_RECENT
        self.full_archive = full_archive
        self.max_lookback_days = None if full_archive else 7
        self.cap = cap
        self.verbose = verbose
        self.requests_made = 0

    # -- low level ---------------------------------------------------------

    def _page(self, params: dict) -> dict:
        url = f"{self.endpoint}?{urllib.parse.urlencode(params)}"
        self.requests_made += 1
        return http_json(url, headers={"Authorization": f"Bearer {self.token}"})

    def _fetch_window(
        self, ca: str, start: datetime, end: datetime, cap: int, origin_dt: datetime
    ) -> tuple[list[Mention], bool, Optional[str]]:
        tweets: list[dict] = []
        users: dict[str, dict] = {}
        next_token: Optional[str] = None
        rate_note: Optional[str] = None

        while len(tweets) < cap:
            params = {
                "query": f'"{ca}"',
                "max_results": str(min(100, max(10, cap - len(tweets)))),
                "sort_order": "recency",
                "start_time": iso(start),
                "end_time": iso(end),
                "tweet.fields": "created_at,author_id,referenced_tweets,text,lang,public_metrics",
                "expansions": "author_id",
                "user.fields": "created_at,username,name,public_metrics,verified",
            }
            if next_token:
                params["next_token"] = next_token

            try:
                data = self._page(params)
            except HttpError as exc:
                if exc.status == 429:
                    rate_note = (
                        "X API rate limit reached mid-pagination - the search was cut "
                        "short and these results may be incomplete."
                    )
                    break
                raise

            for user in (data.get("includes") or {}).get("users", []) or []:
                users[user["id"]] = user
            batch = data.get("data") or []
            tweets.extend(batch)

            if self.verbose:
                print(
                    f"  [x] {iso(start)} -> {iso(end)}: +{len(batch)} "
                    f"(total {len(tweets)})",
                    file=sys.stderr,
                )

            next_token = (data.get("meta") or {}).get("next_token")
            if not next_token or not batch:
                break
        else:
            return self._build_mentions(tweets, users, origin_dt), next_token is not None, rate_note

        return self._build_mentions(tweets, users, origin_dt), False, rate_note

    # -- shaping -----------------------------------------------------------

    def _build_mentions(
        self, tweets: list[dict], users: dict[str, dict], origin_dt: datetime
    ) -> list[Mention]:
        now = datetime.now(timezone.utc)
        out: list[Mention] = []
        for tw in tweets:
            author = users.get(tw.get("author_id", ""), {})
            handle = author.get("username") or "unknown"
            created = parse_x_time(tw["created_at"])

            acct_created_iso = None
            acct_age_h = None
            if author.get("created_at"):
                acct_dt = parse_x_time(author["created_at"])
                acct_created_iso = iso(acct_dt)
                acct_age_h = round((now - acct_dt).total_seconds() / 3600, 2)

            refs = tw.get("referenced_tweets") or []
            ref_types = {r.get("type") for r in refs}

            out.append(
                Mention(
                    tweet_id=tw["id"],
                    handle=handle,
                    display_name=author.get("name") or "",
                    author_id=tw.get("author_id", ""),
                    followers=(author.get("public_metrics") or {}).get("followers_count"),
                    account_created_iso=acct_created_iso,
                    account_age_hours=acct_age_h,
                    created_iso=iso(created),
                    created_unix=int(created.timestamp()),
                    seconds_since_creation=int((created - origin_dt).total_seconds()),
                    url=f"https://x.com/{handle}/status/{tw['id']}",
                    text=tw.get("text", ""),
                    is_retweet="retweeted" in ref_types,
                    is_quote="quoted" in ref_types,
                    is_reply="replied_to" in ref_types,
                )
            )
        return out


# --------------------------------------------------------------------------
# twitterapi.io - third-party X search, no official-tier 7-day wall
# --------------------------------------------------------------------------

TWITTERAPI_IO_SEARCH = "https://api.twitterapi.io/twitter/tweet/advanced_search"

# Twitter's classic tweet timestamp format, e.g. "Wed Sep 19 18:30:16 +0000 2026".
# twitterapi.io passes this format through as-is rather than converting to ISO.
_TWITTERAPI_IO_DATE_FMT = "%a %b %d %H:%M:%S %z %Y"


def _parse_twitterapi_io_time(value: str) -> datetime:
    """Parse a twitterapi.io timestamp, tolerating ISO 8601 as a fallback.

    Not verified against a live response (this sandbox cannot reach
    api.twitterapi.io) - built from the documented/expected response shape.
    If the real API returns something this can't parse, the error message
    names the offending value so it's a quick fix rather than a mystery.
    """
    try:
        return datetime.strptime(value, _TWITTERAPI_IO_DATE_FMT).astimezone(timezone.utc)
    except ValueError:
        pass
    try:
        return parse_x_time(value)
    except ValueError as exc:
        raise FirstCallooorError(
            f"twitterapi.io returned a timestamp we don't recognise: {value!r} "
            f"({exc}). The response schema may have changed - please report this "
            "value so the parser can be updated."
        ) from exc


def _pick(d: dict, *keys, default=None):
    """First present key from a dict, tolerating a couple of naming variants.

    twitterapi.io's exact field names are taken from its documented shape,
    which this sandbox cannot call live to confirm; this hedges against a
    plausible camelCase/snake_case mismatch without guessing wildly.
    """
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


class TwitterAPIIOClient(BaseMentionSearchClient):
    """Third-party X search via twitterapi.io (api.twitterapi.io).

    Unofficial reseller of X's search index, priced per request/credit rather
    than a fixed monthly tier. Used here because it has no equivalent of the
    official API's 7-day recent-search wall, which is what actually blocks
    "first call" lookups on a token that already ran. Time-boxes a query with
    the `since_time:`/`until_time:` (unix epoch) search operators rather than
    dedicated start/end parameters.
    """

    provider_name = "twitterapi.io"
    endpoint = TWITTERAPI_IO_SEARCH
    max_lookback_days = None   # no recent-search wall on this provider

    def __init__(self, api_key: str, *, cap: int = DEFAULT_TWEET_CAP, verbose: bool = False):
        self.api_key = api_key
        self.cap = cap
        self.verbose = verbose
        self.requests_made = 0

    def _page(self, query: str, cursor: str) -> dict:
        params = {"query": query, "queryType": "Latest"}
        if cursor:
            params["cursor"] = cursor
        url = f"{self.endpoint}?{urllib.parse.urlencode(params)}"
        self.requests_made += 1
        return http_json(url, headers={"X-API-Key": self.api_key})

    def _fetch_window(
        self, ca: str, start: datetime, end: datetime, cap: int, origin_dt: datetime
    ) -> tuple[list[Mention], bool, Optional[str]]:
        query = f'"{ca}" since_time:{int(start.timestamp())} until_time:{int(end.timestamp())}'
        raw: list[dict] = []
        cursor = ""
        rate_note: Optional[str] = None
        has_next = False

        # Unlike X's max_results, twitterapi.io's advanced_search takes no
        # page-size parameter - a single page can overshoot `cap` outright.
        # So "the server says there's no more" is NOT the same claim as "we
        # captured everything": if this page alone exceeded cap, results were
        # truncated client-side and must still be reported as hit_cap=True,
        # or a busy window silently drops exactly the earliest tweets in it.
        while len(raw) < cap:
            try:
                data = self._page(query, cursor)
            except HttpError as exc:
                if exc.status == 429:
                    rate_note = (
                        "twitterapi.io rate limit reached mid-pagination - the search "
                        "was cut short and these results may be incomplete."
                    )
                    break
                raise

            batch = _pick(data, "tweets", "data", default=[]) or []
            raw.extend(batch)

            if self.verbose:
                print(
                    f"  [twitterapi.io] {iso(start)} -> {iso(end)}: +{len(batch)} "
                    f"(total {len(raw)})",
                    file=sys.stderr,
                )

            has_next = bool(_pick(data, "has_next_page", "hasNextPage", default=False))
            cursor = _pick(data, "next_cursor", "nextCursor", default="") or ""
            if not has_next or not cursor or not batch:
                break

        hit_cap = has_next or len(raw) > cap
        return self._build_mentions(raw[:cap], origin_dt), hit_cap, rate_note

    # -- shaping -----------------------------------------------------------

    def _build_mentions(self, tweets: list[dict], origin_dt: datetime) -> list[Mention]:
        now = datetime.now(timezone.utc)
        out: list[Mention] = []
        for tw in tweets:
            author = _pick(tw, "author", "user", default={}) or {}
            handle = _pick(author, "userName", "username", "screen_name", default="unknown")
            created_raw = _pick(tw, "createdAt", "created_at")
            if not created_raw:
                continue
            created = _parse_twitterapi_io_time(created_raw)

            acct_created_iso = None
            acct_age_h = None
            acct_raw = _pick(author, "createdAt", "created_at")
            if acct_raw:
                acct_dt = _parse_twitterapi_io_time(acct_raw)
                acct_created_iso = iso(acct_dt)
                acct_age_h = round((now - acct_dt).total_seconds() / 3600, 2)

            tweet_id = str(_pick(tw, "id", "tweetId", "id_str", default=""))
            is_retweet = bool(
                _pick(tw, "isRetweet", default=False) or _pick(tw, "retweeted_tweet")
            )
            is_quote = bool(
                _pick(tw, "isQuote", default=False) or _pick(tw, "quoted_tweet")
            )
            is_reply = bool(
                _pick(tw, "isReply", default=False) or _pick(tw, "inReplyToId")
            )

            out.append(
                Mention(
                    tweet_id=tweet_id,
                    handle=handle,
                    display_name=_pick(author, "name", default="") or "",
                    author_id=str(_pick(author, "id", "userId", default="")),
                    followers=_pick(author, "followers", "followers_count", "followersCount"),
                    account_created_iso=acct_created_iso,
                    account_age_hours=acct_age_h,
                    created_iso=iso(created),
                    created_unix=int(created.timestamp()),
                    seconds_since_creation=int((created - origin_dt).total_seconds()),
                    url=_pick(tw, "url", "twitterUrl", default=f"https://x.com/{handle}/status/{tweet_id}"),
                    text=_pick(tw, "text", "full_text", default=""),
                    is_retweet=is_retweet,
                    is_quote=is_quote,
                    is_reply=is_reply,
                )
            )
        return out


# --------------------------------------------------------------------------
# mock mode - exercises every filter path without an X key
# --------------------------------------------------------------------------

def mock_search(ca: str, origin_dt: datetime, seed: int = 7) -> SearchOutcome:
    rng = random.Random(seed)
    now = datetime.now(timezone.utc)

    specs = [
        # (handle, offset_seconds, followers, account_age_hours, kind, text)
        ("degen_scanner", -4200, 812, 9000, "plain",
         f"recycled post from an old unrelated listing referencing {ca}"),
        ("chain_sniffer", 41, 14820, 26000, "plain",
         f"new pump.fun mint just deployed {ca} - dev holds 3%, bonding curve fresh"),
        ("freshwallet9931", 58, 4, 6, "plain", f"{ca} 100x incoming LFG"),
        ("solcallsdaily", 252, 61400, 41000, "plain",
         f"calling this one early {ca} chart looks clean, low mcap"),
        ("memecoinmike", 611, 128900, 52000, "retweet",
         f"RT @solcallsdaily: calling this one early {ca}"),
        ("alpha_mora", 794, 23150, 18000, "plain",
         f"in at 28k mcap {ca} - watching for the first 100k retest"),
        ("botnet_shill02", 880, 12, 11, "plain", f"{ca} {ca} buy now buy now"),
        ("cryptotia", 1902, 340200, 63000, "plain",
         f"ok this {ca} is actually moving, 400k mcap and holding"),
        ("nftwhale", 3340, 88400, 47000, "quote",
         f"this aged well {ca}"),
        ("latecaller", 7810, 5120, 30000, "plain", f"still early on {ca}?"),
        ("bagholder_j", 14400, 2310, 22000, "reply", f"@alpha_mora what's your exit on {ca}"),
        ("trenchreport", 19980, 44100, 35000, "plain",
         f"{ca} did 12x from first call. congrats to anyone who caught it"),
    ]

    mentions: list[Mention] = []
    for handle, offset, followers, age_h, kind, text in specs:
        created = origin_dt + timedelta(seconds=offset)
        tweet_id = str(rng.randrange(10**18, 10**19))
        mentions.append(
            Mention(
                tweet_id=tweet_id,
                handle=handle,
                display_name=handle.replace("_", " ").title(),
                author_id=str(rng.randrange(10**8, 10**9)),
                followers=followers,
                account_created_iso=iso(now - timedelta(hours=age_h)),
                account_age_hours=float(age_h),
                created_iso=iso(created),
                created_unix=int(created.timestamp()),
                seconds_since_creation=offset,
                url=f"https://x.com/{handle}/status/{tweet_id}",
                text=text,
                is_retweet=(kind == "retweet"),
                is_quote=(kind == "quote"),
                is_reply=(kind == "reply"),
            )
        )

    return SearchOutcome(
        status="mock",
        mentions=mentions,
        notes=[
            "MOCK DATA - no X API call was made. The on-chain timestamp above is "
            "real; the mentions below are synthetic fixtures used to exercise the "
            "filtering, bot-flagging and ranking logic."
        ],
        complete=True,
        requests_made=0,
        provider="mock",
        window_start_iso=iso(origin_dt - timedelta(hours=2)),
        window_end_iso=iso(origin_dt + timedelta(hours=6)),
        query=f'"{ca}"',
        endpoint="mock://fixtures",
    )


# --------------------------------------------------------------------------
# triage: what counts as a genuine call
# --------------------------------------------------------------------------

@dataclass
class Triage:
    timeline: list[Mention] = field(default_factory=list)
    possible_bots: list[Mention] = field(default_factory=list)
    pre_creation: list[Mention] = field(default_factory=list)
    retweets: list[Mention] = field(default_factory=list)
    quotes: list[Mention] = field(default_factory=list)
    duplicates: int = 0


def triage_mentions(
    mentions: list[Mention],
    *,
    include_retweets: bool = False,
    bot_window_h: float = NEW_ACCOUNT_BOT_WINDOW_H,
) -> Triage:
    result = Triage()
    seen: set[str] = set()

    for m in sorted(mentions, key=lambda x: x.created_unix):
        if m.tweet_id in seen:
            result.duplicates += 1
            continue
        seen.add(m.tweet_id)

        # 1. Anything predating the mint cannot be a call on this token.
        if m.seconds_since_creation < 0:
            m.flags.append("pre_creation")
            result.pre_creation.append(m)
            continue

        # 2. Amplification is not an original call.
        if m.is_retweet and not include_retweets:
            m.flags.append("retweet")
            result.retweets.append(m)
            continue
        if m.is_quote and not include_retweets:
            m.flags.append("quote_tweet")
            result.quotes.append(m)
            continue

        # 3. Brand-new accounts get surfaced, never silently trusted.
        if m.account_age_hours is not None and m.account_age_hours < bot_window_h:
            m.flags.append("account_under_24h")
            result.possible_bots.append(m)
            continue

        if m.account_age_hours is None:
            m.flags.append("account_age_unknown")
        result.timeline.append(m)

    return result


# --------------------------------------------------------------------------
# console rendering
# --------------------------------------------------------------------------

_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def bold(t: str) -> str:
    return c(t, "1")


def dim(t: str) -> str:
    return c(t, "2")


def green(t: str) -> str:
    return c(t, "32")


def yellow(t: str) -> str:
    return c(t, "33")


def red(t: str) -> str:
    return c(t, "31")


def rule(width: int = 78) -> str:
    return dim("-" * width)


def section(title: str) -> None:
    print()
    print(bold(title.upper()))
    print(rule())


def render(report: dict, triage: Triage, outcome: SearchOutcome, origin: OnChainOrigin,
           cross: CrossCheck) -> None:
    print()
    print(bold("FIRSTCALLOOR") + dim(f" v{VERSION}  -  first mentioned by, for solana memecoins"))

    # -- token -------------------------------------------------------------
    section("token")
    label = f"{cross.name} (${cross.symbol})" if cross.name and cross.symbol else dim("not resolved")
    print(f"  {'CA':<18}{origin.mint}")
    print(f"  {'Name':<18}{label}")
    print(f"  {'Created':<18}{green(origin.created_iso)}  {dim('<- zero point (on-chain)')}")
    print(f"  {'Genesis tx':<18}{dim(origin.genesis_signature)}")
    scanned = f"{origin.signatures_scanned:,} txs over {origin.pages_scanned} page(s)"
    completeness = green("reached genesis") if origin.exhausted else yellow("CAPPED - may not be genesis")
    print(f"  {'History':<18}{scanned}  [{completeness}]")

    if cross.status == "agree":
        print(f"  {'Cross-check':<18}{green('pump.fun agrees')} {dim(f'(delta {cross.delta_seconds}s)')}")
    elif cross.status == "disagree":
        print(f"  {'Cross-check':<18}{yellow('pump.fun DISAGREES')} by {cross.delta_seconds}s "
              f"{dim(f'(it reports {cross.reported_iso})')}")
        print(f"  {'':<18}{dim('The on-chain time above is what FIRSTCALLOOR trusts.')}")
    else:
        print(f"  {'Cross-check':<18}{dim('unavailable - ' + (cross.detail or 'no data'))}")

    for w in origin.warnings:
        print(f"  {yellow('!')} {w}")

    # -- search ------------------------------------------------------------
    section("search")
    provider_label = {"x": "X API v2", "twitterapi.io": "twitterapi.io", "mock": "mock fixtures"}.get(
        outcome.provider, outcome.provider or "-"
    )
    print(f"  {'Provider':<18}{provider_label}")
    print(f"  {'Query':<18}{outcome.query}   {dim('(exact CA string, never the ticker)')}")
    print(f"  {'Endpoint':<18}{dim(outcome.endpoint or '-')}")
    print(f"  {'Window':<18}{outcome.window_start_iso}  ->  {outcome.window_end_iso}")
    print(f"  {'API requests':<18}{outcome.requests_made}")
    status_line = green("complete") if outcome.complete else yellow("INCOMPLETE")
    print(f"  {'Coverage':<18}{status_line}")
    for note in outcome.notes:
        print(f"  {yellow('!')} {note}")

    # -- first call --------------------------------------------------------
    section("first call")
    if not triage.timeline and outcome.status in ("no_credentials", "forbidden", "error"):
        # "we did not look" is a different claim from "nothing is there".
        print(f"  {yellow('Search did not run - no result.')}")
        print(f"  {dim('See the notes above; the on-chain zero point is still valid.')}")
    elif not triage.timeline:
        print(f"  {yellow('No public mentions found.')}")
        if not outcome.complete:
            print(f"  {dim('Note: the search was incomplete, so absence here is not proof of absence.')}")
        if triage.possible_bots:
            print(f"  {dim(f'{len(triage.possible_bots)} mention(s) came only from accounts under 24h old - see below.')}")
        if triage.pre_creation:
            print(f"  {dim(f'{len(triage.pre_creation)} mention(s) predated the mint and were filtered - see below.')}")
    else:
        first = triage.timeline[0]
        print(f"  {bold('@' + first.handle)}  {dim(fmt_count(first.followers) + ' followers')}")
        print(f"  {green('Called ' + fmt_delta(first.seconds_since_creation) + ' after launch')}"
              f"  {dim('(' + first.created_iso + ')')}")
        print(f"  {dim(first.url)}")
        print(f"  {squash(first.text, 140)}")

    # -- timeline ----------------------------------------------------------
    section(f"timeline  (earliest {min(TIMELINE_SIZE, len(triage.timeline))} genuine mentions)")
    if not triage.timeline:
        print(dim("  nothing to rank"))
    else:
        print(dim(f"  {'#':<3}{'HANDLE':<22}{'FOLLOWERS':>10}  {'TWEETED (UTC)':<22}{'+LAUNCH':>9}"))
        for i, m in enumerate(triage.timeline[:TIMELINE_SIZE], 1):
            print(f"  {i:<3}{('@' + m.handle):<22}{fmt_count(m.followers):>10}  "
                  f"{m.created_iso:<22}{fmt_delta(m.seconds_since_creation):>9}")
            print(dim(f"      {squash(m.text, 92)}"))
            print(dim(f"      {m.url}"))

    # -- flagged -----------------------------------------------------------
    if triage.possible_bots:
        section(f"possible bot mentions  ({len(triage.possible_bots)})")
        print(dim("  Accounts created in the last 24h. Surfaced, not ranked - a fresh"))
        print(dim("  account is usually a bot, but occasionally it is a real early caller."))
        for m in triage.possible_bots[:TIMELINE_SIZE]:
            age = f"{m.account_age_hours:.1f}h old" if m.account_age_hours is not None else "age unknown"
            print(f"  {yellow('~')} {('@' + m.handle):<22}{dim(age):<24}"
                  f"{fmt_delta(m.seconds_since_creation):>9}  {dim(m.url)}")

    if triage.pre_creation:
        section(f"filtered: mentions predating the mint  ({len(triage.pre_creation)})")
        print(dim("  These tweets are timestamped BEFORE the token existed on-chain, so they"))
        print(dim("  cannot be calls on it. Usual causes: a recycled or impersonated CA string"))
        print(dim("  from an unrelated token, a scraper reposting old text, or clock skew."))
        print(dim("  They are excluded from the ranking by design."))
        for m in triage.pre_creation[:TIMELINE_SIZE]:
            print(f"  {red('x')} {('@' + m.handle):<22}{m.created_iso:<22}"
                  f"{fmt_delta(m.seconds_since_creation):>9}  {dim(m.url)}")

    skipped = len(triage.retweets) + len(triage.quotes)
    if skipped:
        section(f"filtered: amplification  ({skipped})")
        print(dim(f"  {len(triage.retweets)} retweet(s) and {len(triage.quotes)} quote tweet(s) "
                  "excluded as non-original."))
        print(dim("  Pass --include-retweets to rank them alongside original posts."))

    print()


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def build_report(
    ca: str,
    origin: OnChainOrigin,
    cross: CrossCheck,
    outcome: SearchOutcome,
    triage: Triage,
    args: argparse.Namespace,
) -> dict:
    first = triage.timeline[0] if triage.timeline else None
    return {
        "tool": "firstcalloor",
        "version": VERSION,
        "generated_at": iso(datetime.now(timezone.utc)),
        "input": {"contract_address": ca, "include_retweets": args.include_retweets},
        "token": {"name": cross.name, "symbol": cross.symbol},
        "origin": {
            "created_at": origin.created_iso,
            "created_at_unix": origin.created_unix,
            "genesis_signature": origin.genesis_signature,
            "blocktime_source": origin.blocktime_source,
            "signatures_scanned": origin.signatures_scanned,
            "pages_scanned": origin.pages_scanned,
            "reached_genesis": origin.exhausted,
            "rpc_endpoint": args.rpc,
            "warnings": origin.warnings,
        },
        "cross_check": {
            "status": cross.status,
            "source": cross.source,
            "reported_at": cross.reported_iso,
            "delta_seconds": cross.delta_seconds,
            "tolerance_seconds": CROSS_CHECK_TOLERANCE_S,
            "detail": cross.detail,
        },
        "search": {
            "status": outcome.status,
            "provider": outcome.provider,
            "complete": outcome.complete,
            "endpoint": outcome.endpoint,
            "query": outcome.query,
            "window_start": outcome.window_start_iso,
            "window_end": outcome.window_end_iso,
            "api_requests": outcome.requests_made,
            "tweets_returned": len(outcome.mentions),
            "notes": outcome.notes,
        },
        "first_call": first.to_dict() if first else None,
        "timeline": [m.to_dict() for m in triage.timeline[:TIMELINE_SIZE]],
        "possible_bot_mentions": [m.to_dict() for m in triage.possible_bots],
        "filtered": {
            "pre_creation": [m.to_dict() for m in triage.pre_creation],
            "retweets": [m.to_dict() for m in triage.retweets],
            "quote_tweets": [m.to_dict() for m in triage.quotes],
            "duplicates_dropped": triage.duplicates,
        },
    }


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="firstcalloor",
        description="Find the earliest verifiable X mention of a Solana memecoin CA.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "environment:\n"
            "  TWITTERAPI_IO_KEY  twitterapi.io API key (no 7-day lookback wall)\n"
            "  X_BEARER_TOKEN     X API v2 bearer token (search access required)\n"
            "  SOLANA_RPC_URL     Solana RPC endpoint (default: public mainnet)\n\n"
            "provider (auto by default): twitterapi.io is preferred when configured,\n"
            "since it has no equivalent of the official API's 7-day recent-search\n"
            "wall; falls back to X, then to no search.\n\n"
            "exit codes:\n"
            "  0 complete   2 bad input   3 incomplete search   1 error\n"
        ),
    )
    p.add_argument("contract_address", help="Solana token mint address (base58, 32-44 chars)")
    p.add_argument("--mock", action="store_true",
                   help="use synthetic mentions instead of a real search (no key needed)")
    p.add_argument("--json", dest="json_path", default=None,
                   help="write the full result set here (default: firstcalloor-<ca8>.json)")
    p.add_argument("--rpc", default=os.environ.get("SOLANA_RPC_URL", DEFAULT_RPC),
                   help="Solana RPC endpoint")
    p.add_argument("--provider", choices=["auto", "x", "twitterapi"], default="auto",
                   help="mention-search provider (default: auto-detect from credentials)")
    p.add_argument("--twitterapi-key", default=None,
                   help="twitterapi.io API key (overrides TWITTERAPI_IO_KEY)")
    p.add_argument("--bearer-token", default=None, help="X API bearer token (overrides env)")
    p.add_argument("--full-archive", action="store_true",
                   help="use /2/tweets/search/all instead of recent search (X API Pro+; "
                        "no effect on twitterapi.io, which has no such split)")
    p.add_argument("--max-tweets", type=int, default=DEFAULT_TWEET_CAP,
                   help=f"cap on tweets fetched (default {DEFAULT_TWEET_CAP})")
    p.add_argument("--sig-page-cap", type=int, default=DEFAULT_SIG_PAGE_CAP,
                   help=f"max signature pages to walk (default {DEFAULT_SIG_PAGE_CAP})")
    p.add_argument("--include-retweets", action="store_true",
                   help="rank retweets and quote tweets as mentions")
    p.add_argument("--pre-window-hours", type=int, default=24,
                   help="hours before launch to probe for recycled-CA mentions (0 disables)")
    p.add_argument("--no-cross-check", action="store_true", help="skip the pump.fun cross-check")
    p.add_argument("--verbose", "-v", action="store_true", help="log pagination to stderr")
    return p.parse_args(argv)


def select_provider(args: argparse.Namespace) -> tuple[str, Optional[str]]:
    """Pick a mention-search provider and its credential.

    Returns (provider, credential); provider is "x", "twitterapi.io", or
    "none". An explicit --provider wins. Auto-detection prefers twitterapi.io
    when configured, since - unlike the official API's free/Basic tiers - it
    has no 7-day recent-search wall, which is what actually blocks a "first
    call" lookup on a token that already ran; it falls back to X, then to no
    search at all.
    """
    x_token = getattr(args, "bearer_token", None) or os.environ.get(
        "X_BEARER_TOKEN"
    ) or os.environ.get("TWITTER_BEARER_TOKEN")
    ta_key = getattr(args, "twitterapi_key", None) or os.environ.get(
        "TWITTERAPI_IO_KEY"
    ) or os.environ.get("TWITTERAPI_KEY")

    requested = getattr(args, "provider", "auto")
    if requested == "x":
        return ("x", x_token) if x_token else ("none", None)
    if requested == "twitterapi":
        return ("twitterapi.io", ta_key) if ta_key else ("none", None)

    if ta_key:
        return "twitterapi.io", ta_key
    if x_token:
        return "x", x_token
    return "none", None


def _build_search_client(provider: str, credential: str, args: argparse.Namespace):
    if provider == "twitterapi.io":
        return TwitterAPIIOClient(credential, cap=args.max_tweets, verbose=args.verbose)
    return XSearchClient(
        credential, full_archive=args.full_archive, cap=args.max_tweets, verbose=args.verbose
    )


def run_analysis(ca: str, args: argparse.Namespace) -> tuple[dict, Triage, SearchOutcome,
                                                              OnChainOrigin, CrossCheck]:
    """Full pipeline for an already-validated CA.

    Shared by the CLI and the serverless API so the website and the terminal
    can never disagree about what a result means.
    """
    provider, credential = select_provider(args)

    # 1. zero point, straight from the chain
    if args.verbose:
        print(f"[1/3] resolving on-chain creation time via {args.rpc}", file=sys.stderr)
    origin = resolve_origin(SolanaRPC(args.rpc, args.verbose), ca, args.sig_page_cap)
    origin_dt = utc_from_unix(origin.created_unix)

    # 2. cross-check (advisory only, never load-bearing)
    cross = (
        CrossCheck(status="unavailable", detail="skipped via --no-cross-check")
        if args.no_cross_check
        else cross_check_pumpfun(ca, origin.created_unix)
    )

    # 3. mention search
    now_iso = iso(datetime.now(timezone.utc))

    def stub(status: str, notes: list[str], requests_made: int = 0,
              endpoint: Optional[str] = None) -> SearchOutcome:
        return SearchOutcome(
            status=status,
            complete=False,
            query=f'"{ca}"',
            endpoint=endpoint,
            window_start_iso=iso(origin_dt),
            window_end_iso=now_iso,
            requests_made=requests_made,
            notes=notes,
            provider=None if provider == "none" else provider,
        )

    if getattr(args, "mock", False):
        outcome = mock_search(ca, origin_dt)
    elif provider == "none":
        outcome = stub("no_credentials", [
            "No mention-search credentials are configured, so no search ran. The "
            "on-chain zero point above is still accurate.",
            "Set TWITTERAPI_IO_KEY (twitterapi.io - no 7-day lookback wall) or "
            "X_BEARER_TOKEN (official X API - the free tier has no search access "
            "at all) to enable it.",
        ])
    else:
        client = _build_search_client(provider, credential, args)
        try:
            outcome = client.search(ca, origin_dt)
            pre, pre_notes = client.probe_before_launch(ca, origin_dt, args.pre_window_hours)
            outcome.mentions.extend(pre)
            outcome.notes.extend(pre_notes)
            outcome.requests_made = client.requests_made
        except (HttpError, FirstCallooorError) as exc:
            # A search-stage failure degrades the result, it never aborts the
            # run - the on-chain half above is still real and worth reporting.
            endpoint = getattr(client, "endpoint", None)
            if isinstance(exc, HttpError) and exc.status in (401, 403):
                hints = {
                    "x": (
                        "X returned 403. The free tier has no search access at all; "
                        "recent search needs Basic or above, and full-archive search "
                        "needs Pro."
                        if exc.status == 403 else
                        "X returned 401 - the bearer token was rejected. Check that "
                        "X_BEARER_TOKEN is the app's Bearer Token, not an API key or "
                        "secret."
                    ),
                    "twitterapi.io": (
                        f"twitterapi.io returned {exc.status}. Check that "
                        "TWITTERAPI_IO_KEY is correct and the account still has "
                        "credits remaining."
                    ),
                }
                outcome = stub("forbidden", [hints[provider]], client.requests_made, endpoint)
            elif isinstance(exc, HttpError):
                outcome = stub(
                    "error",
                    [f"{provider} search failed: HTTP {exc.status} {squash(exc.body, 200)}"],
                    client.requests_made, endpoint,
                )
            else:
                outcome = stub("error", [str(exc)], client.requests_made, endpoint)

    triage = triage_mentions(outcome.mentions, include_retweets=args.include_retweets)
    report = build_report(ca, origin, cross, outcome, triage, args)
    return report, triage, outcome, origin, cross


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)

    try:
        ca = validate_contract_address(args.contract_address)
    except FirstCallooorError as exc:
        print(f"\n{red('input error')}\n  {exc}\n", file=sys.stderr)
        return 2

    try:
        report, triage, outcome, origin, cross = run_analysis(ca, args)
    except FirstCallooorError as exc:
        print(f"\n{red('error')}\n  {exc}\n", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 1

    render(report, triage, outcome, origin, cross)

    out_path = args.json_path or f"firstcalloor-{ca[:8]}.json"
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
    print(dim(f"  full result set written to {out_path}"))
    print()

    return 0 if outcome.complete else 3


if __name__ == "__main__":
    sys.exit(main())
