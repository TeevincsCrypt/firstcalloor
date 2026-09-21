"""Tests for firstcalloor.

The X API and pump.fun are stubbed at the HTTP layer, so the search strategy
(window expansion, cap contraction, rate-limit handling) is verified without
network access or an API key.
"""

import argparse
import base64
import os
import re
import sys
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))

import _engine as fc


VALID_CA = "6g7YqRniiD5fqCs9uW61QKjmsc6SYgR14mY5sNPzpump"


# --------------------------------------------------------------------------
# input validation
# --------------------------------------------------------------------------

class TestValidation(unittest.TestCase):
    def test_accepts_pumpfun_mint(self):
        self.assertEqual(fc.validate_contract_address(f"  {VALID_CA} "), VALID_CA)

    def test_accepts_43_char_address(self):
        wsol = "So11111111111111111111111111111111111111112"
        self.assertEqual(fc.validate_contract_address(wsol), wsol)

    def test_rejects_dollar_ticker(self):
        with self.assertRaises(fc.FirstCallooorError) as ctx:
            fc.validate_contract_address("$WIF")
        self.assertIn("looks like a ticker", str(ctx.exception))

    def test_rejects_bare_ticker(self):
        for ticker in ("WIF", "bonk", "PEPE2"):
            with self.assertRaises(fc.FirstCallooorError):
                fc.validate_contract_address(ticker)

    def test_rejects_base58_lookalike_characters(self):
        with self.assertRaises(fc.FirstCallooorError) as ctx:
            fc.validate_contract_address(VALID_CA[:-1] + "0")
        self.assertIn("base58", str(ctx.exception))

    def test_rejects_wrong_byte_length(self):
        # Valid base58, right character count, but not 32 bytes decoded.
        with self.assertRaises(fc.FirstCallooorError):
            fc.validate_contract_address("1" * 44)

    def test_rejects_empty(self):
        with self.assertRaises(fc.FirstCallooorError):
            fc.validate_contract_address("")


class TestFormatting(unittest.TestCase):
    def test_delta(self):
        self.assertEqual(fc.fmt_delta(41), "41s")
        self.assertEqual(fc.fmt_delta(252), "4m12s")
        self.assertEqual(fc.fmt_delta(3700), "1h01m")
        self.assertEqual(fc.fmt_delta(-4200), "-1h10m")

    def test_counts(self):
        self.assertEqual(fc.fmt_count(4), "4")
        self.assertEqual(fc.fmt_count(14820), "14.8K")
        self.assertEqual(fc.fmt_count(None), "?")

    def test_squash_truncates_and_flattens(self):
        self.assertEqual(fc.squash("a\n\n  b"), "a b")
        self.assertEqual(len(fc.squash("x" * 200, 20)), 20)


# --------------------------------------------------------------------------
# triage
# --------------------------------------------------------------------------

def mention(handle, offset, *, age_h=5000.0, rt=False, quote=False, tid=None):
    origin = datetime(2026, 9, 19, 18, 0, 0, tzinfo=timezone.utc)
    created = origin + timedelta(seconds=offset)
    return fc.Mention(
        tweet_id=tid or f"{handle}-{offset}",
        handle=handle,
        display_name=handle,
        author_id=handle,
        followers=1000,
        account_created_iso=fc.iso(created - timedelta(hours=age_h)),
        account_age_hours=age_h,
        created_iso=fc.iso(created),
        created_unix=int(created.timestamp()),
        seconds_since_creation=offset,
        url=f"https://x.com/{handle}/status/1",
        text="text",
        is_retweet=rt,
        is_quote=quote,
    )


class TestTriage(unittest.TestCase):
    def test_pre_creation_is_filtered_not_ranked(self):
        t = fc.triage_mentions([mention("old", -600), mention("real", 60)])
        self.assertEqual([m.handle for m in t.timeline], ["real"])
        self.assertEqual([m.handle for m in t.pre_creation], ["old"])
        self.assertIn("pre_creation", t.pre_creation[0].flags)

    def test_retweets_and_quotes_excluded_by_default(self):
        t = fc.triage_mentions([mention("a", 10), mention("b", 20, rt=True), mention("c", 30, quote=True)])
        self.assertEqual([m.handle for m in t.timeline], ["a"])
        self.assertEqual(len(t.retweets), 1)
        self.assertEqual(len(t.quotes), 1)

    def test_retweets_included_on_request(self):
        t = fc.triage_mentions(
            [mention("a", 10), mention("b", 20, rt=True)], include_retweets=True
        )
        self.assertEqual([m.handle for m in t.timeline], ["a", "b"])

    def test_fresh_accounts_surfaced_separately_never_ranked(self):
        t = fc.triage_mentions([mention("bot", 5, age_h=3.0), mention("human", 90)])
        self.assertEqual([m.handle for m in t.timeline], ["human"])
        self.assertEqual([m.handle for m in t.possible_bots], ["bot"])
        self.assertIn("account_under_24h", t.possible_bots[0].flags)

    def test_timeline_is_ordered_earliest_first(self):
        t = fc.triage_mentions([mention("c", 300), mention("a", 10), mention("b", 60)])
        self.assertEqual([m.handle for m in t.timeline], ["a", "b", "c"])

    def test_duplicate_tweet_ids_dropped_once(self):
        t = fc.triage_mentions([mention("a", 10, tid="X"), mention("a", 10, tid="X")])
        self.assertEqual(len(t.timeline), 1)
        self.assertEqual(t.duplicates, 1)

    def test_unknown_account_age_is_ranked_but_flagged(self):
        m = mention("a", 10)
        m.account_age_hours = None
        t = fc.triage_mentions([m])
        self.assertEqual(len(t.timeline), 1)
        self.assertIn("account_age_unknown", t.timeline[0].flags)


# --------------------------------------------------------------------------
# HttpBudget.retry_on_429 - regression coverage for a second production
# incident: even after cutting search widths from 5 to 3 and adding pacing,
# a rate limit still recurred, because each rate-limited window was actually
# costing TWO requests (the original attempt plus one retry) - retrying a
# 429 within a ~10s serverless budget can never land inside a real rate
# limit reset (typically tens of seconds), so it only doubles pressure on an
# already-tripped limiter for a wait that was doomed from the start.
# --------------------------------------------------------------------------

class TestRetryOn429(unittest.TestCase):
    def setUp(self):
        self._real_urlopen = fc.urllib.request.urlopen
        self._real_sleep = fc.time.sleep
        self._budget_snapshot = vars(fc.HttpBudget).copy()
        fc.time.sleep = lambda *_a, **_k: None

    def tearDown(self):
        fc.urllib.request.urlopen = self._real_urlopen
        fc.time.sleep = self._real_sleep
        for k, v in self._budget_snapshot.items():
            if not k.startswith("__"):
                setattr(fc.HttpBudget, k, v)

    def _always_429(self):
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(1)
            raise fc.urllib.error.HTTPError(req.full_url, 429, "rate limited", {}, None)

        fc.urllib.request.urlopen = fake_urlopen
        return calls

    def test_retries_once_by_default(self):
        calls = self._always_429()
        fc.HttpBudget.retry_on_429 = True
        fc.HttpBudget.retries = 1
        with self.assertRaises(fc.HttpError) as ctx:
            fc.http_json("https://example.test")
        self.assertEqual(len(calls), 2)
        self.assertEqual(ctx.exception.status, 429)

    def test_does_not_retry_when_disabled(self):
        calls = self._always_429()
        fc.HttpBudget.retry_on_429 = False
        fc.HttpBudget.retries = 1
        with self.assertRaises(fc.HttpError) as ctx:
            fc.http_json("https://example.test")
        self.assertEqual(len(calls), 1)
        self.assertEqual(ctx.exception.status, 429)

    def test_disabling_429_retry_does_not_affect_5xx_retries(self):
        calls = self._always_429()

        def fake_urlopen(req, timeout=None):
            calls.append(1)
            raise fc.urllib.error.HTTPError(req.full_url, 503, "unavailable", {}, None)

        fc.urllib.request.urlopen = fake_urlopen
        fc.HttpBudget.retry_on_429 = False
        fc.HttpBudget.retries = 1
        with self.assertRaises(fc.HttpError):
            fc.http_json("https://example.test")
        self.assertEqual(len(calls), 2, "a 503 should still retry once regardless of retry_on_429")

    def test_web_entrypoint_disables_retry_on_429_by_default(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "analyze_retry_check", str(Path(__file__).resolve().parents[1] / "api" / "analyze.py")
        )
        mod = importlib.util.module_from_spec(spec)
        os.environ.pop("FIRSTCALLOOR_RETRY_ON_429", None)
        spec.loader.exec_module(mod)
        self.assertFalse(fc.HttpBudget.retry_on_429)


# --------------------------------------------------------------------------
# X search strategy, stubbed at the HTTP layer
# --------------------------------------------------------------------------

class FakeX:
    """Serves a fixed corpus of tweets honouring start_time/end_time and paging."""

    def __init__(self, tweets, *, page_size=100, fail_after=None):
        self.tweets = tweets           # list of (id, offset_seconds, author)
        self.page_size = page_size
        self.fail_after = fail_after   # raise 429 after N requests
        self.calls = 0
        self.windows = []

    def __call__(self, url, **kwargs):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise fc.HttpError(429, "rate limited", url)

        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        start = fc.parse_x_time(q["start_time"][0])
        end = fc.parse_x_time(q["end_time"][0])
        self.windows.append((start, end))

        origin = ORIGIN
        inside = [
            t for t in self.tweets
            if start <= origin + timedelta(seconds=t[1]) < end
        ]
        inside.sort(key=lambda t: -t[1])          # recency order, like X
        offset = int(q.get("next_token", ["0"])[0])
        limit = min(self.page_size, int(q["max_results"][0]))
        page = inside[offset:offset + limit]
        next_offset = offset + len(page)

        data, users = [], {}
        for tid, secs, author in page:
            data.append({
                "id": tid,
                "author_id": author,
                "created_at": fc.iso(origin + timedelta(seconds=secs)),
                "text": f"mention {tid}",
            })
            users[author] = {
                "id": author,
                "username": author,
                "name": author,
                "created_at": fc.iso(origin - timedelta(days=900)),
                "public_metrics": {"followers_count": 500},
            }

        meta = {"result_count": len(page)}
        if next_offset < len(inside):
            meta["next_token"] = str(next_offset)
        return {"data": data, "includes": {"users": list(users.values())}, "meta": meta}


ORIGIN = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(microsecond=0)


class TestSearchStrategy(unittest.TestCase):
    def setUp(self):
        self._real = fc.http_json
        self._real_sleep = fc.time.sleep
        fc.time.sleep = lambda *_a, **_k: None  # inter-request pacing, not timing, under test

    def tearDown(self):
        fc.http_json = self._real
        fc.time.sleep = self._real_sleep

    def _client(self, fake, **kw):
        fc.http_json = fake
        return fc.XSearchClient("token", **kw)

    def test_finds_earliest_mention_after_expanding_window(self):
        # Nothing in the first hour; mentions only at 70 and 95 minutes.
        fake = FakeX([("t1", 70 * 60, "a"), ("t2", 95 * 60, "b")])
        out = self._client(fake).search(VALID_CA, ORIGIN)
        self.assertEqual(out.status, "ok")
        self.assertTrue(out.complete)
        earliest = min(out.mentions, key=lambda m: m.created_unix)
        self.assertEqual(earliest.tweet_id, "t1")
        self.assertEqual(earliest.seconds_since_creation, 70 * 60)

    def test_window_is_anchored_at_launch_not_now(self):
        fake = FakeX([("t1", 120, "a")])
        self._client(fake).search(VALID_CA, ORIGIN)
        self.assertEqual(fake.windows[0][0], ORIGIN)

    def test_contracts_window_when_cap_is_hit(self):
        # 400 tweets spread over 6h, cap 50: a naive newest-first sweep would
        # return only late mentions. Contraction must pull us back to launch.
        corpus = [(f"t{i}", 60 + i * 50, f"a{i}") for i in range(400)]
        fake = FakeX(corpus)
        out = self._client(fake, cap=50).search(VALID_CA, ORIGIN)
        earliest = min(out.mentions, key=lambda m: m.created_unix)
        self.assertEqual(earliest.tweet_id, "t0")
        # Later windows must be strictly tighter than the first.
        self.assertLess(fake.windows[-1][1], fake.windows[0][1])

    def test_rate_limit_midway_returns_partial_with_note(self):
        corpus = [(f"t{i}", 60 + i * 10, f"a{i}") for i in range(500)]
        fake = FakeX(corpus, fail_after=1)
        out = self._client(fake, cap=300).search(VALID_CA, ORIGIN)
        self.assertEqual(out.status, "partial")
        self.assertFalse(out.complete)
        self.assertTrue(any("cut short" in n for n in out.notes))
        self.assertTrue(out.mentions, "partial results should still be returned")

    def test_recent_search_clamps_old_token_and_warns(self):
        old_origin = datetime.now(timezone.utc) - timedelta(days=30)
        fake = FakeX([])
        out = self._client(fake).search(VALID_CA, old_origin)
        self.assertTrue(any("7 days" in n for n in out.notes))
        self.assertGreater(fc.parse_x_time(out.window_start_iso), old_origin)

    def test_full_archive_does_not_clamp(self):
        old_origin = datetime.now(timezone.utc) - timedelta(days=30)
        fake = FakeX([])
        out = self._client(fake, full_archive=True).search(VALID_CA, old_origin)
        self.assertFalse(any("7 days" in n for n in out.notes))
        self.assertIn("search/all", out.endpoint)

    def test_zero_mentions_is_not_an_error(self):
        out = self._client(FakeX([])).search(VALID_CA, ORIGIN)
        self.assertEqual(out.mentions, [])
        self.assertEqual(out.status, "ok")

    def test_pre_launch_probe_finds_recycled_ca_mentions(self):
        fake = FakeX([("old1", -1800, "ghost"), ("t1", 60, "a")])
        client = self._client(fake)
        pre, _ = client.probe_before_launch(VALID_CA, ORIGIN, hours=6)
        self.assertEqual([m.tweet_id for m in pre], ["old1"])
        self.assertLess(pre[0].seconds_since_creation, 0)

    def test_retweet_detection_from_referenced_tweets(self):
        client = fc.XSearchClient("token")
        built = client._build_mentions(
            [{
                "id": "1", "author_id": "a", "created_at": fc.iso(ORIGIN),
                "text": "RT ...",
                "referenced_tweets": [{"type": "retweeted", "id": "9"}],
            }],
            {"a": {"username": "a", "name": "A",
                   "created_at": fc.iso(ORIGIN - timedelta(hours=3)),
                   "public_metrics": {"followers_count": 7}}},
            ORIGIN,
        )
        self.assertTrue(built[0].is_retweet)
        self.assertLess(built[0].account_age_hours, fc.NEW_ACCOUNT_BOT_WINDOW_H)


