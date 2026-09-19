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

    def tearDown(self):
        fc.http_json = self._real

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
    def __init__(self, pages, block_time=1789842575):
        super().__init__("stub://rpc")
        self.pages = pages
        self._bt = block_time
        self.requests = []

    def call(self, method, params):
        self.requests.append(method)
        if method == "getSignaturesForAddress":
            return self.pages.pop(0) if self.pages else []
        if method == "getTransaction":
            return {"blockTime": self._bt}
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

    def tearDown(self):
        fc.http_json = self._real

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

    def tearDown(self):
        fc.http_json = self._real

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
