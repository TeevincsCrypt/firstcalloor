# FIRSTCALLOOR

**First Mentioned By — for Solana memecoins.**

Give it a contract address. It establishes a trustworthy "zero point" from the
token's on-chain creation timestamp, searches X for mentions of that exact CA
string, throws out the noise, and tells you who called it first — with a link,
a timestamp, and a time-since-launch for every claim.

```
FIRST CALL
------------------------------------------------------------------------------
  @chain_sniffer  14.8K followers
  Called 41s after launch  (2026-09-19T18:30:16Z)
  https://x.com/chain_sniffer/status/...
  new pump.fun mint just deployed 6g7YqRn...pump - dev holds 3%, bonding curve fresh
```

---

Runs as a **website** and as a **CLI**, off the same engine.

## Quick start

No dependencies — stdlib only, Python 3.9+. Nothing to install.

```bash
python dev_server.py                       # website at http://localhost:8000
python firstcalloor.py <CONTRACT_ADDRESS>  # same engine, in the terminal
```

The CLI prints a table and writes `firstcalloor-<ca8>.json`.

**Without an X API key the on-chain half still works** — real creation
timestamp, real genesis transaction, verified against the chain. The mention
search then reports that it did not run, rather than implying there was
nothing to find. Add `--mock` on the CLI to see the full output shape against
synthetic mentions.

## Deploy to Vercel