# --------------------------------------------------------------------------
# on-chain origin, stubbed RPC
# --------------------------------------------------------------------------

class FakeRPC(fc.SolanaRPC):
    def __init__(self, pages, block_time=1789842575, dev_wallet="DevWa11etFakeAddress11111111111111111111"):
        super().__init__("stub://rpc")
        self.pages = pages
        self._bt = block_time
        self._dev_wallet = dev_wallet
        self.requests = []

    def call(self, method, params):
        self.requests.append(method)
        if method == "getSignaturesForAddress":
            return self.pages.pop(0) if self.pages else []
        if method == "getTransaction":
            if self._bt is None:
                return {}
            keys = [self._dev_wallet, "So11111111111111111111111111111111111111112"]
            return {
                "blockTime": self._bt,
                "transaction": {"message": {"accountKeys": keys}},
                "meta": {"preBalances": [10_000_000_000, 0], "postBalances": [9_000_000_000, 0],
                         "preTokenBalances": [], "postTokenBalances": []},
            }
        return None


def sigs(n, start_id=0):
    return [{"signature": f"sig{start_id + i}", "blockTime": 1789842575 + i} for i in range(n)]


class TestClampedWindowUsesTrueOrigin(unittest.TestCase):
    """Regression test: when the recent-search lookback wall clamps the
    search window forward (old token, X Basic), a mention's
    seconds_since_creation must still be measured against the token's TRUE
    launch time, never against the clamped window start. The two are equal
    for a fresh token, which is why this needs its own old-token test - it's
    exactly the case the window-expansion refactor could silently break.
    """

    def setUp(self):
        self._real = fc.http_json
        self._real_sleep = fc.time.sleep
        fc.time.sleep = lambda *_a, **_k: None  # inter-request pacing, not timing, under test

    def tearDown(self):
        fc.http_json = self._real
        fc.time.sleep = self._real_sleep

    def test_clamped_window_still_measures_from_true_origin(self):
        true_origin = (datetime.now(timezone.utc) - timedelta(days=30)).replace(microsecond=0)
        # Somewhere inside the clamped (last-7-days) window, far from true_origin.
        tweet_absolute_time = (datetime.now(timezone.utc) - timedelta(days=3)).replace(microsecond=0)
        expected_seconds_since_launch = int((tweet_absolute_time - true_origin).total_seconds())

        def fake(url, **kwargs):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            start = fc.parse_x_time(q["start_time"][0])
            end = fc.parse_x_time(q["end_time"][0])
            if not (start <= tweet_absolute_time < end):
                return {"data": [], "includes": {"users": []}, "meta": {}}
            return {
                "data": [{
                    "id": "t1", "author_id": "a",
                    "created_at": fc.iso(tweet_absolute_time), "text": "old news",
                }],
                "includes": {"users": [{
                    "id": "a", "username": "caller", "name": "Caller",
                    "created_at": fc.iso(tweet_absolute_time - timedelta(days=900)),
                    "public_metrics": {"followers_count": 100},
                }]},
                "meta": {},
            }

        fc.http_json = fake
        client = fc.XSearchClient("token")  # full_archive=False -> 7-day wall applies
        out = client.search(VALID_CA, true_origin)

        self.assertEqual(len(out.mentions), 1)
        # Must equal (tweet time - TRUE origin), never (tweet time - clamped
        # window start, which sits ~23 days later than true_origin here).
        self.assertEqual(out.mentions[0].seconds_since_creation, expected_seconds_since_launch)


# --------------------------------------------------------------------------
# twitterapi.io search strategy, stubbed at the HTTP layer
#
# NOTE: api.twitterapi.io is unreachable from the sandbox this was built in
# (same network policy that blocks api.x.com and pump.fun), so this client
# has never made a live call. The response shape below is built from
# twitterapi.io's documented/expected format - a "tweets" list with
# has_next_page/next_cursor pagination, classic Twitter-style timestamps
# ("Wed Sep 19 18:30:16 +0000 2026" rather than ISO 8601), and a nested
# author object. If the real API's field names differ even slightly, only
# TwitterAPIIOClient._build_mentions and _parse_twitterapi_io_time need to
# change - the earliest-first strategy itself is inherited, already covered,
# and provider-agnostic.
# --------------------------------------------------------------------------

def _classic_twitter_time(dt: datetime) -> str:
    return dt.strftime("%a %b %d %H:%M:%S +0000 %Y")


class FakeTwitterAPIIO:
    """Serves a fixed corpus of tweets honouring since_time:/until_time: query
    operators and cursor pagination, in twitterapi.io's documented shape."""

    def __init__(self, tweets, *, page_size=100, fail_after=None):
        self.tweets = tweets           # list of (id, offset_seconds, author)
        self.page_size = page_size
        self.fail_after = fail_after
        self.calls = 0
        self.windows = []

    def __call__(self, url, **kwargs):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise fc.HttpError(429, "rate limited", url)

        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        query_str = q["query"][0]
        since = int(re.search(r"since_time:(\d+)", query_str).group(1))
        until = int(re.search(r"until_time:(\d+)", query_str).group(1))
        start = datetime.fromtimestamp(since, tz=timezone.utc)
        end = datetime.fromtimestamp(until, tz=timezone.utc)
        self.windows.append((start, end))

        origin = ORIGIN
        inside = [
            t for t in self.tweets
            if start <= origin + timedelta(seconds=t[1]) < end
        ]
        inside.sort(key=lambda t: -t[1])
        offset = int(q.get("cursor", ["0"])[0])
        page = inside[offset:offset + self.page_size]
        next_offset = offset + len(page)

        tweets_out = []
        for tid, secs, author in page:
            tweets_out.append({
                "id": tid,
                "text": f"mention {tid}",
                "createdAt": _classic_twitter_time(origin + timedelta(seconds=secs)),
                "url": f"https://x.com/{author}/status/{tid}",
                "author": {
                    "userName": author,
                    "name": author,
                    "followers": 500,
                    "createdAt": _classic_twitter_time(origin - timedelta(days=900)),
                },
            })

        has_more = next_offset < len(inside)
        return {
            "tweets": tweets_out,
            "has_next_page": has_more,
            "next_cursor": str(next_offset) if has_more else "",
        }


class TestTwitterAPIIOStrategy(unittest.TestCase):
    def setUp(self):
        self._real = fc.http_json
        self._real_sleep = fc.time.sleep
        fc.time.sleep = lambda *_a, **_k: None  # inter-request pacing, not timing, under test

    def tearDown(self):
        fc.http_json = self._real
        fc.time.sleep = self._real_sleep

    def _client(self, fake, **kw):
        fc.http_json = fake
        return fc.TwitterAPIIOClient("key", **kw)

    def test_finds_earliest_mention_after_expanding_window(self):
        fake = FakeTwitterAPIIO([("t1", 70 * 60, "a"), ("t2", 95 * 60, "b")])
        out = self._client(fake).search(VALID_CA, ORIGIN)
        self.assertEqual(out.status, "ok")
        self.assertTrue(out.complete)
        earliest = min(out.mentions, key=lambda m: m.created_unix)
        self.assertEqual(earliest.tweet_id, "t1")
        self.assertEqual(earliest.seconds_since_creation, 70 * 60)

    def test_contracts_window_when_cap_is_hit(self):
        corpus = [(f"t{i}", 60 + i * 50, f"a{i}") for i in range(400)]
        fake = FakeTwitterAPIIO(corpus)
        out = self._client(fake, cap=50).search(VALID_CA, ORIGIN)
        earliest = min(out.mentions, key=lambda m: m.created_unix)
        self.assertEqual(earliest.tweet_id, "t0")
        self.assertLess(fake.windows[-1][1], fake.windows[0][1])

    def test_rate_limit_midway_returns_partial_with_note(self):
        corpus = [(f"t{i}", 60 + i * 10, f"a{i}") for i in range(500)]
        fake = FakeTwitterAPIIO(corpus, fail_after=1)
        out = self._client(fake, cap=300).search(VALID_CA, ORIGIN)
        self.assertEqual(out.status, "partial")
        self.assertFalse(out.complete)
        self.assertTrue(any("cut short" in n for n in out.notes))
        self.assertTrue(out.mentions)

    def test_no_lookback_wall_on_a_very_old_token(self):
        # The whole point of this provider: unlike X Basic, a token launched
        # months ago is not clamped to a 7-day search window.
        old_origin = datetime.now(timezone.utc) - timedelta(days=200)
        fake = FakeTwitterAPIIO([])
        out = self._client(fake).search(VALID_CA, old_origin)
        self.assertFalse(any("7 day" in n or "lookback" in n for n in out.notes))
        self.assertEqual(fc.parse_x_time(fc.iso(old_origin)),
                          fc.parse_x_time(out.window_start_iso))

    def test_zero_mentions_is_not_an_error(self):
        out = self._client(FakeTwitterAPIIO([])).search(VALID_CA, ORIGIN)
        self.assertEqual(out.mentions, [])
        self.assertEqual(out.status, "ok")
        self.assertEqual(out.provider, "twitterapi.io")

    def test_pre_launch_probe_finds_recycled_ca_mentions(self):
        fake = FakeTwitterAPIIO([("old1", -1800, "ghost"), ("t1", 60, "a")])
        client = self._client(fake)
        pre, _ = client.probe_before_launch(VALID_CA, ORIGIN, hours=6)
        self.assertEqual([m.tweet_id for m in pre], ["old1"])
        self.assertLess(pre[0].seconds_since_creation, 0)

    def test_since_until_time_operators_bracket_the_window(self):
        fake = FakeTwitterAPIIO([("t1", 120, "a")])
        self._client(fake).search(VALID_CA, ORIGIN)
        self.assertEqual(fake.windows[0][0], ORIGIN)

    def test_paces_between_requests_for_a_quiet_token(self):
        # A token with zero mentions is the expensive case: every expansion
        # width comes back empty, firing one request per width with no
        # pacing would risk tripping a provider's burst rate limit well
        # before its real quota is exhausted - which is exactly what
        # happened in production. Assert the pacing actually fires between
        # requests, not just that it's defined somewhere.
        sleeps = []
        fc.time.sleep = lambda s: sleeps.append(s)
        fake = FakeTwitterAPIIO([])  # no mentions at all -> every width is tried
        self._client(fake).search(VALID_CA, ORIGIN)
        self.assertEqual(fake.calls, len(fc.EXPAND_WINDOWS_H))
        self.assertEqual(len(sleeps), len(fc.EXPAND_WINDOWS_H) - 1)
        self.assertTrue(all(s == fc.INTER_REQUEST_PACING_S for s in sleeps))

    def test_no_pacing_sleep_before_the_first_request(self):
        sleeps = []
        fc.time.sleep = lambda s: sleeps.append(s)
        fake = FakeTwitterAPIIO([("t1", 60, "a")])  # found immediately, one call only
        self._client(fake).search(VALID_CA, ORIGIN)
        self.assertEqual(fake.calls, 1)
        self.assertEqual(sleeps, [])


class TestTwitterAPIIOParsing(unittest.TestCase):
    def test_classic_twitter_date_format(self):
        dt = fc._parse_twitterapi_io_time("Wed Sep 19 18:30:16 +0000 2026")
        self.assertEqual(dt, datetime(2026, 9, 19, 18, 30, 16, tzinfo=timezone.utc))

    def test_iso_fallback(self):
        dt = fc._parse_twitterapi_io_time("2026-09-19T18:30:16.000Z")
        self.assertEqual(dt, datetime(2026, 9, 19, 18, 30, 16, tzinfo=timezone.utc))

    def test_unparseable_timestamp_raises_actionable_error(self):
        with self.assertRaises(fc.FirstCallooorError) as ctx:
            fc._parse_twitterapi_io_time("not-a-timestamp")
        self.assertIn("not-a-timestamp", str(ctx.exception))

    def test_pick_prefers_first_present_key(self):
        self.assertEqual(fc._pick({"a": 1, "b": 2}, "z", "a", "b"), 1)
        self.assertEqual(fc._pick({"b": 2}, "a", "b"), 2)
        self.assertIsNone(fc._pick({}, "a", "b"))
        self.assertEqual(fc._pick({"a": None, "b": 3}, "a", "b"), 3)

    def test_build_mentions_tolerates_missing_optional_fields(self):
        client = fc.TwitterAPIIOClient("key")
        built = client._build_mentions(
            [{"id": "1", "createdAt": _classic_twitter_time(ORIGIN), "text": "x",
              "author": {"userName": "solo"}}],
            ORIGIN,
        )
        self.assertEqual(len(built), 1)
        self.assertEqual(built[0].handle, "solo")
        self.assertIsNone(built[0].followers)
        self.assertIsNone(built[0].account_age_hours)

    def test_build_mentions_skips_tweet_with_no_timestamp(self):
        client = fc.TwitterAPIIOClient("key")
        built = client._build_mentions(
            [{"id": "1", "text": "no createdAt at all", "author": {"userName": "x"}}],
            ORIGIN,
        )
        self.assertEqual(built, [])


