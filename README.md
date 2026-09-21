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

Then set environment variables under **Settings → Environment Variables**.
**The Value field holds only the value** — e.g. `https://mainnet.helius-rpc.com/?api-key=YOUR_KEY`,
never `SOLANA_RPC_URL = https://...` or a quoted string. (The tool now
auto-corrects that specific mistake for `SOLANA_RPC_URL` and the credential
variables if you make it anyway, but it's cleaner to just enter the bare value.)

| Variable | Needed? | Why |
|---|---|---|
| `SOLANA_RPC_URL` | **Strongly recommended** | The default public RPC rate-limits cloud IPs hard; a hosted deploy will hit 429s without a dedicated endpoint. A free Helius key is enough. |
| `TWITTERAPI_IO_KEY` | Preferred for mentions | Preferred over `X_BEARER_TOKEN` when both are set — no 7-day lookback wall. Without either, the site shows on-chain data only. |
| `X_BEARER_TOKEN` | Fallback for mentions | Used only if `TWITTERAPI_IO_KEY` is unset. |
| `X_FULL_ARCHIVE` | Optional | `1` to use full-archive search on the official X API (Pro tier). No effect on twitterapi.io. |
| `FIRSTCALLOOR_SIG_PAGE_CAP` | Optional | Signature pages per request, default `12`. Lower it if you hit the function timeout. |
| `FIRSTCALLOOR_HTTP_TIMEOUT` | Optional | Per-request timeout in seconds, default `6`. The CLI uses a generous 30s; the web path needs every call to fail fast instead of eating the function's whole budget. |
| `FIRSTCALLOOR_HTTP_RETRIES` | Optional | Retries per request, default `1`. Raise cautiously — each retry can add several seconds. |
| `FIRSTCALLOOR_TIME_BUDGET` | Optional | Wall-clock seconds for the whole run, default `7.5`. Optional on-chain extras stand down (and say so) rather than overrun Vercel's 10s function limit. |
| `FIRSTCALLOOR_PRE_WINDOW_HOURS` | Optional | Hours probed before launch for recycled-CA mentions, default `0` on web (`24` on the CLI) — it's an extra, non-essential network round trip the tight web budget usually can't spare. |

**Serverless timeouts matter here.** Walking a busy token's signature history
back to genesis is the slow part, and Vercel Hobby caps functions at 10s. The
web path uses a tighter page cap than the CLI and says which happened —
`reached genesis` or `capped` — rather than silently reporting a wrong birth
time.

This also applies below the surface: every HTTP call the web path makes uses
a short timeout and minimal retries (`FIRSTCALLOOR_HTTP_TIMEOUT`/`_RETRIES`
above), not the CLI's generous 30s/4-retry defaults. Under those CLI
defaults, one slow or rate-limited call — the public Solana RPC throttling a
Vercel IP, a lagging provider — can burn the *entire* function's time budget
by itself; Vercel then kills the process before this tool's own error
handling runs, and the browser sees the platform's own opaque crash page
(`Unexpected token 'A', "..."`) instead of JSON. The tightened budget makes a
bad call fail fast with a clear, in-budget note instead.

### Project layout

```
index.html           frontend, vanilla JS, no build step
api/analyze.py       serverless endpoint: GET /api/analyze?ca=<CA>
api/_engine.py       the engine (underscore = bundled by Vercel, not routed)
firstcalloor.py      CLI wrapper around that same engine
dev_server.py        local server mirroring Vercel's routing
tests/               187 tests, no network or API key needed
```

---

## What you need to provide

### 1. A mention-search provider — the hard requirement

This is the part that gates everything. FIRSTCALLOOR supports two providers
and auto-detects whichever is configured; if both are, **twitterapi.io is
preferred**, since it has no equivalent of the official API's 7-day wall —
which is what actually blocks a "first call" lookup on a token that already
ran, i.e. the normal case someone asks this question about.

**twitterapi.io** (recommended) — unofficial third-party reseller of X's
search index, priced per request/credit rather than a fixed monthly tier.

```bash
export TWITTERAPI_IO_KEY="..."          # from twitterapi.io's dashboard
```

Being unofficial cuts both ways: no 7-day wall and far cheaper than X Pro, but
it can break or change shape without notice, and its terms are the reseller's,
not X's. Worth knowing before depending on it for anything beyond personal use.

**"Rate limit reached mid-pagination" doesn't mean your credits ran out.**
Free/trial tiers usually cap requests-per-minute separately from the total
credit pool, and a **quiet token** (few or no mentions yet) is the case that
trips it — every search window comes back empty, so the tool tries
progressively wider ones until something turns up, firing one request per
width. It's paced and capped at 3 widths specifically to avoid this, but a
low enough per-minute cap can still be hit. A real rate-limit reset is
typically tens of seconds — far longer than a serverless function's entire
~10s budget — so the honest move when it happens is to say the search was
cut short rather than pretend to wait it out. The on-chain half is
unaffected either way.

**Official X API** — used automatically if `TWITTERAPI_IO_KEY` isn't set.
**The free tier has no search access at all** (it's post-only, 100
reads/month), so FIRSTCALLOOR cannot find mentions on it.

| Tier | Cost | Endpoint | Lookback | What it means here |
|---|---|---|---|---|
| Free | $0 | none | — | **Search unavailable.** Use `--mock`. |
| Basic | ~$200/mo | `/2/tweets/search/recent` | **7 days** | Works only on tokens launched in the last week. |
| Pro | ~$5,000/mo | `/2/tweets/search/all` | 2006 → now | Works on any token. Pass `--full-archive`. |

Get a token at [developer.x.com](https://developer.x.com) → create a Project +
App → **Keys and tokens** → **Bearer Token** (the long `AAAAAAAA...` string,
*not* the API Key/Secret pair).

```bash
export X_BEARER_TOKEN="AAAAAAAA..."     # or pass --bearer-token
```

The 7-day limit is not a detail you can work around on Basic. If the token
launched more than a week ago, the search window is clamped and the tool says
so loudly — because the real first call is provably outside what it can see,
and reporting the earliest *visible* mention as "First Call" would be a lie.
(twitterapi.io has no such wall, which is the whole reason it's preferred.)

To force a specific provider rather than auto-detect: `--provider x` or
`--provider twitterapi` on the CLI.

**A note on verification.** The twitterapi.io client is built from its
documented response shape and unit-tested against a stub of it, but this
project's build environment has no network path to `api.twitterapi.io`, so it
has never been exercised against a live response. If your first real run
returns something that fails to parse, the error names the exact field or
timestamp that didn't match — that's a quick fix, not a rewrite.

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
Neither X API v2 nor twitterapi.io offer an ascending sort. Paginating
newest-first on a busy token burns the whole quota on late mentions and never
reaches the first call. Instead:

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

## On-chain extras

More signals, computed purely from Solana RPC data — no dependency on
whether the mention search ran, found anything, or has credits left.

- **Token name, from the chain** — read straight off the mint account.
  Token-2022 mints (what pump.fun issues today) carry name, symbol and
  metadata URI in a `tokenMetadata` extension on the mint itself, so the
  name costs **zero extra RPC calls** — it rides along on the account the
  rug check already fetches. Classic SPL tokens keep theirs in a Metaplex
  metadata account at a program-derived address, which costs one more call
  and only when the mint didn't already carry it. The PDA derivation
  (sha256 bump walk plus an ed25519 off-curve check) is implemented in pure
  stdlib, like everything else here. This replaced pump.fun's API as the
  name source: that endpoint has returned HTTP 530 or 0 on *every* call this
  project has ever made to it, and the chain always has the answer.
- **Dev holdings** — how much of this token the dev wallet still holds,
  summed across all of its token accounts for the mint. `holding_known` is
  the load-bearing field: `false` means the lookup did not complete, which
  is reported as "unknown", never as "the dev holds none of it". Those are
  very different claims to make about a dev wallet.
- **Name check** — other Solana tokens sharing *or imitating* this token's
  identity. Reusing a name that already ran is a standard impersonation
  play; so is keeping the ticker while changing the name, and so is
  respelling either one. All of those are the same attack on someone
  scrolling a feed, so all of them are searched:

  | Checked | Example against `Dons` / `$DONS` |
  |---|---|
  | Same name | `Dons` |
  | Same ticker | `Dons Official` / `$DONS` |
  | Name ↔ ticker swap | a token *named* `DONS` |
  | Respelled (case, spacing, punctuation) | `D.O.N.S`, `dons` |
  | Respelled (confusable characters) | `D0ns`, `S0LANA` for `SOLANA` |
  | Doubled letters | `Donss` |
  | One-character lookalike | `Donsy` |

  Both the name **and** the ticker are searched — a copycat usually keeps
  the ticker, since that's the part people type. Every result says *which*
  kind of match it is, because an outright collision and a mere lookalike
  are different claims: only a real collision ("same name", "same ticker",
  or a respelling of either) triggers the "an older token already uses this
  identity" headline, and real collisions sort above lookalikes so a
  resemblance can never push a genuine one off the list. Fuzzy matching is
  disabled below 4 characters — on a 2–3 character ticker nearly everything
  is one edit from everything else, and a check that cries wolf gets ignored
  exactly when it matters.

  There is no on-chain index of token names, so this uses DexScreener's
  public search endpoint (no key). Timestamps are labelled for what they
  actually are — **first DEX pair seen, not mint creation**; a token can
  live on a bonding curve well before it has a pair. If one of the two
  searches fails, the partial list is shown *and says it's partial*; only a
  total failure degrades to `unavailable`.
- **First buyers** — the earliest wallets to receive tokens after genesis.
  A wallet counts as a buyer when its own token balance for the mint
  increases between a transaction's pre- and post-state; the transaction's
  fee payer (a Solana-protocol fact, not a pump.fun-specific assumption) is
  treated as that buyer. Reuses the signature batch already fetched while
  walking to genesis — zero extra `getSignaturesForAddress` calls.
- **Dev wallet fingerprinting** — the genesis transaction's fee payer is the
  dev wallet; its recent history is checked for other pump.fun mints it has
  created. Deliberately bounded (a quick recent-activity check, not a full
  audit) and reported as such — a dev's full lifetime history can run into
  thousands of transactions, and this project has repeatedly had to rein in
  unbounded RPC cost elsewhere (search pacing, retry budgets).
- **Rug signal** — mint/freeze authority status plus holder
  concentration, combined into a verdict (`elevated_risk` /
  `some_risk_signals` / `no_major_red_flags` / `unknown`) with the
  underlying evidence always shown, never a bare yes/no. **Concentration
  counts individual wallets only.** `getTokenLargestAccounts` returns the
  largest *token accounts*, and for a pump.fun token the largest by far is
  the bonding curve itself — it starts holding essentially the entire
  supply and releases it as people buy; after graduation the AMM pool holds
  the same position. Counting those made every token read as 90–100%
  concentrated, which describes the launch mechanism rather than any
  insider. They're identified for free, with no extra RPC call: a user
  wallet is an ed25519 public key and lies *on* the curve, while a
  program-derived address is *off* it by construction, so the curve check
  separates vaults from wallets without hardcoding anyone's program layout.
  Known AMM programs are additionally named. Pooled supply is reported on
  its own row, not hidden. Built entirely from
  standard SPL Token Program fields — not pump.fun-internal contract
  details — so it doesn't carry the "unverified against a live contract"
  risk the liquidity/MC simulator would (see below). **Heuristic, not
  financial advice**: it catches unrevoked mint/freeze authority and heavy
  concentration, not coordinated dumping, social-engineered exits, or
  anything off-chain.
- **Migration tracker** — flags when a top holder account is owned by a
  well-established AMM program (Raydium, PumpSwap), meaning the token likely
  trades on a real pool now rather than only the bonding curve. Best-effort:
  an unrecognized venue reads as "not migrated," not "confirmed still on
  pump.fun."
- **Insider correlation** — flags a mention whose own tweet text contains
  one of the first-buy wallet addresses. This is the only insider signal
  built, deliberately: there is no public, general way to link an X handle
  to a wallet address, and claiming otherwise would fabricate a confidence
  the data doesn't support.

**Not built, on purpose:** a liquidity-depth/market-cap simulator needs
pump.fun's exact bonding-curve account layout and constants, which this
project's environment has no network path to verify and which pump.fun has
changed before — a wrong-but-confident slippage number is worse than none on
a tool people might size real trades around. A pump.fun page-injection
browser extension is a different deliverable (manifest, content script,
Chrome Web Store distribution) from this website, and matching pump.fun's
live DOM isn't verifiable from here either.

**Web vs. CLI defaults** — the dev-wallet scan now runs by default on the
website too, paid for by dropping the pump.fun cross-check, which cost
strictly more (up to a 6s timeout) and gave strictly less now that the token
name comes off the chain for free:

| Variable | Default (web) | Default (CLI flag) |
|---|---|---|
| `FIRSTCALLOOR_NO_FIRST_BUYERS` | off (runs) | `--no-first-buyers` |
| `FIRSTCALLOOR_NO_DEV_SCAN` | off (runs) | `--no-dev-scan` |
| `FIRSTCALLOOR_NO_RISK_CHECK` | off (runs) | `--no-risk-check` |
| `FIRSTCALLOOR_NO_NAME_CHECK` | off (runs) | `--no-name-check` |
| `FIRSTCALLOOR_NO_CROSS_CHECK` | **on (skipped)** | `--no-cross-check` |
| `FIRSTCALLOOR_FIRST_BUYERS_LIMIT` / `_SCAN_CAP` | `5` / `6` | `--first-buyers-limit` / `--first-buyers-scan-cap` (`10` / `20`) |
| `FIRSTCALLOOR_DEV_SCAN_CAP` / `_MAX_LAUNCHES` | `8` / `2` | `--dev-scan-cap` / `--dev-scan-max-launches` (`40` / `8`) |

### The time budget

`HttpBudget` bounds any *single* request. `FIRSTCALLOOR_TIME_BUDGET`
(default `7.5` seconds on the web path, unset/unlimited on the CLI) bounds
the **whole run**, which is a different failure mode: a dozen
individually-fine RPC calls against a slow endpoint still add up past
Vercel's 10s wall, and overrunning that wall isn't a slow page — the
platform kills the process and the browser gets an opaque crash page
instead of anything this code would have said.

It matters most because the on-chain extras run *before* the mention
search, so without a guard the bonus context can eat the budget and the
actual answer — who called it first — never runs at all. Each extra has to
show it can afford itself *and* leave the search its reserve, or it stands
down. Measured against a slow public RPC (~0.6s per `getTransaction`), a
full run costs ~19s; the budget brings that to ~7s by standing down on
whatever doesn't fit.

A stand-down is always reported as **"not checked"**, never as a result.
An unscanned dev wallet reports `scan_ran: false` and renders as "Not
checked" rather than "None found" — a check that didn't run must never be
readable as a clean bill of health. The reserve is only held back when a
mention search is actually going to run; with no credentials configured
it would be protecting a search that never happens.

---

## Options

```
--mock                  synthetic mentions, no key needed
--json PATH             where to write the result set
--provider {auto,x,twitterapi}   mention provider (default: auto-detect)
--twitterapi-key KEY    twitterapi.io key (overrides TWITTERAPI_IO_KEY)
--bearer-token TOKEN    X bearer token (overrides X_BEARER_TOKEN)
--full-archive          use /2/tweets/search/all (X API Pro+; no effect on twitterapi.io)
--max-tweets N          cap on tweets fetched (default 200)
--sig-page-cap N        max signature pages to walk (default 50 = 50k txs)
--include-retweets      rank retweets and quote tweets as mentions
--pre-window-hours N    hours before launch to probe for recycled CAs (0 off)
--no-cross-check        skip the pump.fun cross-check
--no-first-buyers       skip first-buy-wallet detection
--no-dev-scan           skip the dev wallet's other-launches check
--no-risk-check         skip the mint/freeze authority + holder-concentration signal
--no-name-check         skip the search for other tokens using the same name
--time-budget SECONDS   wall-clock cap for the whole run; optional extras stand
                        down (and say so) rather than overrun it. Unset =
                        unlimited, right for a terminal, never for serverless
--first-buyers-limit N  max first-buyer wallets to report (default 10)
--first-buyers-scan-cap N   post-genesis txs to check for buys (default 20)
--dev-scan-cap N        dev wallet's recent txs to check (default 40)
--dev-scan-max-launches N   stop dev scan after finding this many (default 8)
--rpc URL               Solana RPC endpoint
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

187 tests. X, twitterapi.io, DexScreener, and pump.fun are all stubbed at the HTTP layer,
so the search strategy — window expansion, cap contraction, rate-limit
cut-off, the 7-day clamp (and its absence on twitterapi.io) — is verified
without network access or an API key. The on-chain extras (first buyers, dev
fingerprinting, rug signal, migration, insider correlation) are covered
against a scripted RPC stub and separately verified live against real
pump.fun mints on Solana mainnet. This build environment has no network path
to `api.twitterapi.io` itself, so that provider's stub is the only
verification it has had; see the note under provider setup above.

---

## Scope (v1)

**In:** CA → on-chain creation timestamp → X search → filtered ranked
timeline → first buyers → dev fingerprinting → rug signal → migration status
→ insider correlation → console + JSON.

**Out:** Telegram scanning, follower-weighted "market mover" scoring, a
liquidity-depth/MC simulator (needs unverifiable pump.fun-internal
constants — see "On-chain extras" above), a pump.fun page-injection browser
extension (a different deliverable entirely), persistence. No database —
every request is computed fresh, so nothing here builds a caller-reputation
history across tokens yet.