Import the repo at [vercel.com/new](https://vercel.com/new). No build command,
no framework preset, no `requirements.txt` — Vercel serves `index.html`
statically and `api/analyze.py` as a Python function automatically.

Then set environment variables under **Settings → Environment Variables**:

| Variable | Needed? | Why |
|---|---|---|
| `SOLANA_RPC_URL` | **Strongly recommended** | The default public RPC rate-limits cloud IPs hard; a hosted deploy will hit 429s without a dedicated endpoint. A free Helius key is enough. |
| `X_BEARER_TOKEN` | Required for mentions | Without it the site shows on-chain data only. |
| `X_FULL_ARCHIVE` | Optional | `1` to use full-archive search (X API Pro). |
| `FIRSTCALLOOR_SIG_PAGE_CAP` | Optional | Signature pages per request, default `12`. Lower it if you hit the function timeout. |

**Serverless timeouts matter here.** Walking a busy token's signature history
back to genesis is the slow part, and Vercel Hobby caps functions at 10s. The
web path uses a tighter page cap than the CLI and says which happened —
`reached genesis` or `capped` — rather than silently reporting a wrong birth
time.

### Project layout

```
index.html           frontend, vanilla JS, no build step
api/analyze.py       serverless endpoint: GET /api/analyze?ca=<CA>
api/_engine.py       the engine (underscore = bundled by Vercel, not routed)
firstcalloor.py      CLI wrapper around that same engine
dev_server.py        local server mirroring Vercel's routing
tests/               33 tests, no network or API key needed
```

---

## What you need to provide

### 1. X (Twitter) API key — the hard requirement

This is the part that gates everything. **The X free tier has no search access
at all** (it is post-only, 100 reads/month), so FIRSTCALLOOR cannot find
mentions on it. Your options:

| Tier | Cost | Endpoint | Lookback | What it means here |
|---|---|---|---|---|
| Free | $0 | none | — | **Search unavailable.** Use `--mock`. |
| Basic | ~$200/mo | `/2/tweets/search/recent` | **7 days** | Works only on tokens launched in the last week. |
| Pro | ~$5,000/mo | `/2/tweets/search/all` | 2006 → now | The tier that makes this tool work on any token. Pass `--full-archive`. |

Get a token at [developer.x.com](https://developer.x.com) → create a Project +
App → **Keys and tokens** → **Bearer Token**. It is the long `AAAAAAAA...`
string, *not* the API Key/Secret pair.

```bash
export X_BEARER_TOKEN="AAAAAAAA..."     # or pass --bearer-token
```

The 7-day limit is not a detail you can work around. On a Basic key, if the
token launched more than a week ago, the search window is clamped and the tool
says so loudly — because the real first call is provably outside what it can
see, and reporting the earliest *visible* mention as "First Call" would be a
lie.

### 2. Solana RPC — optional

Defaults to the public endpoint, no key, works out of the box:

```
https://api.mainnet-beta.solana.com
```

It rate-limits hard, and walking a busy token's signature history back to
genesis can take many pages. If you hit 429s or the run is slow, get a free
[Helius](https://helius.dev) key (1M credits/month) and:

```bash
export SOLANA_RPC_URL="https://mainnet.helius-rpc.com/?api-key=YOUR_KEY"
```

### 3. pump.fun — nothing to provide

Used as an advisory cross-check on the creation timestamp. Its public API sits
behind Cloudflare and is frequently unreachable; when it is, the run continues
and the cross-check reports `unavailable`. It is never load-bearing.

---

## How it decides who was first

**The zero point is on-chain, not self-reported.** `getSignaturesForAddress` is
paginated backwards to the mint's very first transaction, then `getTransaction`
confirms its block time. Any API's `createdAt` field is treated as a claim to
be checked, never as truth — if pump.fun disagrees by more than 60 seconds, the
output flags it and keeps the on-chain value.

**The query is the CA string, never the ticker.** Tickers collide constantly;
a search for `$MOON` returns a dozen unrelated tokens. The exact mint address
is the only unambiguous identifier, which is also why a ticker as input is
rejected rather than guessed at.

**Finding the *earliest* mention is not the same as fetching 200 tweets.**
X API v2 has no ascending sort. Paginating newest-first on a busy token burns
the whole quota on late mentions and never reaches the first call. Instead:

1. Anchor a search window at the launch timestamp.
2. Expand it outward (1h → 6h → 24h → 72h → 7d) only until mentions appear.
3. If the window overflows the tweet cap, **contract** it back toward launch
   until the earliest mentions fit inside it.

That keeps quota spend low and, more importantly, makes "first" mean first.

**What gets filtered, and why it is shown anyway:**

| Category | Treatment |
|---|---|
| Tweeted **before** the mint existed | Excluded from ranking, listed separately. A recycled or impersonated CA, a scraper reposting old text, or clock skew — never a real call. |
| Retweets / quote tweets | Excluded as non-original. `--include-retweets` to rank them. |
| Accounts **under 24h old** | Surfaced under "possible bot mentions", never folded into the timeline. Usually a bot; occasionally a real early caller — you decide. |
| Duplicate tweet IDs | Dropped, counted. |

Nothing is silently discarded. Every filtered mention appears in the console
output and the JSON, with the reason attached.

---

## Options

```
--mock                  synthetic mentions, no X key needed
--json PATH             where to write the result set
--full-archive          use /2/tweets/search/all (X API Pro+)
--max-tweets N          cap on tweets fetched (default 200)
--sig-page-cap N        max signature pages to walk (default 50 = 50k txs)
--include-retweets      rank retweets and quote tweets as mentions
--pre-window-hours N    hours before launch to probe for recycled CAs (0 off)
--no-cross-check        skip the pump.fun cross-check
--rpc URL               Solana RPC endpoint
--bearer-token TOKEN    X bearer token (overrides env)
-v, --verbose           log pagination to stderr
```

**Exit codes:** `0` complete · `2` bad input · `3` search incomplete (rate
limit, no credentials, clamped window) · `1` error.

`3` is the one to watch in scripts: it means the answer you got is real but
partial, and "no mentions found" under a `3` is not evidence of absence.

---

## Output

Console table plus a JSON file carrying the full result set — `first_call`,
`timeline`, `possible_bot_mentions`, `filtered.pre_creation`,
`filtered.retweets`, the on-chain `origin` block with its genesis signature,
and `search` metadata including every note about incompleteness.

```json
{
  "first_call": {
    "handle": "chain_sniffer",
    "followers_at_query_time": 14820,
    "tweeted_at": "2026-09-19T18:30:16Z",
    "seconds_since_token_creation": 41,
    "time_since_creation": "41s",
    "url": "https://x.com/chain_sniffer/status/...",
    "flags": []
  }
}
```

---

## Tests

```bash
python -m unittest discover -s tests -v
```

33 tests. The X API and pump.fun are stubbed at the HTTP layer, so the search
strategy — window expansion, cap contraction, rate-limit cut-off, the 7-day
clamp — is verified without network access or an API key.

---

## Scope (v1)

**In:** CA → on-chain creation timestamp → X search → filtered ranked timeline
→ console + JSON.

**Out:** Telegram scanning, follower-weighted "market mover" scoring,
persistence. No database — every request is computed fresh.