class TestProviderSelection(unittest.TestCase):
    def setUp(self):
        self._env_backup = {
            k: os.environ.pop(k, None)
            for k in ("X_BEARER_TOKEN", "TWITTER_BEARER_TOKEN", "TWITTERAPI_IO_KEY", "TWITTERAPI_KEY")
        }

    def tearDown(self):
        for k, v in self._env_backup.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

    @staticmethod
    def _args(provider="auto", bearer_token=None, twitterapi_key=None):
        return argparse.Namespace(
            provider=provider, bearer_token=bearer_token, twitterapi_key=twitterapi_key
        )

    def test_auto_prefers_twitterapi_io_when_both_configured(self):
        os.environ["X_BEARER_TOKEN"] = "xtok"
        os.environ["TWITTERAPI_IO_KEY"] = "tatok"
        provider, cred = fc.select_provider(self._args())
        self.assertEqual((provider, cred), ("twitterapi.io", "tatok"))

    def test_auto_falls_back_to_x(self):
        os.environ["X_BEARER_TOKEN"] = "xtok"
        provider, cred = fc.select_provider(self._args())
        self.assertEqual((provider, cred), ("x", "xtok"))

    def test_auto_none_when_nothing_configured(self):
        self.assertEqual(fc.select_provider(self._args()), ("none", None))

    def test_explicit_provider_x_ignores_twitterapi_key(self):
        os.environ["TWITTERAPI_IO_KEY"] = "tatok"
        os.environ["X_BEARER_TOKEN"] = "xtok"
        provider, cred = fc.select_provider(self._args(provider="x"))
        self.assertEqual((provider, cred), ("x", "xtok"))

    def test_explicit_provider_without_its_credential_is_none(self):
        os.environ["X_BEARER_TOKEN"] = "xtok"
        provider, cred = fc.select_provider(self._args(provider="twitterapi"))
        self.assertEqual((provider, cred), ("none", None))

    def test_cli_flag_overrides_environment(self):
        os.environ["TWITTERAPI_IO_KEY"] = "env-key"
        provider, cred = fc.select_provider(self._args(twitterapi_key="cli-key"))
        self.assertEqual((provider, cred), ("twitterapi.io", "cli-key"))


class TestEnvValueCleaning(unittest.TestCase):
    """Regression coverage for a real production incident: a hosting UI's
    environment-variable form got filled in with the whole "NAME = value"
    line instead of just the value, and the resulting urllib error
    ("unknown url type: solana_rpc_url = https") was far too cryptic to
    self-diagnose from.
    """

    def test_clean_env_value_strips_name_equals_prefix(self):
        self.assertEqual(
            fc.clean_env_value("SOLANA_RPC_URL = https://x.test/y", "SOLANA_RPC_URL"),
            "https://x.test/y",
        )

    def test_clean_env_value_is_case_insensitive_on_name(self):
        self.assertEqual(
            fc.clean_env_value("solana_rpc_url=https://x.test", "SOLANA_RPC_URL"),
            "https://x.test",
        )

    def test_clean_env_value_strips_wrapping_quotes(self):
        self.assertEqual(fc.clean_env_value('"https://x.test"', "X"), "https://x.test")
        self.assertEqual(fc.clean_env_value("'https://x.test'", "X"), "https://x.test")

    def test_clean_env_value_strips_bare_whitespace(self):
        self.assertEqual(fc.clean_env_value("  https://x.test  ", "X"), "https://x.test")

    def test_clean_env_value_leaves_a_clean_value_untouched(self):
        self.assertEqual(fc.clean_env_value("https://x.test", "X"), "https://x.test")

    def test_clean_env_value_handles_missing_input(self):
        self.assertEqual(fc.clean_env_value(None, "X"), "")
        self.assertEqual(fc.clean_env_value("", "X"), "")

    def test_solana_rpc_self_heals_the_reported_incident_value(self):
        # The exact shape of value that produced "unknown url type:
        # solana_rpc_url = https" in production.
        rpc = fc.SolanaRPC("SOLANA_RPC_URL = https://mainnet.helius-rpc.com/?api-key=abc123")
        self.assertEqual(rpc.endpoint, "https://mainnet.helius-rpc.com/?api-key=abc123")

    def test_solana_rpc_rejects_a_genuinely_non_url_value_clearly(self):
        with self.assertRaises(fc.FirstCallooorError) as ctx:
            fc.SolanaRPC("not a url at all")
        self.assertIn("doesn't look like a URL", str(ctx.exception))

    def test_solana_rpc_error_message_masks_api_key(self):
        with self.assertRaises(fc.FirstCallooorError) as ctx:
            fc.SolanaRPC("RPC = also not a url, key=SUPERSECRET123")
        self.assertNotIn("SUPERSECRET123", str(ctx.exception))

    def test_mask_secret_redacts_common_credential_params(self):
        masked = fc.mask_secret("https://x.test/?api-key=ABC&api_token=DEF&ok=1")
        self.assertNotIn("ABC", masked)
        self.assertNotIn("DEF", masked)
        self.assertIn("ok=1", masked)

    def test_select_provider_cleans_bearer_token_from_env(self):
        args = argparse.Namespace(provider="auto", bearer_token=None, twitterapi_key=None)
        os.environ["X_BEARER_TOKEN"] = "X_BEARER_TOKEN = AAAA1234"
        try:
            provider, cred = fc.select_provider(args)
        finally:
            del os.environ["X_BEARER_TOKEN"]
        self.assertEqual((provider, cred), ("x", "AAAA1234"))

    def test_select_provider_cleans_twitterapi_key_from_env(self):
        args = argparse.Namespace(provider="auto", bearer_token=None, twitterapi_key=None)
        os.environ["TWITTERAPI_IO_KEY"] = '"my-real-key"'
        try:
            provider, cred = fc.select_provider(args)
        finally:
            del os.environ["TWITTERAPI_IO_KEY"]
        self.assertEqual((provider, cred), ("twitterapi.io", "my-real-key"))


class TestOrigin(unittest.TestCase):
    def test_walks_to_genesis_and_takes_oldest(self):
        rpc = FakeRPC([sigs(fc.SIG_PAGE_SIZE), sigs(5, start_id=1000)])
        origin = fc.resolve_origin(rpc, VALID_CA, page_cap=10)
        self.assertEqual(origin.genesis_signature, "sig1004")
        self.assertTrue(origin.exhausted)
        self.assertEqual(origin.signatures_scanned, fc.SIG_PAGE_SIZE + 5)
        self.assertEqual(origin.warnings, [])

    def test_page_cap_flags_uncertain_genesis(self):
        rpc = FakeRPC([sigs(fc.SIG_PAGE_SIZE) for _ in range(3)])
        origin = fc.resolve_origin(rpc, VALID_CA, page_cap=2)
        self.assertFalse(origin.exhausted)
        self.assertTrue(any("capped" in w.lower() for w in origin.warnings))

    def test_no_transactions_is_a_clear_error(self):
        with self.assertRaises(fc.FirstCallooorError) as ctx:
            fc.resolve_origin(FakeRPC([[]]), VALID_CA, page_cap=5)
        self.assertIn("No transactions found", str(ctx.exception))

    def test_falls_back_to_listing_blocktime(self):
        rpc = FakeRPC([sigs(3)], block_time=None)
        origin = fc.resolve_origin(rpc, VALID_CA, page_cap=5)
        self.assertEqual(origin.blocktime_source, "getSignaturesForAddress")
        self.assertEqual(origin.created_unix, 1789842577)
        self.assertTrue(origin.warnings)


class TestCrossCheck(unittest.TestCase):
    def setUp(self):
        self._real = fc.http_json

    def tearDown(self):
        fc.http_json = self._real

    def _stub(self, payload):
        fc.http_json = lambda url, **kw: payload

    def test_agreement_within_tolerance(self):
        self._stub({"created_timestamp": 1789842575000, "name": "N", "symbol": "S"})
        cc = fc.cross_check_pumpfun(VALID_CA, 1789842575)
        self.assertEqual(cc.status, "agree")
        self.assertEqual(cc.delta_seconds, 0)

    def test_disagreement_beyond_60s_is_flagged(self):
        self._stub({"created_timestamp": 1789842575000 + 120_000})
        cc = fc.cross_check_pumpfun(VALID_CA, 1789842575)
        self.assertEqual(cc.status, "disagree")
        self.assertEqual(cc.delta_seconds, 120)

    def test_unreachable_api_is_not_fatal(self):
        def boom(url, **kw):
            raise fc.HttpError(403, "cloudflare", url)
        fc.http_json = boom
        self.assertEqual(fc.cross_check_pumpfun(VALID_CA, 1789842575).status, "unavailable")


# --------------------------------------------------------------------------
# on-chain extras: first buyers, dev fingerprinting, rug signal, migration
#
# A flexible scripted RPC stub, since these functions each need much richer
# per-call responses (full transactions with account keys and balances) than
# the simple FakeRPC above provides.
# --------------------------------------------------------------------------

class ScriptedRPC(fc.SolanaRPC):
    def __init__(self, responses: dict):
        super().__init__("stub://rpc")
        self.responses = responses   # method -> value, or method -> callable(params)
        self.calls: list[tuple] = []

    def call(self, method, params):
        self.calls.append((method, params))
        handler = self.responses.get(method)
        if handler is None:
            return None
        return handler(params) if callable(handler) else handler


def make_tx(fee_payer, mint, *, pre_amt=None, post_amt=None,
            pre_sol=5_000_000_000, post_sol=4_000_000_000, block_time=1000,
            other_keys=("OtherKey1",), extra_program_keys=()):
    balances_pre = []
    balances_post = []
    if pre_amt is not None:
        balances_pre.append({"accountIndex": 0, "owner": fee_payer, "mint": mint,
                              "uiTokenAmount": {"uiAmount": pre_amt}})
    if post_amt is not None:
        balances_post.append({"accountIndex": 0, "owner": fee_payer, "mint": mint,
                               "uiTokenAmount": {"uiAmount": post_amt}})
    return {
        "blockTime": block_time,
        "transaction": {"message": {"accountKeys": [fee_payer, *other_keys, *extra_program_keys]}},
        "meta": {
            "preBalances": [pre_sol, 0],
            "postBalances": [post_sol, 0],
            "preTokenBalances": balances_pre,
            "postTokenBalances": balances_post,
        },
    }


def make_origin(dev_wallet="DevWallet1", mint=VALID_CA, created_unix=1000,
                 exhausted=True, post_genesis_batch=None):
    return fc.OnChainOrigin(
        mint=mint, created_unix=created_unix, created_iso=fc.iso(fc.utc_from_unix(created_unix)),
        genesis_signature="genesis_sig", signatures_scanned=len(post_genesis_batch or []),
        pages_scanned=1, exhausted=exhausted, blocktime_source="getTransaction",
        dev_wallet=dev_wallet, post_genesis_batch=post_genesis_batch or [],
    )


