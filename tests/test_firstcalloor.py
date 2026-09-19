"""Tests for firstcalloor.

The X API and pump.fun are stubbed at the HTTP layer, so the search strategy
(window expansion, cap contraction, rate-limit handling) is verified without
network access or an API key.
"""

import argparse
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

        buyers, notes = fc.find_first_buyers(rpc, origin, limit=10, scan_cap=10)
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

        buyers, _ = fc.find_first_buyers(rpc, origin)
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

        buyers, notes = fc.find_first_buyers(rpc, origin)
        self.assertEqual(buyers, [])
        self.assertTrue(notes)

    def test_respects_limit(self):
        batch = [{"signature": f"buy{i}", "blockTime": 1000 + i} for i in range(5, 0, -1)]
        batch.append({"signature": "genesis_sig", "blockTime": 1000})
        origin = make_origin(post_genesis_batch=batch)
        txs = {f"buy{i}": make_tx(f"Buyer{i}", VALID_CA, pre_amt=None, post_amt=100.0)
               for i in range(1, 6)}
        rpc = ScriptedRPC({"getTransaction": lambda p: txs.get(p[0])})

        buyers, _ = fc.find_first_buyers(rpc, origin, limit=2, scan_cap=10)
        self.assertEqual(len(buyers), 2)

    def test_respects_scan_cap_even_with_more_candidates(self):
        batch = [{"signature": f"buy{i}", "blockTime": 1000 + i} for i in range(10, 0, -1)]
        batch.append({"signature": "genesis_sig", "blockTime": 1000})
        origin = make_origin(post_genesis_batch=batch)
        txs = {f"buy{i}": make_tx(f"Buyer{i}", VALID_CA, pre_amt=None, post_amt=100.0)
               for i in range(1, 11)}
        rpc = ScriptedRPC({"getTransaction": lambda p: txs.get(p[0])})

        buyers, _ = fc.find_first_buyers(rpc, origin, limit=100, scan_cap=3)
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

        buyers, _ = fc.find_first_buyers(rpc, origin)
        self.assertEqual(len(buyers), 1)

    def test_skipped_when_walk_did_not_reach_true_genesis(self):
        origin = make_origin(exhausted=False, post_genesis_batch=[{"signature": "x"}])
        buyers, notes = fc.find_first_buyers(ScriptedRPC({}), origin)
        self.assertEqual(buyers, [])
        self.assertTrue(any("genesis" in n.lower() for n in notes))

    def test_empty_batch_is_handled_without_crashing(self):
        origin = make_origin(exhausted=True, post_genesis_batch=[])
        buyers, notes = fc.find_first_buyers(ScriptedRPC({}), origin)
        self.assertEqual(buyers, [])


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
            "getMultipleAccounts": lambda p: {"value": [None]},
        })
        risk = fc.assess_risk(rpc, VALID_CA, None)
        self.assertEqual(risk.verdict, "elevated_risk")
        self.assertGreater(risk.top10_pct, 70)

    def test_moderate_concentration_is_some_risk_signals(self):
        holders = [{"address": "big", "uiAmount": 500_000.0}]
        rpc = ScriptedRPC({
            "getAccountInfo": lambda p: self._mint_info_response(None, None, supply="1000000000000", decimals=6),
            "getTokenLargestAccounts": lambda p: {"value": holders},
            "getMultipleAccounts": lambda p: {"value": [None]},
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

    def test_owner_resolution_failure_keeps_amounts(self):
        holders = [{"address": "acc1", "uiAmount": 42.0}]

        def boom(params):
            raise fc.FirstCallooorError("rate limited")

        rpc = ScriptedRPC({
            "getTokenLargestAccounts": lambda p: {"value": holders},
            "getMultipleAccounts": boom,
        })
        result, note = fc.get_top_holders(rpc, VALID_CA, supply_ui=100.0)
        self.assertIsNone(note)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