class TestFindFirstBuyers(unittest.TestCase):
    def test_detects_a_buy(self):
        batch = [
            {"signature": "buy1", "blockTime": 1010},
            {"signature": "genesis_sig", "blockTime": 1000},
        ]
        origin = make_origin(post_genesis_batch=batch)
        txs = {"buy1": make_tx("Buyer1", VALID_CA, pre_amt=None, post_amt=500.0, block_time=1010)}
        rpc = ScriptedRPC({"getTransaction": lambda p: txs.get(p[0])})

        buyers, notes, skipped = fc.find_first_buyers(rpc, origin, limit=10, scan_cap=10)
        self.assertEqual(len(buyers), 1)
        self.assertEqual(buyers[0].wallet, "Buyer1")
        self.assertEqual(buyers[0].tokens_received, 500.0)
        self.assertEqual(buyers[0].seconds_since_creation, 10)

    def test_skips_the_dev_wallets_own_transactions(self):
        batch = [
            {"signature": "dev_followup", "blockTime": 1005},
            {"signature": "genesis_sig", "blockTime": 1000},
        ]
        origin = make_origin(dev_wallet="DevWallet1", post_genesis_batch=batch)
        txs = {"dev_followup": make_tx("DevWallet1", VALID_CA, pre_amt=None, post_amt=999.0)}
        rpc = ScriptedRPC({"getTransaction": lambda p: txs.get(p[0])})

        buyers, _, _ = fc.find_first_buyers(rpc, origin)
        self.assertEqual(buyers, [])

    def test_skips_non_buy_transactions(self):
        batch = [
            {"signature": "a_sell", "blockTime": 1005},
            {"signature": "genesis_sig", "blockTime": 1000},
        ]
        origin = make_origin(post_genesis_batch=batch)
        # post <= pre: a sell or an unrelated transaction, not a buy.
        txs = {"a_sell": make_tx("Seller1", VALID_CA, pre_amt=500.0, post_amt=200.0)}
        rpc = ScriptedRPC({"getTransaction": lambda p: txs.get(p[0])})

        buyers, notes, skipped = fc.find_first_buyers(rpc, origin)
        self.assertEqual(buyers, [])
        self.assertTrue(notes)
        self.assertFalse(skipped)

    def test_respects_limit(self):
        batch = [{"signature": f"buy{i}", "blockTime": 1000 + i} for i in range(5, 0, -1)]
        batch.append({"signature": "genesis_sig", "blockTime": 1000})
        origin = make_origin(post_genesis_batch=batch)
        txs = {f"buy{i}": make_tx(f"Buyer{i}", VALID_CA, pre_amt=None, post_amt=100.0)
               for i in range(1, 6)}
        rpc = ScriptedRPC({"getTransaction": lambda p: txs.get(p[0])})

        buyers, _, _ = fc.find_first_buyers(rpc, origin, limit=2, scan_cap=10)
        self.assertEqual(len(buyers), 2)

    def test_respects_scan_cap_even_with_more_candidates(self):
        batch = [{"signature": f"buy{i}", "blockTime": 1000 + i} for i in range(10, 0, -1)]
        batch.append({"signature": "genesis_sig", "blockTime": 1000})
        origin = make_origin(post_genesis_batch=batch)
        txs = {f"buy{i}": make_tx(f"Buyer{i}", VALID_CA, pre_amt=None, post_amt=100.0)
               for i in range(1, 11)}
        rpc = ScriptedRPC({"getTransaction": lambda p: txs.get(p[0])})

        buyers, _, _ = fc.find_first_buyers(rpc, origin, limit=100, scan_cap=3)
        self.assertEqual(len(buyers), 3)

    def test_dedupes_the_same_wallet(self):
        batch = [
            {"signature": "buy2", "blockTime": 1020},
            {"signature": "buy1", "blockTime": 1010},
            {"signature": "genesis_sig", "blockTime": 1000},
        ]
        origin = make_origin(post_genesis_batch=batch)
        txs = {
            "buy1": make_tx("SameWallet", VALID_CA, pre_amt=None, post_amt=100.0, block_time=1010),
            "buy2": make_tx("SameWallet", VALID_CA, pre_amt=100.0, post_amt=200.0, block_time=1020),
        }
        rpc = ScriptedRPC({"getTransaction": lambda p: txs.get(p[0])})

        buyers, _, _ = fc.find_first_buyers(rpc, origin)
        self.assertEqual(len(buyers), 1)

    def test_skipped_when_walk_did_not_reach_true_genesis(self):
        origin = make_origin(exhausted=False, post_genesis_batch=[{"signature": "x"}])
        buyers, notes, skipped = fc.find_first_buyers(ScriptedRPC({}), origin)
        self.assertEqual(buyers, [])
        self.assertTrue(any("genesis" in n.lower() for n in notes))
        self.assertTrue(skipped)

    def test_empty_batch_is_handled_without_crashing(self):
        origin = make_origin(exhausted=True, post_genesis_batch=[])
        buyers, notes, skipped = fc.find_first_buyers(ScriptedRPC({}), origin)
        self.assertEqual(buyers, [])

    def test_zero_buys_found_is_not_the_same_as_skipped(self):
        # Regression: a capped walk (skipped=True, zero buyers) was
        # presented identically to "checked and genuinely found none"
        # (skipped=False, zero buyers) in both the CLI and the website -
        # exactly the "search did not run" vs "no mentions found"
        # distinction this tool already draws elsewhere, missed here.
        batch = [
            {"signature": "not_a_buy", "blockTime": 1005},
            {"signature": "genesis_sig", "blockTime": 1000},
        ]
        origin_checked = make_origin(exhausted=True, post_genesis_batch=batch)
        txs = {"not_a_buy": make_tx("Someone", VALID_CA, pre_amt=500.0, post_amt=200.0)}
        rpc = ScriptedRPC({"getTransaction": lambda p: txs.get(p[0])})
        buyers, _, skipped = fc.find_first_buyers(rpc, origin_checked)
        self.assertEqual(buyers, [])
        self.assertFalse(skipped, "genuinely checked and found nothing is not a skip")

        origin_capped = make_origin(exhausted=False, post_genesis_batch=[{"signature": "x"}])
        buyers2, _, skipped2 = fc.find_first_buyers(ScriptedRPC({}), origin_capped)
        self.assertEqual(buyers2, [])
        self.assertTrue(skipped2, "a capped walk must report skipped, not zero-found")


class TestBuildReportFirstBuyersSkipped(unittest.TestCase):
    def _minimal_report_inputs(self):
        origin = make_origin(exhausted=False, post_genesis_batch=[{"signature": "x"}])
        cross = fc.CrossCheck(status="unavailable")
        outcome = fc.SearchOutcome(status="mock", provider="mock")
        triage = fc.triage_mentions([])
        args = fc.parse_args(["--mock", VALID_CA])
        return origin, cross, outcome, triage, args

    def test_skipped_flag_reaches_the_report(self):
        origin, cross, outcome, triage, args = self._minimal_report_inputs()
        report = fc.build_report(
            VALID_CA, origin, cross, outcome, triage, args,
            buyers=[], buyer_notes=["First-buyer detection skipped: ..."], buyers_skipped=True,
        )
        self.assertEqual(report["first_buyers"]["wallets"], [])
        self.assertTrue(report["first_buyers"]["skipped"])

    def test_not_skipped_when_genuinely_checked(self):
        origin, cross, outcome, triage, args = self._minimal_report_inputs()
        report = fc.build_report(
            VALID_CA, origin, cross, outcome, triage, args,
            buyers=[], buyer_notes=[], buyers_skipped=False,
        )
        self.assertFalse(report["first_buyers"]["skipped"])


class TestFindOtherLaunches(unittest.TestCase):
    def test_finds_a_pump_fun_created_mint(self):
        sigs_batch = [{"signature": "create1", "blockTime": 500}]
        tx = make_tx("Dev1", "OldMint", pre_amt=None, post_amt=None, block_time=500,
                     extra_program_keys=(fc.PUMP_FUN_PROGRAM_ID,))
        tx["meta"]["postTokenBalances"] = [
            {"accountIndex": 3, "owner": "Dev1", "mint": "NewMint111", "uiTokenAmount": {"uiAmount": 1.0}}
        ]
        rpc = ScriptedRPC({
            "getSignaturesForAddress": lambda p: sigs_batch,
            "getTransaction": lambda p: tx if p[0] == "create1" else None,
        })
        profile = fc.find_other_launches(rpc, "Dev1", exclude_mint="CurrentMint", signature_cap=40)
        self.assertEqual(len(profile.other_launches), 1)
        self.assertEqual(profile.other_launches[0].mint, "NewMint111")
        self.assertTrue(profile.is_serial_deployer)

    def test_ignores_transactions_without_the_pump_program(self):
        sigs_batch = [{"signature": "unrelated1", "blockTime": 500}]
        tx = make_tx("Dev1", "X", block_time=500)  # no PUMP_FUN_PROGRAM_ID in keys
        tx["meta"]["postTokenBalances"] = [
            {"accountIndex": 3, "owner": "Dev1", "mint": "SomeMint", "uiTokenAmount": {"uiAmount": 1.0}}
        ]
        rpc = ScriptedRPC({
            "getSignaturesForAddress": lambda p: sigs_batch,
            "getTransaction": lambda p: tx,
        })
        profile = fc.find_other_launches(rpc, "Dev1", exclude_mint="CurrentMint")
        self.assertEqual(profile.other_launches, [])

    def test_ignores_the_excluded_mint(self):
        sigs_batch = [{"signature": "create1", "blockTime": 500}]
        tx = make_tx("Dev1", "X", block_time=500, extra_program_keys=(fc.PUMP_FUN_PROGRAM_ID,))
        tx["meta"]["postTokenBalances"] = [
            {"accountIndex": 3, "owner": "Dev1", "mint": "CurrentMint", "uiTokenAmount": {"uiAmount": 1.0}}
        ]
        rpc = ScriptedRPC({
            "getSignaturesForAddress": lambda p: sigs_batch,
            "getTransaction": lambda p: tx,
        })
        profile = fc.find_other_launches(rpc, "Dev1", exclude_mint="CurrentMint")
        self.assertEqual(profile.other_launches, [])

    def test_ignores_an_existing_token_that_already_had_a_balance(self):
        sigs_batch = [{"signature": "buy1", "blockTime": 500}]
        tx = make_tx("Dev1", "X", block_time=500, extra_program_keys=(fc.PUMP_FUN_PROGRAM_ID,))
        # accountIndex 3 already had a pre-balance - not a fresh mint creation.
        tx["meta"]["preTokenBalances"] = [
            {"accountIndex": 3, "owner": "Dev1", "mint": "OldMint", "uiTokenAmount": {"uiAmount": 5.0}}
        ]
        tx["meta"]["postTokenBalances"] = [
            {"accountIndex": 3, "owner": "Dev1", "mint": "OldMint", "uiTokenAmount": {"uiAmount": 10.0}}
        ]
        rpc = ScriptedRPC({
            "getSignaturesForAddress": lambda p: sigs_batch,
            "getTransaction": lambda p: tx,
        })
        profile = fc.find_other_launches(rpc, "Dev1", exclude_mint="CurrentMint")
        self.assertEqual(profile.other_launches, [])

    def test_stops_early_once_max_launches_found(self):
        sigs_batch = [{"signature": f"create{i}", "blockTime": 500 + i} for i in range(10)]
        txs = {}
        for i in range(10):
            tx = make_tx("Dev1", "X", block_time=500 + i, extra_program_keys=(fc.PUMP_FUN_PROGRAM_ID,))
            tx["meta"]["postTokenBalances"] = [
                {"accountIndex": 3, "owner": "Dev1", "mint": f"Mint{i}", "uiTokenAmount": {"uiAmount": 1.0}}
            ]
            txs[f"create{i}"] = tx
        rpc = ScriptedRPC({
            "getSignaturesForAddress": lambda p: sigs_batch,
            "getTransaction": lambda p: txs.get(p[0]),
        })
        profile = fc.find_other_launches(rpc, "Dev1", exclude_mint="CurrentMint",
                                          signature_cap=10, max_launches=3)
        self.assertEqual(len(profile.other_launches), 3)
        self.assertFalse(profile.scan_exhausted)
        self.assertTrue(any("stopped after finding" in n.lower() for n in profile.notes))

    def test_no_dev_wallet_returns_empty_profile(self):
        profile = fc.find_other_launches(ScriptedRPC({}), None, exclude_mint="X")
        self.assertIsNone(profile.dev_wallet)
        self.assertEqual(profile.other_launches, [])
        self.assertTrue(profile.notes)

    def test_rpc_failure_degrades_gracefully(self):
        def boom(params):
            raise fc.FirstCallooorError("rate limited")
        rpc = ScriptedRPC({"getSignaturesForAddress": boom})
        profile = fc.find_other_launches(rpc, "Dev1", exclude_mint="X")
        self.assertEqual(profile.other_launches, [])
        self.assertTrue(profile.notes)


class TestAssessRisk(unittest.TestCase):
    def _mint_info_response(self, mint_authority, freeze_authority, supply="1000000000000000", decimals=6):
        return {"value": {"data": {"parsed": {"info": {
            "mintAuthority": mint_authority, "freezeAuthority": freeze_authority,
            "supply": supply, "decimals": decimals,
        }}}}}

    def test_mint_info_unavailable_is_unknown_verdict(self):
        rpc = ScriptedRPC({"getAccountInfo": lambda p: None})
        risk = fc.assess_risk(rpc, VALID_CA, None)
        self.assertEqual(risk.verdict, "unknown")

    def test_active_mint_authority_is_elevated_risk(self):
        rpc = ScriptedRPC({
            "getAccountInfo": lambda p: self._mint_info_response("SomeDevWallet", None),
            "getTokenLargestAccounts": lambda p: {"value": []},
        })
        risk = fc.assess_risk(rpc, VALID_CA, None)
        self.assertEqual(risk.verdict, "elevated_risk")
        self.assertFalse(risk.mint_authority_revoked)
        self.assertTrue(any("mint authority" in r.lower() for r in risk.reasons))

    def test_active_freeze_authority_is_elevated_risk(self):
        rpc = ScriptedRPC({
            "getAccountInfo": lambda p: self._mint_info_response(None, "SomeDevWallet"),
            "getTokenLargestAccounts": lambda p: {"value": []},
        })
        risk = fc.assess_risk(rpc, VALID_CA, None)
        self.assertEqual(risk.verdict, "elevated_risk")
        self.assertFalse(risk.freeze_authority_revoked)

    def test_revoked_authorities_and_low_concentration_is_clean(self):
        holders = [{"address": f"acc{i}", "uiAmount": 1000.0} for i in range(3)]
        rpc = ScriptedRPC({
            "getAccountInfo": lambda p: self._mint_info_response(None, None, supply="1000000000000000"),
            "getTokenLargestAccounts": lambda p: {"value": holders},
            "getMultipleAccounts": lambda p: {"value": [None] * len(holders)},
        })
        risk = fc.assess_risk(rpc, VALID_CA, None)
        self.assertEqual(risk.verdict, "no_major_red_flags")
        self.assertTrue(risk.mint_authority_revoked)
        self.assertTrue(risk.freeze_authority_revoked)

    def test_high_concentration_is_elevated_risk(self):
        # supply is 1_000_000 ui; one holder alone has 800_000 (80%).
        holders = [{"address": "whale", "uiAmount": 800_000.0}]
        rpc = ScriptedRPC({
            "getAccountInfo": lambda p: self._mint_info_response(None, None, supply="1000000000000", decimals=6),
            "getTokenLargestAccounts": lambda p: {"value": holders},
            "getMultipleAccounts": lambda p: {"value": [
                {"data": {"parsed": {"info": {"owner": "ChReo21irgNsRVi5TPvaXgnwxSNnsKJ7Lt1bpAiME8ci"}}}}]},
        })
        risk = fc.assess_risk(rpc, VALID_CA, None)
        self.assertEqual(risk.verdict, "elevated_risk")
        self.assertGreater(risk.top10_pct, 70)

    def test_moderate_concentration_is_some_risk_signals(self):
        holders = [{"address": "big", "uiAmount": 500_000.0}]
        rpc = ScriptedRPC({
            "getAccountInfo": lambda p: self._mint_info_response(None, None, supply="1000000000000", decimals=6),
            "getTokenLargestAccounts": lambda p: {"value": holders},
            "getMultipleAccounts": lambda p: {"value": [
                {"data": {"parsed": {"info": {"owner": "ChReo21irgNsRVi5TPvaXgnwxSNnsKJ7Lt1bpAiME8ci"}}}}]},
        })
        risk = fc.assess_risk(rpc, VALID_CA, None)
        self.assertEqual(risk.verdict, "some_risk_signals")

    def test_serial_deployer_bumps_verdict_even_with_clean_mint(self):
        dev_profile = fc.DevProfile(
            dev_wallet="Dev1",
            other_launches=[fc.OtherLaunch(mint=f"M{i}", signature=f"s{i}",
                                             created_unix=1, created_iso="x") for i in range(3)],
        )
        rpc = ScriptedRPC({
            "getAccountInfo": lambda p: self._mint_info_response(None, None, supply="1000000000000000"),
            "getTokenLargestAccounts": lambda p: {"value": []},
        })
        risk = fc.assess_risk(rpc, VALID_CA, dev_profile)
        self.assertEqual(risk.verdict, "some_risk_signals")
        self.assertEqual(risk.dev_other_launches, 3)

    def test_holder_lookup_failure_notes_but_does_not_crash(self):
        def boom(params):
            raise fc.FirstCallooorError("rate limited")
        rpc = ScriptedRPC({
            "getAccountInfo": lambda p: self._mint_info_response(None, None, supply="1000000000000000"),
            "getTokenLargestAccounts": boom,
        })
        risk = fc.assess_risk(rpc, VALID_CA, None)
        self.assertIsNone(risk.top10_pct)
        self.assertTrue(any("concentration" in n.lower() for n in risk.notes))
        # authorities were both revoked and nothing else flags - still clean
        # despite not knowing concentration, since we distinguish "unknown"
        # from "checked and bad".
        self.assertEqual(risk.verdict, "no_major_red_flags")


class TestGetTopHolders(unittest.TestCase):
    def test_uses_one_batched_call_not_one_per_holder(self):
        holders = [{"address": f"acc{i}", "uiAmount": 10.0} for i in range(5)]
        rpc = ScriptedRPC({
            "getTokenLargestAccounts": lambda p: {"value": holders},
            "getMultipleAccounts": lambda p: {
                "value": [{"data": {"parsed": {"info": {"owner": f"owner{i}"}}}} for i in range(5)]
            },
        })
        result, note = fc.get_top_holders(rpc, VALID_CA, supply_ui=100.0)
        self.assertIsNone(note)
        self.assertEqual(len(result), 5)
        self.assertEqual(result[0].owner, "owner0")
        self.assertEqual(result[0].pct_of_supply, 10.0)
        methods_called = [c[0] for c in rpc.calls]
        self.assertEqual(methods_called.count("getMultipleAccounts"), 1)
        self.assertEqual(methods_called.count("getAccountInfo"), 0)

    def test_largest_accounts_failure_returns_note(self):
        def boom(params):
            raise fc.FirstCallooorError("rate limited")
        rpc = ScriptedRPC({"getTokenLargestAccounts": boom})
        result, note = fc.get_top_holders(rpc, VALID_CA, supply_ui=100.0)
        self.assertEqual(result, [])
        self.assertIsNotNone(note)

    def test_owner_resolution_failure_keeps_amounts_but_warns(self):
        # The amounts are still real, but with no owner the bonding curve
        # can't be told from a whale - so this must NOT quietly produce a
        # concentration number built on unclassified accounts.
        holders = [{"address": "acc1", "uiAmount": 42.0}]

        def boom(params):
            raise fc.FirstCallooorError("rate limited")

        rpc = ScriptedRPC({
            "getTokenLargestAccounts": lambda p: {"value": holders},
            "getMultipleAccounts": boom,
        })
        result, note = fc.get_top_holders(rpc, VALID_CA, supply_ui=100.0)
        self.assertIsNotNone(note)
        self.assertIn("told apart from real wallets", note)
        self.assertEqual(len(result), 1)
        self.assertIsNone(result[0].owner)
        self.assertEqual(result[0].amount_ui, 42.0)


class TestCheckMigration(unittest.TestCase):
    def test_detects_a_known_amm_owner(self):
        holders = [
            fc.HolderInfo(owner="SomeWallet", token_account="a", amount_ui=1.0, pct_of_supply=1.0),
            fc.HolderInfo(owner="675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
                          token_account="b", amount_ui=99.0, pct_of_supply=99.0),
        ]
        status = fc.check_migration(holders)
        self.assertTrue(status.migrated)
        self.assertEqual(status.venue, "Raydium AMM v4")

    def test_no_known_amm_owner_is_not_migrated(self):
        holders = [fc.HolderInfo(owner="RandomWallet", token_account="a", amount_ui=1.0, pct_of_supply=1.0)]
        status = fc.check_migration(holders)
        self.assertFalse(status.migrated)
        self.assertIsNone(status.venue)

    def test_empty_holders_is_not_migrated(self):
        status = fc.check_migration([])
        self.assertFalse(status.migrated)


class TestInsiderCorrelation(unittest.TestCase):
    def test_flags_mention_containing_a_buyer_wallet(self):
        m = mention("caller1", 10)
        m.text = f"just aped {m.text} wallet ChReo21irgNsRVi5TPvaXgnwxSNnsKJ7Lt1bpAiME8ci proof"
        fc.flag_insider_mentions([m], ["ChReo21irgNsRVi5TPvaXgnwxSNnsKJ7Lt1bpAiME8ci"])
        self.assertEqual(m.matched_buyer_wallet, "ChReo21irgNsRVi5TPvaXgnwxSNnsKJ7Lt1bpAiME8ci")
        self.assertIn("posted_own_buy_wallet", m.flags)

    def test_does_not_flag_unrelated_text(self):
        m = mention("caller1", 10)
        fc.flag_insider_mentions([m], ["SomeOtherWalletAddress11111111111111111111"])
        self.assertIsNone(m.matched_buyer_wallet)
        self.assertNotIn("posted_own_buy_wallet", m.flags)

    def test_handles_empty_wallet_list(self):
        m = mention("caller1", 10)
        fc.flag_insider_mentions([m], [])
        self.assertIsNone(m.matched_buyer_wallet)

    def test_handles_empty_mentions_list(self):
        fc.flag_insider_mentions([], ["SomeWallet"])  # must not raise


# --------------------------------------------------------------------------
# base58 encoding + program-derived addresses
# --------------------------------------------------------------------------

class TestBase58Encode(unittest.TestCase):
    def test_round_trips_a_real_address(self):
        self.assertEqual(fc.b58_encode(fc.b58_decode(VALID_CA)), VALID_CA)

    def test_round_trips_leading_zero_bytes(self):
        # Leading zero bytes encode as leading '1's; losing them would produce
        # a different, wrong address rather than an obvious error.
        raw = b"\x00\x00" + b"\x11" * 30
        self.assertTrue(fc.b58_encode(raw).startswith("11"))
        self.assertEqual(fc.b58_decode(fc.b58_encode(raw)), raw)

    def test_empty_input(self):
        self.assertEqual(fc.b58_encode(b""), "")


class TestFindProgramAddress(unittest.TestCase):
    def test_matches_known_metaplex_pda(self):
        # USDC's real metadata account: verified against mainnet, where this
        # address is owned by the Metaplex program and decodes to "USD Coin".
        # If the bump walk or the curve check drifts, this comes out different.
        usdc = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        pda = fc.find_program_address(
            [b"metadata", fc.b58_decode(fc.METAPLEX_METADATA_PROGRAM), fc.b58_decode(usdc)],
            fc.METAPLEX_METADATA_PROGRAM,
        )
        self.assertEqual(pda, "5x38Kp4hvdomTCnCrAny4UtMUt5rQBdB6px2K1Ui45Wq")

    def test_result_is_off_curve(self):
        pda = fc.find_program_address(
            [b"metadata", fc.b58_decode(fc.METAPLEX_METADATA_PROGRAM), fc.b58_decode(VALID_CA)],
            fc.METAPLEX_METADATA_PROGRAM,
        )
        self.assertFalse(fc._is_on_ed25519_curve(fc.b58_decode(pda)))

    def test_is_deterministic(self):
        seeds = [b"metadata", fc.b58_decode(fc.METAPLEX_METADATA_PROGRAM), fc.b58_decode(VALID_CA)]
        self.assertEqual(
            fc.find_program_address(seeds, fc.METAPLEX_METADATA_PROGRAM),
            fc.find_program_address(seeds, fc.METAPLEX_METADATA_PROGRAM),
        )


# --------------------------------------------------------------------------
# token identity - the name, straight from the chain
# --------------------------------------------------------------------------

def token2022_mint(name="Dons", symbol="DONS", uri="https://example.test/d.json"):
    return {
        "decimals": 6,
        "supply": "1000000000000000",
        "extensions": [
            {"extension": "transferFeeConfig", "state": {}},
            {"extension": "tokenMetadata",
             "state": {"name": name, "symbol": symbol, "uri": uri}},
        ],
    }


def metaplex_account(name="USD Coin", symbol="USDC", uri="https://example.test/u.json"):
    """A Metaplex metadata account, encoded the way the chain returns it."""
    def borsh_str(s):
        raw = s.encode("utf-8")
        return len(raw).to_bytes(4, "little") + raw
    blob = (b"\x04" + b"\x01" * 32 + b"\x02" * 32
            + borsh_str(name) + borsh_str(symbol) + borsh_str(uri))
    return {"value": {"data": [base64.b64encode(blob).decode(), "base64"]}}


class TestResolveTokenIdentity(unittest.TestCase):
    def test_reads_token_2022_embedded_metadata(self):
        rpc = ScriptedRPC({})
        identity = fc.resolve_token_identity(rpc, VALID_CA, token2022_mint())
        self.assertEqual(identity.name, "Dons")
        self.assertEqual(identity.symbol, "DONS")
        self.assertEqual(identity.source, "token-2022")
        # The name was already in the mint account the risk check fetched, so
        # resolving it must cost no extra RPC call at all.
        self.assertEqual(rpc.calls, [])

    def test_falls_back_to_metaplex_for_classic_spl(self):
        rpc = ScriptedRPC({"getAccountInfo": lambda p: metaplex_account()})
        identity = fc.resolve_token_identity(rpc, VALID_CA, {"decimals": 6, "supply": "1"})
        self.assertEqual(identity.name, "USD Coin")
        self.assertEqual(identity.symbol, "USDC")
        self.assertEqual(identity.source, "metaplex")

    def test_fetches_the_mint_itself_when_not_given_one(self):
        seen = []

        def account_info(params):
            seen.append(params[0])
            if params[0] == VALID_CA:
                return {"value": {"data": {"parsed": {"info": token2022_mint()}}}}
            return None

        identity = fc.resolve_token_identity(ScriptedRPC({"getAccountInfo": account_info}), VALID_CA)
        self.assertEqual(identity.source, "token-2022")
        self.assertEqual(seen, [VALID_CA])

    def test_unavailable_when_nothing_has_the_name(self):
        rpc = ScriptedRPC({"getAccountInfo": {"value": None}})
        identity = fc.resolve_token_identity(rpc, VALID_CA, {"decimals": 6})
        self.assertIsNone(identity.name)
        self.assertEqual(identity.source, "unavailable")

    def test_rpc_failure_degrades_instead_of_raising(self):
        def boom(params):
            raise fc.FirstCallooorError("RPC down")

        identity = fc.resolve_token_identity(ScriptedRPC({"getAccountInfo": boom}), VALID_CA)
        self.assertEqual(identity.source, "unavailable")

    def test_ignores_a_blank_embedded_name(self):
        # An extension present but empty must not beat the Metaplex fallback.
        mint = token2022_mint(name="  ", symbol="")
        rpc = ScriptedRPC({"getAccountInfo": lambda p: metaplex_account()})
        identity = fc.resolve_token_identity(rpc, VALID_CA, mint)
        self.assertEqual(identity.source, "metaplex")

    def test_truncated_metaplex_account_does_not_raise(self):
        rpc = ScriptedRPC({"getAccountInfo": {"value": {"data": [base64.b64encode(b"\x04" * 10).decode(), "base64"]}}})
        identity = fc.resolve_token_identity(rpc, VALID_CA, {"decimals": 6})
        self.assertEqual(identity.source, "unavailable")


# --------------------------------------------------------------------------
# dev holdings
# --------------------------------------------------------------------------

def token_accounts(*amounts):
    return {"value": [
        {"account": {"data": {"parsed": {"info": {"tokenAmount": {"uiAmount": a}}}}}}
        for a in amounts
    ]}


class TestGetWalletHolding(unittest.TestCase):
    def test_sums_every_token_account_for_the_wallet(self):
        rpc = ScriptedRPC({"getTokenAccountsByOwner": token_accounts(100.0, 50.5)})
        amount, pct, known = fc.get_wallet_holding(rpc, "DevWallet1", VALID_CA, 1000.0)
        self.assertEqual(amount, 150.5)
        self.assertEqual(pct, 15.05)
        self.assertTrue(known)

    def test_zero_balance_is_known_not_unknown(self):
        # "Dev sold everything" and "we could not check" must never collapse
        # into the same rendered claim.
        rpc = ScriptedRPC({"getTokenAccountsByOwner": {"value": []}})
        amount, pct, known = fc.get_wallet_holding(rpc, "DevWallet1", VALID_CA, 1000.0)
        self.assertEqual(amount, 0.0)
        self.assertEqual(pct, 0.0)
        self.assertTrue(known)

    def test_rpc_failure_is_unknown(self):
        def boom(params):
            raise fc.FirstCallooorError("rate limited")

        amount, pct, known = fc.get_wallet_holding(
            ScriptedRPC({"getTokenAccountsByOwner": boom}), "DevWallet1", VALID_CA, 1000.0)
        self.assertIsNone(amount)
        self.assertIsNone(pct)
        self.assertFalse(known)

    def test_no_wallet_is_unknown_and_costs_no_call(self):
        rpc = ScriptedRPC({})
        self.assertEqual(fc.get_wallet_holding(rpc, None, VALID_CA, 1000.0), (None, None, False))
        self.assertEqual(rpc.calls, [])

    def test_percentage_omitted_without_a_known_supply(self):
        rpc = ScriptedRPC({"getTokenAccountsByOwner": token_accounts(10.0)})
        amount, pct, known = fc.get_wallet_holding(rpc, "DevWallet1", VALID_CA, None)
        self.assertEqual(amount, 10.0)
        self.assertIsNone(pct)
        self.assertTrue(known)

    def test_queries_only_the_mint_in_question(self):
        rpc = ScriptedRPC({"getTokenAccountsByOwner": token_accounts(1.0)})
        fc.get_wallet_holding(rpc, "DevWallet1", VALID_CA, 1000.0)
        method, params = rpc.calls[0]
        self.assertEqual(method, "getTokenAccountsByOwner")
        self.assertEqual(params[0], "DevWallet1")
        self.assertEqual(params[1], {"mint": VALID_CA})


# --------------------------------------------------------------------------
# name collisions - other tokens already using this name
# --------------------------------------------------------------------------

def pair(address, name="Dons", symbol="DONS", created_ms=1_600_000_000_000, chain="solana"):
    return {
        "chainId": chain,
        "pairCreatedAt": created_ms,
        "baseToken": {"address": address, "name": name, "symbol": symbol},
    }


class TestFindNameCollisions(unittest.TestCase):
    def setUp(self):
        self.original = fc.http_json
        self.requested = []

    def tearDown(self):
        fc.http_json = self.original

    def stub(self, payload):
        def fake(url, **kwargs):
            self.requested.append(url)
            if isinstance(payload, Exception):
                raise payload
            return payload

        fc.http_json = fake

    def test_skips_without_a_name_and_makes_no_request(self):
        self.stub({"pairs": []})
        result = fc.find_name_collisions(None, VALID_CA)
        self.assertEqual(result.status, "skipped")
        self.assertEqual(self.requested, [])

    def test_finds_an_older_twin(self):
        self.stub({"pairs": [
            pair("OtherMint1", created_ms=1_600_000_000_000),
            pair("OtherMint2", created_ms=1_500_000_000_000),
        ]})
        result = fc.find_name_collisions("Dons", VALID_CA)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.oldest.mint, "OtherMint2")
        self.assertEqual([t.mint for t in result.twins], ["OtherMint2", "OtherMint1"])
        self.assertIn("q=Dons", self.requested[0])

    def test_excludes_the_token_being_analysed(self):
        self.stub({"pairs": [pair(VALID_CA), pair("OtherMint1")]})
        result = fc.find_name_collisions("Dons", VALID_CA)
        self.assertEqual([t.mint for t in result.twins], ["OtherMint1"])

    def test_excludes_other_chains(self):
        self.stub({"pairs": [pair("EthMint1", chain="ethereum"), pair("SolMint1")]})
        result = fc.find_name_collisions("Dons", VALID_CA)
        self.assertEqual([t.mint for t in result.twins], ["SolMint1"])

    def test_excludes_loose_search_matches(self):
        # DexScreener matches substrings; only an exact name or symbol hit is
        # actually the same name, and claiming otherwise is a false alarm.
        self.stub({"pairs": [
            pair("Loose1", name="Dons Inu", symbol="DONSINU"),
            pair("Exact1", name="Dons", symbol="DONS"),
        ]})
        result = fc.find_name_collisions("Dons", VALID_CA)
        self.assertEqual([t.mint for t in result.twins], ["Exact1"])

    def test_matches_on_symbol_and_ignores_case(self):
        self.stub({"pairs": [pair("Sym1", name="Something Else", symbol="dOnS")]})
        result = fc.find_name_collisions("DONS", VALID_CA)
        self.assertEqual([t.mint for t in result.twins], ["Sym1"])

    def test_dedupes_pairs_keeping_the_earliest_per_mint(self):
        self.stub({"pairs": [
            pair("OtherMint1", created_ms=1_600_000_000_000),
            pair("OtherMint1", created_ms=1_400_000_000_000),
            pair("OtherMint1", created_ms=1_700_000_000_000),
        ]})
        result = fc.find_name_collisions("Dons", VALID_CA)
        self.assertEqual(len(result.twins), 1)
        self.assertEqual(result.twins[0].first_pair_unix, 1_400_000_000)

    def test_undated_twins_sort_last_and_never_become_the_oldest(self):
        self.stub({"pairs": [
            pair("NoDate1", created_ms=None),
            pair("Dated1", created_ms=1_600_000_000_000),
        ]})
        result = fc.find_name_collisions("Dons", VALID_CA)
        self.assertEqual([t.mint for t in result.twins], ["Dated1", "NoDate1"])
        self.assertEqual(result.oldest.mint, "Dated1")

    def test_caps_the_number_of_twins_reported(self):
        self.stub({"pairs": [
            pair(f"Mint{i}", created_ms=1_500_000_000_000 + i) for i in range(20)
        ]})
        result = fc.find_name_collisions("Dons", VALID_CA)
        self.assertEqual(len(result.twins), fc.NAME_MATCH_LIMIT)

    def test_no_matches_says_so_rather_than_going_quiet(self):
        self.stub({"pairs": []})
        result = fc.find_name_collisions("Dons", VALID_CA)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.twins, [])
        self.assertIn("No other Solana token", result.detail)

    def test_http_error_degrades_to_unavailable(self):
        self.stub(fc.HttpError(403, "blocked", fc.DEXSCREENER_SEARCH))
        result = fc.find_name_collisions("Dons", VALID_CA)
        self.assertEqual(result.status, "unavailable")
        self.assertIn("403", result.detail)

    def test_any_other_failure_degrades_to_unavailable(self):
        self.stub(ValueError("bad json"))
        result = fc.find_name_collisions("Dons", VALID_CA)
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.searched_name, "Dons")

    def test_serialised_result_carries_the_date_caveat(self):
        self.stub({"pairs": [pair("OtherMint1")]})
        payload = fc.find_name_collisions("Dons", VALID_CA).to_dict()
        self.assertEqual(payload["count"], 1)
        self.assertIn("first traded", payload["caveat"])
        self.assertTrue(payload["twins"][0]["chart_url"].endswith("OtherMint1"))


# --------------------------------------------------------------------------
# the report's token block
# --------------------------------------------------------------------------

class TestReportTokenIdentity(unittest.TestCase):
    def build(self, identity, collisions=None):
        args = fc.parse_args(["--mock", VALID_CA])
        return fc.build_report(
            VALID_CA, make_origin(), fc.CrossCheck(status="unavailable", name="From pump.fun"),
            fc.SearchOutcome(status="ok"), fc.Triage(), args,
            identity=identity, collisions=collisions,
        )

    def test_prefers_the_on_chain_name_over_pump_fun(self):
        report = self.build(fc.TokenIdentity(name="Dons", symbol="DONS", source="token-2022"))
        self.assertEqual(report["token"]["name"], "Dons")
        self.assertEqual(report["token"]["name_source"], "token-2022")

    def test_falls_back_to_pump_fun_when_the_chain_had_nothing(self):
        report = self.build(fc.TokenIdentity())
        self.assertEqual(report["token"]["name"], "From pump.fun")
        self.assertEqual(report["token"]["name_source"], "unavailable")

    def test_name_collisions_absent_when_not_checked(self):
        self.assertIsNone(self.build(fc.TokenIdentity())["name_collisions"])


# --------------------------------------------------------------------------
# wall-clock budget
# --------------------------------------------------------------------------

class TestTimeBudget(unittest.TestCase):
    def test_no_limit_affords_everything(self):
        b = fc.TimeBudget(None)
        self.assertTrue(b.can_afford(9999.0))
        self.assertIsNone(b.remaining())

    def test_keeps_the_search_reserve_back(self):
        # 10s budget, 4s reserved for the search: a 7s phase must not run
        # even though 7 < 10, or the search it was meant to protect starves.
        b = fc.TimeBudget(10.0, reserve=4.0)
        self.assertTrue(b.can_afford(5.0))
        self.assertFalse(b.can_afford(7.0))

    def test_reserve_can_be_waived_per_check(self):
        b = fc.TimeBudget(10.0, reserve=4.0)
        self.assertTrue(b.can_afford(7.0, keep_reserve=False))

    def test_nothing_is_affordable_once_the_budget_is_spent(self):
        b = fc.TimeBudget(10.0, reserve=0.0)
        b.started -= 11.0          # pretend 11s have passed
        self.assertFalse(b.can_afford(0.1))
        self.assertLess(b.remaining(), 0)

    def test_skip_note_says_it_was_not_checked(self):
        # The wording is the point: a skipped check must never be readable as
        # a result. This project's whole premise is that a wrong answer is
        # worse than no answer.
        b = fc.TimeBudget(10.0)
        msg = b.note_skip("Dev history scan")
        self.assertIn("Dev history scan", msg)
        self.assertIn("not checked", msg)
        self.assertIn("not a finding", msg)
        self.assertEqual(b.skipped, [msg])


class TestSkippedScanIsNotACleanResult(unittest.TestCase):
    def test_unscanned_dev_is_not_reported_as_having_no_launches(self):
        scanned = fc.DevProfile(dev_wallet="DevWallet1", other_launches=[])
        skipped = fc.DevProfile(dev_wallet="DevWallet1", other_launches=[], scan_ran=False)
        self.assertFalse(scanned.is_serial_deployer)
        self.assertFalse(skipped.is_serial_deployer)
        # Both have zero launches, but only one of them actually looked.
        self.assertTrue(scanned.to_dict()["scan_ran"])
        self.assertFalse(skipped.to_dict()["scan_ran"])

    def test_risk_verdict_records_that_dev_history_was_not_checked(self):
        rpc = ScriptedRPC({
            "getAccountInfo": {"value": {"data": {"parsed": {"info": {
                "decimals": 6, "supply": "1000000", "mintAuthority": None,
                "freezeAuthority": None}}}}},
            "getTokenLargestAccounts": {"value": []},
        })
        risk = fc.assess_risk(rpc, VALID_CA,
                               fc.DevProfile(dev_wallet="DevWallet1", scan_ran=False))
        self.assertTrue(any("not checked" in n for n in risk.notes))

    def test_risk_says_nothing_extra_when_the_scan_did_run(self):
        rpc = ScriptedRPC({
            "getAccountInfo": {"value": {"data": {"parsed": {"info": {
                "decimals": 6, "supply": "1000000", "mintAuthority": None,
                "freezeAuthority": None}}}}},
            "getTokenLargestAccounts": {"value": []},
        })
        risk = fc.assess_risk(rpc, VALID_CA, fc.DevProfile(dev_wallet="DevWallet1"))
        self.assertFalse(any("not checked" in n for n in risk.notes))


# --------------------------------------------------------------------------
# holder concentration - wallets vs. the launch mechanism
# --------------------------------------------------------------------------

class TestClassifyHolderOwner(unittest.TestCase):
    def test_known_amm_program_is_named(self):
        raydium = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
        self.assertEqual(fc.classify_holder_owner(raydium), (True, "Raydium AMM v4"))

    def test_pump_fun_bonding_curve_is_program_owned(self):
        # The curve holds essentially the whole supply of a young token; if
        # it counts as a holder, every token reads as ~100% concentrated.
        curve = fc.find_program_address(
            [b"bonding-curve", fc.b58_decode(VALID_CA)], fc.PUMP_FUN_PROGRAM_ID)
        is_program, venue = fc.classify_holder_owner(curve)
        self.assertTrue(is_program)
        self.assertEqual(venue, "bonding curve or program vault")

    def test_raydium_pool_authority_is_program_owned(self):
        # A PDA, so off the ed25519 curve, so not a wallet anyone can sign for.
        is_program, _ = fc.classify_holder_owner("5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1")
        self.assertTrue(is_program)

    def test_a_real_wallet_is_not_program_owned(self):
        self.assertEqual(
            fc.classify_holder_owner("ChReo21irgNsRVi5TPvaXgnwxSNnsKJ7Lt1bpAiME8ci"),
            (False, None))

    def test_missing_owner_is_not_assumed_to_be_a_program(self):
        self.assertEqual(fc.classify_holder_owner(None), (False, None))

    def test_undecodable_owner_does_not_raise(self):
        self.assertEqual(fc.classify_holder_owner("not-base58-0OIl"), (False, None))


def largest_accounts(*entries):
    """entries: (token_account, owner, ui_amount)"""
    return {"value": [{"address": ta, "uiAmount": amt} for ta, _, amt in entries]}


def owner_accounts(*entries):
    return {"value": [
        {"data": {"parsed": {"info": {"owner": owner}}}} for _, owner, _ in entries
    ]}


def mint_account(supply="1000000000", decimals=0, mint_auth=None, freeze_auth=None):
    return {"value": {"data": {"parsed": {"info": {
        "decimals": decimals, "supply": supply,
        "mintAuthority": mint_auth, "freezeAuthority": freeze_auth}}}}}


class TestConcentrationExcludesTheLaunchMechanism(unittest.TestCase):
    def setUp(self):
        self.curve = fc.find_program_address(
            [b"bonding-curve", fc.b58_decode(VALID_CA)], fc.PUMP_FUN_PROGRAM_ID)
        self.wallet_a = "ChReo21irgNsRVi5TPvaXgnwxSNnsKJ7Lt1bpAiME8ci"
        self.wallet_b = "8mvXGkM7RJky5JcZ2TjhjEkmqAiPLPiTT9h9TJgLnV7V"

    def _risk(self, entries):
        return fc.assess_risk(ScriptedRPC({
            "getAccountInfo": mint_account(),
            "getTokenLargestAccounts": largest_accounts(*entries),
            "getMultipleAccounts": owner_accounts(*entries),
        }), VALID_CA, None)

    def test_bonding_curve_supply_is_not_counted_as_concentration(self):
        # 90% in the curve, 3% across two wallets. The old behaviour reported
        # 93% and flagged every single token as elevated risk.
        risk = self._risk([
            ("TokenAcctCurve", self.curve, 900_000_000),
            ("TokenAcctA", self.wallet_a, 20_000_000),
            ("TokenAcctB", self.wallet_b, 10_000_000),
        ])
        self.assertEqual(risk.top10_pct, 3.0)
        self.assertEqual(risk.pooled_pct, 90.0)

    def test_pooled_supply_is_explained_rather_than_hidden(self):
        risk = self._risk([
            ("TokenAcctCurve", self.curve, 900_000_000),
            ("TokenAcctA", self.wallet_a, 20_000_000),
        ])
        self.assertTrue(any("excluded from the concentration" in n for n in risk.notes))

    def test_a_curve_heavy_token_is_no_longer_elevated_risk(self):
        risk = self._risk([
            ("TokenAcctCurve", self.curve, 990_000_000),
            ("TokenAcctA", self.wallet_a, 1_000_000),
        ])
        self.assertEqual(risk.verdict, "no_major_red_flags")

    def test_genuine_wallet_concentration_still_flags(self):
        # Same 99% total, but held by actual wallets this time.
        risk = self._risk([
            ("TokenAcctA", self.wallet_a, 800_000_000),
            ("TokenAcctB", self.wallet_b, 190_000_000),
        ])
        self.assertEqual(risk.top10_pct, 99.0)
        self.assertIsNone(risk.pooled_pct)
        self.assertEqual(risk.verdict, "elevated_risk")

    def test_unresolvable_owners_report_no_figure_rather_than_a_wrong_one(self):
        # Without owners, an unclassified bonding curve counts as a whale -
        # exactly the 90-100% false alarm this classification exists to stop.
        def boom(params):
            raise fc.FirstCallooorError("rate limited")

        risk = fc.assess_risk(ScriptedRPC({
            "getAccountInfo": mint_account(),
            "getTokenLargestAccounts": largest_accounts(
                ("TokenAcctCurve", self.curve, 990_000_000)),
            "getMultipleAccounts": boom,
        }), VALID_CA, None)
        self.assertIsNone(risk.top10_pct)
        self.assertIsNone(risk.pooled_pct)
        self.assertNotEqual(risk.verdict, "elevated_risk")

    def test_all_pooled_reports_no_wallet_figure_rather_than_zero(self):
        # Nothing but the curve: "0% concentration" would read as a clean
        # bill of health for a token nobody holds yet.
        risk = self._risk([("TokenAcctCurve", self.curve, 1_000_000_000)])
        self.assertIsNone(risk.top10_pct)
        self.assertEqual(risk.pooled_pct, 100.0)
        self.assertTrue(any("no wallet-concentration figure" in n for n in risk.notes))


# --------------------------------------------------------------------------
# name / ticker lookalikes
# --------------------------------------------------------------------------

class TestClassifyNameMatch(unittest.TestCase):
    def m(self, tn, ts, name="Dons", symbol="DONS"):
        return fc.classify_name_match(name, symbol, tn, ts)

    def test_exact_name(self):
        self.assertEqual(self.m("Dons", "OTHER"), fc.MATCH_SAME_NAME)

    def test_exact_ticker(self):
        self.assertEqual(self.m("Totally Different", "DONS"), fc.MATCH_SAME_TICKER)

    def test_case_is_ignored(self):
        self.assertEqual(self.m("dOnS", "xxx"), fc.MATCH_SAME_NAME)

    def test_leading_dollar_on_a_ticker_is_ignored(self):
        self.assertEqual(self.m("Other", "$DONS"), fc.MATCH_SAME_TICKER)

    def test_our_name_against_their_ticker(self):
        # The swap is a common dodge: same identity, different field.
        self.assertEqual(
            fc.classify_name_match("Dons", None, "Unrelated", "Dons"),
            fc.MATCH_SAME_TICKER)

    def test_punctuation_and_spacing_respelling(self):
        self.assertEqual(self.m("D.O.N.S", "xxx"), fc.MATCH_RESPELLED_NAME)

    def test_digit_for_letter_respelling(self):
        self.assertEqual(
            fc.classify_name_match("Solana", "SOL", "S0LANA", "xxx"),
            fc.MATCH_RESPELLED_NAME)

    def test_doubled_letter_respelling(self):
        self.assertEqual(self.m("Donss", "xxx"), fc.MATCH_RESPELLED_NAME)

    def test_one_edit_is_a_lookalike_not_a_collision(self):
        kind = fc.classify_name_match("Popcat", "POPCAT", "Popcats", "PPCT")
        self.assertIn(kind, (fc.MATCH_LOOKALIKE_NAME, fc.MATCH_RESPELLED_NAME))

    def test_unrelated_token_is_not_a_match(self):
        self.assertIsNone(self.m("Bonk", "BONK"))

    def test_short_tickers_do_not_fuzzy_match(self):
        # At 2-3 characters nearly everything is one edit from everything
        # else; fuzzy matching there would flag half the chain.
        self.assertIsNone(fc.classify_name_match("AI", "AI", "AJ", "AJ"))

    def test_exact_match_still_counts_on_a_short_ticker(self):
        self.assertEqual(
            fc.classify_name_match("AI Thing", "AI", "Other", "ai"),
            fc.MATCH_SAME_TICKER)

    def test_impersonation_set_excludes_mere_lookalikes(self):
        self.assertIn(fc.MATCH_SAME_NAME, fc.IMPERSONATION_MATCHES)
        self.assertIn(fc.MATCH_RESPELLED_TICKER, fc.IMPERSONATION_MATCHES)
        self.assertNotIn(fc.MATCH_LOOKALIKE_NAME, fc.IMPERSONATION_MATCHES)


class TestNameCollisionsWithLookalikes(unittest.TestCase):
    def setUp(self):
        self.original = fc.http_json
        self.requested = []

    def tearDown(self):
        fc.http_json = self.original

    def stub(self, by_query):
        """by_query: query string -> payload or Exception."""
        def fake(url, **kwargs):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("q", [""])[0]
            self.requested.append(q)
            payload = by_query.get(q, {"pairs": []})
            if isinstance(payload, Exception):
                raise payload
            return payload

        fc.http_json = fake

    def test_searches_both_the_name_and_the_ticker(self):
        self.stub({})
        fc.find_name_collisions("Dons Coin", VALID_CA, "DONS")
        self.assertEqual(self.requested, ["Dons Coin", "DONS"])

    def test_does_not_search_the_same_term_twice(self):
        # DexScreener's search is case-insensitive, so a name and ticker
        # that differ only in case are one query, not two wasted requests.
        self.stub({})
        fc.find_name_collisions("DONS", VALID_CA, "dons")
        self.assertEqual(self.requested, ["DONS"])

    def test_finds_a_ticker_copy_the_name_search_would_miss(self):
        self.stub({"DONS": {"pairs": [pair("TickerCopy", name="Completely Other", symbol="DONS")]}})
        result = fc.find_name_collisions("Dons Coin", VALID_CA, "DONS")
        self.assertEqual([t.mint for t in result.twins], ["TickerCopy"])
        self.assertEqual(result.twins[0].match, fc.MATCH_SAME_TICKER)

    def test_labels_why_each_twin_matched(self):
        self.stub({"Dons": {"pairs": [
            pair("Exact1", name="Dons", symbol="DONS", created_ms=1_600_000_000_000),
            pair("Respelled1", name="D0ns", symbol="XXX", created_ms=1_650_000_000_000),
        ]}})
        result = fc.find_name_collisions("Dons", VALID_CA, "DONS")
        by_mint = {t.mint: t.match for t in result.twins}
        self.assertEqual(by_mint["Exact1"], fc.MATCH_SAME_NAME)
        self.assertEqual(by_mint["Respelled1"], fc.MATCH_RESPELLED_NAME)

    def test_real_collisions_sort_above_mere_lookalikes(self):
        self.stub({"Dons": {"pairs": [
            pair("Lookalike1", name="Donsy", symbol="XXX", created_ms=1_400_000_000_000),
            pair("Exact1", name="Dons", symbol="DONS", created_ms=1_600_000_000_000),
        ]}})
        result = fc.find_name_collisions("Dons", VALID_CA, "DONS")
        # Lookalike1 is older, but an outright collision is the thing to see.
        self.assertEqual(result.twins[0].mint, "Exact1")

    def test_oldest_is_a_real_collision_not_an_older_lookalike(self):
        self.stub({"Dons": {"pairs": [
            pair("Lookalike1", name="Donsy", symbol="XXX", created_ms=1_400_000_000_000),
            pair("Exact1", name="Dons", symbol="DONS", created_ms=1_600_000_000_000),
        ]}})
        result = fc.find_name_collisions("Dons", VALID_CA, "DONS")
        self.assertEqual(result.oldest.mint, "Exact1")

    def test_no_oldest_when_only_lookalikes_were_found(self):
        # Nothing here actually holds the name, so there is no "an older
        # token already uses this name" claim to make.
        self.stub({"Dons": {"pairs": [pair("Lookalike1", name="Donsy", symbol="XXX")]}})
        result = fc.find_name_collisions("Dons", VALID_CA, "DONS")
        self.assertEqual(len(result.twins), 1)
        self.assertIsNone(result.oldest)

    def test_one_failed_search_degrades_to_a_partial_list_not_unavailable(self):
        self.stub({
            "Dons Coin": {"pairs": [pair("Exact1", name="Dons Coin", symbol="DONS")]},
            "DONS": fc.HttpError(429, "slow down", fc.DEXSCREENER_SEARCH),
        })
        result = fc.find_name_collisions("Dons Coin", VALID_CA, "DONS")
        self.assertEqual(result.status, "ok")
        self.assertEqual([t.mint for t in result.twins], ["Exact1"])
        self.assertIn("may be incomplete", result.detail)

    def test_both_searches_failing_is_unavailable(self):
        err = fc.HttpError(403, "blocked", fc.DEXSCREENER_SEARCH)
        self.stub({"Dons Coin": err, "DONS": err})
        result = fc.find_name_collisions("Dons Coin", VALID_CA, "DONS")
        self.assertEqual(result.status, "unavailable")

    def test_skipped_without_a_name_or_a_ticker(self):
        self.stub({})
        result = fc.find_name_collisions(None, VALID_CA, None)
        self.assertEqual(result.status, "skipped")
        self.assertEqual(self.requested, [])

    def test_runs_on_a_ticker_alone(self):
        self.stub({"DONS": {"pairs": [pair("Exact1", name="Other", symbol="DONS")]}})
        result = fc.find_name_collisions(None, VALID_CA, "DONS")
        self.assertEqual([t.mint for t in result.twins], ["Exact1"])

    def test_serialised_result_breaks_down_the_match_kinds(self):
        self.stub({"Dons": {"pairs": [
            pair("Exact1", name="Dons", symbol="DONS"),
            pair("Look1", name="Donsy", symbol="XXX"),
        ]}})
        payload = fc.find_name_collisions("Dons", VALID_CA, "DONS").to_dict()
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["impersonation_count"], 1)
        self.assertEqual(payload["match_breakdown"][fc.MATCH_SAME_NAME], 1)
        self.assertTrue(payload["twins"][0]["is_impersonation"])


# --------------------------------------------------------------------------
# dev portfolio - everything the wallet holds right now
# --------------------------------------------------------------------------

def holding(mint, amount, decimals=6):
    return {"account": {"data": {"parsed": {"info": {
        "mint": mint,
        "tokenAmount": {"uiAmount": amount, "decimals": decimals},
    }}}}}


def supply_value(supply, decimals=6):
    return {"data": {"parsed": {"info": {"supply": supply, "decimals": decimals}}}}


class PortfolioRPC(ScriptedRPC):
    """Routes the two getTokenAccountsByOwner programs separately, and the
    several getMultipleAccounts calls (supplies, then names) in order."""

    def __init__(self, *, classic=None, token2022=None, supplies=None,
                 multi_sequence=None, fail_programs=()):
        super().__init__({})
        self.classic = classic if classic is not None else []
        self.token2022 = token2022 if token2022 is not None else []
        self.supplies = supplies or {}
        self.multi_sequence = list(multi_sequence or [])
        self.fail_programs = set(fail_programs)
        self.multi_calls = 0

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "getTokenAccountsByOwner":
            program = params[1].get("programId")
            if program in self.fail_programs:
                raise fc.FirstCallooorError("rate limited")
            return {"value": self.classic if program == fc.TOKEN_PROGRAM_ID else self.token2022}
        if method == "getMultipleAccounts":
            self.multi_calls += 1
            if self.multi_sequence:
                return self.multi_sequence.pop(0)
            # default: the supply lookup
            return {"value": [self.supplies.get(m) for m in params[0]]}
        return None


MINT_A = "A1bcdefghjkmnpqrstuvwxyzABCDEFGH23456789pump"
MINT_B = "B2bcdefghjkmnpqrstuvwxyzABCDEFGH23456789pump"


class TestGetWalletPortfolio(unittest.TestCase):
    def test_no_wallet_is_skipped_and_costs_no_call(self):
        rpc = PortfolioRPC()
        result = fc.get_wallet_portfolio(rpc, None, VALID_CA)
        self.assertEqual(result.status, "skipped")
        self.assertEqual(rpc.calls, [])

    def test_reads_both_token_programs(self):
        rpc = PortfolioRPC(classic=[holding(MINT_A, 10.0)],
                            token2022=[holding(MINT_B, 20.0)])
        result = fc.get_wallet_portfolio(rpc, "DevWallet1", VALID_CA)
        self.assertEqual(result.total_positions, 2)
        programs = [p[1].get("programId") for m, p in rpc.calls
                    if m == "getTokenAccountsByOwner"]
        self.assertEqual(programs, [fc.TOKEN_PROGRAM_ID, fc.TOKEN_2022_PROGRAM_ID])

    def test_zero_balances_are_closed_positions_not_holdings(self):
        rpc = PortfolioRPC(classic=[holding(MINT_A, 0), holding(MINT_B, 5.0)])
        result = fc.get_wallet_portfolio(rpc, "DevWallet1", VALID_CA)
        self.assertEqual([e.mint for e in result.entries], [MINT_B])

    def test_sums_several_accounts_for_the_same_mint(self):
        rpc = PortfolioRPC(classic=[holding(MINT_A, 10.0), holding(MINT_A, 2.5)])
        result = fc.get_wallet_portfolio(rpc, "DevWallet1", VALID_CA)
        self.assertEqual(result.entries[0].amount_ui, 12.5)

    def test_ranks_by_share_of_each_tokens_own_supply(self):
        # 100 of a 1,000-supply token is a bigger position than 500 of a
        # 1,000,000-supply one, even though 500 is the larger number.
        rpc = PortfolioRPC(
            classic=[holding(MINT_A, 500.0), holding(MINT_B, 100.0)],
            supplies={MINT_A: supply_value(str(1_000_000 * 10**6)),
                      MINT_B: supply_value(str(1_000 * 10**6))},
        )
        result = fc.get_wallet_portfolio(rpc, "DevWallet1", VALID_CA)
        self.assertEqual([e.mint for e in result.entries], [MINT_B, MINT_A])
        self.assertEqual(result.entries[0].pct_of_supply, 10.0)

    def test_the_analysed_token_sorts_first_whatever_its_size(self):
        rpc = PortfolioRPC(
            classic=[holding(MINT_A, 999_999.0), holding(VALID_CA, 1.0)],
            supplies={MINT_A: supply_value(str(1_000_000 * 10**6)),
                      VALID_CA: supply_value(str(1_000_000_000 * 10**6))},
        )
        result = fc.get_wallet_portfolio(rpc, "DevWallet1", VALID_CA)
        self.assertEqual(result.entries[0].mint, VALID_CA)
        self.assertTrue(result.entries[0].is_this_token)

    def test_unknown_supply_leaves_share_null_and_sorts_last(self):
        rpc = PortfolioRPC(
            classic=[holding(MINT_A, 5.0), holding(MINT_B, 5.0)],
            supplies={MINT_B: supply_value(str(1_000 * 10**6))},
        )
        result = fc.get_wallet_portfolio(rpc, "DevWallet1", VALID_CA)
        self.assertEqual(result.entries[0].mint, MINT_B)
        self.assertIsNone(result.entries[-1].pct_of_supply)

    def test_empty_wallet_says_so_rather_than_going_quiet(self):
        result = fc.get_wallet_portfolio(PortfolioRPC(), "DevWallet1", VALID_CA)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.total_positions, 0)
        self.assertIn("no tokens", result.detail)

    def test_both_programs_failing_is_unavailable(self):
        rpc = PortfolioRPC(fail_programs=(fc.TOKEN_PROGRAM_ID, fc.TOKEN_2022_PROGRAM_ID))
        result = fc.get_wallet_portfolio(rpc, "DevWallet1", VALID_CA)
        self.assertEqual(result.status, "unavailable")

    def test_one_program_failing_is_a_partial_list_that_says_so(self):
        rpc = PortfolioRPC(token2022=[holding(MINT_B, 5.0)],
                            fail_programs=(fc.TOKEN_PROGRAM_ID,))
        result = fc.get_wallet_portfolio(rpc, "DevWallet1", VALID_CA)
        self.assertEqual(result.status, "ok")
        self.assertEqual([e.mint for e in result.entries], [MINT_B])
        self.assertIn("missing positions", result.detail)

    def test_caps_how_many_positions_are_named(self):
        mints = [f"Mnt{i}".ljust(44, "x") for i in range(20)]
        rpc = PortfolioRPC(classic=[holding(m, 10.0) for m in mints])
        result = fc.get_wallet_portfolio(rpc, "DevWallet1", VALID_CA, name_limit=5)
        self.assertEqual(result.total_positions, 20)
        self.assertEqual(len(result.entries), 5)

    def test_serialised_result_carries_the_no_price_caveat(self):
        rpc = PortfolioRPC(classic=[holding(MINT_A, 10.0)])
        payload = fc.get_wallet_portfolio(rpc, "DevWallet1", VALID_CA).to_dict()
        self.assertEqual(payload["total_positions"], 1)
        self.assertIn("no price source", payload["caveat"])
        self.assertIn("already sold shows nothing", payload["caveat"])


class TestResolveMintNames(unittest.TestCase):
    def test_token_2022_names_cost_a_single_batched_call(self):
        rpc = PortfolioRPC(multi_sequence=[{"value": [
            {"data": {"parsed": {"info": token2022_mint(name="Alpha", symbol="ALP")}}},
            {"data": {"parsed": {"info": token2022_mint(name="Beta", symbol="BET")}}},
        ]}])
        names = fc._resolve_mint_names(rpc, [MINT_A, MINT_B])
        self.assertEqual(names[MINT_A].name, "Alpha")
        self.assertEqual(names[MINT_B].name, "Beta")
        self.assertEqual(rpc.multi_calls, 1)

    def test_falls_back_to_one_batched_metaplex_call(self):
        rpc = PortfolioRPC(multi_sequence=[
            {"value": [{"data": {"parsed": {"info": {"decimals": 6}}}}]},   # no embedded name
            metaplex_batch := {"value": [metaplex_account(name="Classic", symbol="CLS")["value"]]},
        ])
        names = fc._resolve_mint_names(rpc, [MINT_A])
        self.assertEqual(names[MINT_A].name, "Classic")
        self.assertEqual(names[MINT_A].source, "metaplex")
        self.assertEqual(rpc.multi_calls, 2)

    def test_no_mints_makes_no_call(self):
        rpc = PortfolioRPC()
        self.assertEqual(fc._resolve_mint_names(rpc, []), {})
        self.assertEqual(rpc.calls, [])

    def test_rpc_failure_leaves_tokens_unnamed_rather_than_raising(self):
        class Boom(ScriptedRPC):
            def call(self, method, params):
                raise fc.FirstCallooorError("down")

        self.assertEqual(fc._resolve_mint_names(Boom({}), [MINT_A]), {})


# --------------------------------------------------------------------------
# budget pricing from measured latency
# --------------------------------------------------------------------------

class FakeTimedRPC:
    def __init__(self, avg):
        self._avg = avg

    @property
    def avg_call_seconds(self):
        return self._avg


class TestBudgetPricing(unittest.TestCase):
    def test_falls_back_to_a_pessimistic_rate_before_anything_is_measured(self):
        b = fc.TimeBudget(10.0)
        expected = 5 * fc.ASSUMED_CALL_SECONDS * fc.COST_SAFETY_FACTOR
        self.assertAlmostEqual(b.price(5, FakeTimedRPC(None)), expected)
        self.assertAlmostEqual(b.price(5, None), expected)

    def test_a_fast_endpoint_prices_work_cheaply(self):
        # The whole point: on a fast endpoint the dev scan must fit, where a
        # fixed pessimistic estimate would have skipped it.
        b = fc.TimeBudget(8.0)
        b.started -= 2.0                       # 6s left
        fast = FakeTimedRPC(0.05)
        self.assertTrue(b.can_afford_calls(fc.CALLS_DEV_SCAN, fast))
        self.assertTrue(b.can_afford_calls(fc.CALLS_DEV_PORTFOLIO, fast))

    def test_a_slow_endpoint_prices_the_same_work_out_of_reach(self):
        b = fc.TimeBudget(8.0)
        b.started -= 2.0                       # same 6s left
        slow = FakeTimedRPC(1.2)
        self.assertFalse(b.can_afford_calls(fc.CALLS_DEV_SCAN, slow))

    def test_safety_factor_is_applied(self):
        b = fc.TimeBudget(10.0)
        self.assertAlmostEqual(
            b.price(10, FakeTimedRPC(0.1)), 10 * 0.1 * fc.COST_SAFETY_FACTOR)

    def test_reserve_defaults_to_zero(self):
        # The search runs before the extras, so there is nothing downstream
        # to protect - a non-zero default would hold time back from every
        # caller that didn't think about it, which is what skipped the dev
        # scan on deployments that had plenty of time for it.
        self.assertEqual(fc.TimeBudget(10.0).reserve, 0.0)

    def test_every_phase_fits_a_normal_budget_on_a_fast_endpoint(self):
        # The regression this pricing exists to prevent: on a fast endpoint
        # with 6s left, every extra must run rather than stand down. Uses
        # the WEB caps (6 and 8), which is the deployment that has a wall
        # clock at all - the CLI's much larger caps are unbounded by design.
        budget = fc.TimeBudget(8.0)
        budget.started -= 2.0
        fast = FakeTimedRPC(0.06)
        phases = (
            fc.calls_for_first_buyers(6),
            fc.calls_for_dev_scan(8),
            fc.CALLS_RISK_CHECK, fc.CALLS_DEV_HOLDING,
            fc.CALLS_DEV_PORTFOLIO, fc.CALLS_NAME_CHECK,
        )
        for calls in phases:
            self.assertTrue(budget.can_afford_calls(calls, fast))
            budget.started -= calls * 0.06      # charge what it really costs

    def test_call_estimates_account_for_the_version_retry(self):
        # One signature can cost two getTransaction calls; counting one
        # underestimates the scan phases by half, which overruns the wall.
        self.assertEqual(fc.calls_for_first_buyers(6), 12)
        self.assertEqual(fc.calls_for_dev_scan(8), 17)

    def test_no_limit_affords_any_call_count(self):
        self.assertTrue(fc.TimeBudget(None).can_afford_calls(10_000, FakeTimedRPC(5.0)))


class TestRpcLatencyTracking(unittest.TestCase):
    def test_average_is_none_before_any_call(self):
        self.assertIsNone(fc.SolanaRPC("stub://rpc").avg_call_seconds)

    def test_failed_calls_still_count_toward_the_average(self):
        # A slow endpoint that times out is the strongest possible evidence
        # that it is slow; ignoring those samples would price the next phase
        # off only the calls that happened to succeed.
        rpc = fc.SolanaRPC("stub://rpc")
        original = fc.http_json
        try:
            def boom(*a, **k):
                raise fc.HttpError(429, "slow down", "stub://rpc")

            fc.http_json = boom
            with self.assertRaises(fc.FirstCallooorError):
                rpc.call("getSignaturesForAddress", [])
        finally:
            fc.http_json = original
        self.assertEqual(rpc.call_count, 1)
        self.assertIsNotNone(rpc.avg_call_seconds)

    def test_successful_calls_are_counted(self):
        rpc = fc.SolanaRPC("stub://rpc")
        original = fc.http_json
        try:
            fc.http_json = lambda *a, **k: {"result": 1}
            rpc.call("getAccountInfo", [])
            rpc.call("getAccountInfo", [])
        finally:
            fc.http_json = original
        self.assertEqual(rpc.call_count, 2)
        self.assertGreaterEqual(rpc.avg_call_seconds, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
