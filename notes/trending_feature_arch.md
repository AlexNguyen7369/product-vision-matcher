# Trending Items — Feature Architecture

> **Status:** Implemented on the modern **eBay Browse API** (OAuth client-credentials).
> Offline unit tests pass (**127 passed, 0 failed**). Online integration testing
> with real `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` and live Redis not yet done.
>
> **Scope:** Development feature. eBay only for the first cut; built behind a
> `TrendingProvider` protocol so other marketplaces drop in later.

This document is the implementation spec for the **Trending Items** feature: a new
tab in the existing Flask UI that surfaces the top 10 trending eBay items, ranked
by a weighted blend of three signals.

It is organised into two parts:

- **[Version 2 — Current Implementation](#version-2--current-implementation-ebay-browse-api)** — the live spec; everything here matches the code.
- **[Version 1 — Legacy Implementation](#version-1--legacy-implementation-deprecated)** — the original design, kept for history, plus the v1→v2 migration map.

---

## Version History

| Version | Status | Data source | Auth | Signals (keyword / engagement / demand) |
| ------- | ------ | ----------- | ---- | --------------------------------------- |
| **v2** | ✅ **Current** | **Browse API** (`buy/browse/v1`) | OAuth 2.0 client-credentials (`EBAY_CLIENT_ID` + `EBAY_CLIENT_SECRET`) | Best Match rank / **sold volume** / **sell-through** |
| **v1** | ⛔ Deprecated | Merchandising + Finding APIs (`svcs.ebay.com`) | App ID query key (`EBAY_APP_ID`) | list rank / **watch count** / **sold-completed rate** |

**Why v2 exists:** eBay **retired** the Merchandising (`getMostWatchedItems`) and
Finding (`findCompletedItems`) APIs that v1 depended on, and the App-ID query-key
auth they used. The credentials issued to developers today are **OAuth keys for the
REST APIs**. v2 moves the feature onto the current **Browse API**, reachable with
those OAuth credentials. Because Browse exposes **no watch counts and no
completed-sales data**, the *watch* signal was redefined as **sold volume**
(`estimatedSoldQuantity`) and the *sold-rate* signal as **sell-through**
(`sold / (sold + available)`). The cache schema key was bumped **`v1` → `v2`**
accordingly. Full mapping in **[Version 1 § Migration Map](#l4-migration-map-v1--v2)**.

The **module boundaries, scoring math, and Redis caching strategy are unchanged
between versions** — only the network layer (`trending_fetcher.py`) and the signal
shapes were reworked.

---
---

# Version 2 — Current Implementation (eBay Browse API)

## 1. Feature Summary

A **Trending** tab in the Product Vision Matcher UI shows the **top 10 trending
eBay items**, ranked by a weighted score built from three independent signals:

| Signal              | Source (eBay Browse API)                                   | Weight |
| ------------------- | ---------------------------------------------------------- | ------ |
| Keyword search rank | `item_summary/search` — Best Match position per seed query | **2×** |
| Sold volume         | `getItem` — `estimatedSoldQuantity` (units sold)           | **2×** |
| Sell-through rate   | `getItem` — sold / (sold + `estimatedAvailableQuantity`)   | **1×** |

Each signal is **min-max normalized independently** to `[0, 1]`, then combined:

```
score = (2 × norm_keyword) + (2 × norm_volume) + (1 × norm_sold)
```

Results are **cached in Redis** with a **3-hour key TTL** (plus a warm-refresh
buffer, §9) so the UI does not hammer the eBay API on every page load. The whole
fetch/score path sits behind a `TrendingProvider` protocol (mirroring the existing
`ReverseSearchProvider` pattern), so Amazon and other marketplaces can be added
later without touching the scorer, cache, or Flask layers.

**Design principles inherited from the codebase:**

- Shared dataclasses and protocols live in `models.py`; modules import types from
  there, never from each other.
- The orchestration layer depends on the **protocol**, not the concrete eBay
  implementation, so backends are swappable.
- Credentials and HTTP clients are **injected via the constructor** — no
  import-time global state — so every module is unit-testable offline.

---

## 2. Prerequisites

### 2.1 Environment variables (add to `.env`)

```
EBAY_CLIENT_ID=<your_ebay_client_id>
EBAY_CLIENT_SECRET=<your_ebay_client_secret>
REDIS_URL=redis://localhost:6379/0     # local dev; use redis://redis:6379/0 inside Docker Compose
```

**EBAY_CLIENT_ID / EBAY_CLIENT_SECRET** — from [developer.ebay.com](https://developer.ebay.com/):

- Register/login and create an app keyset to obtain a **Client ID** and **Client
  Secret** (production keyset).
- These two are exchanged for an **application access token** via the OAuth 2.0
  *client-credentials* grant (scope `https://api.ebay.com/oauth/api_scope`).
- **`EBAY_RUNAME` is NOT required** — that is only for user-consent
  (authorization-code) flows. Public Browse search uses an application token.
- The **Browse API** is available on a standard developer account (default 5,000
  calls/day).

**REDIS_URL** — requires a running Redis instance:

- Local dev (Mac): `brew install redis && brew services start redis`
- Docker Compose: the compose file handles it; set `REDIS_URL=redis://redis:6379/0`

> All three values are secret/config and live in `.env`, which is already
> gitignored and `.dockerignore`d (see `dockerfile_plan.md` §2) — never committed,
> never baked into an image layer. They are read at runtime exactly like
> `SERPAPI_KEY`. Your existing `SERPAPI_KEY` is unaffected — Trending is
> independent of it.

### 2.2 Python dependencies (already in `requirements.txt`)

| Package | Purpose |
| ------- | ------- |
| `httpx` | HTTP client for `trending_fetcher.py` (OAuth + Browse calls) |
| `redis` | Python client used by `trending_cache.py` |
| `fakeredis` | In-process Redis for offline tests in `test_setup.py` |

No new dependencies were needed for v2 — OAuth is done with `httpx` + `base64`.

---

## 3. Data Flow

```
GET /trending  (Flask route in server.py)
        │
        ▼
trending_cache.load()
   ├── cache HIT (TTL > 0) ──► return cached list[TrendingItem]
   │                           └─ if TTL < 15 min: kick a warm refresh (§9.2), still serve now
   └── cache MISS / EXPIRED ─► fetch path below (guarded by a single-flight lock, §9.4)
        │
        ▼
trending_fetcher.EbayTrendingProvider          (implements TrendingProvider)
   1. _get_token()              ─► POST  identity/v1/oauth2/token      (token cached ~2h)
   2. fetch_keyword_signals(60) ─► GET   browse/v1/item_summary/search?q=<seed>
                                     · iterate DEFAULT_SEED_QUERIES
                                     · Best Match position = rank
                                     · dedupe across seeds → keep best (lowest) rank
                                     · KeywordSignal carries title + itemWebUrl
   3. fetch_volume_signals(ids) ─► GET   browse/v1/item/{id}  → estimatedSoldQuantity
   4. fetch_sold_signals(ids)   ─► GET   browse/v1/item/{id}  → sold / (sold + available)
                                     · steps 3 & 4 SHARE one memoized getItem call per id
        │  raw signal lists: list[KeywordSignal], list[VolumeSignal], list[SoldSignal]
        ▼
trending_scorer.score_trending()
   · join by item_id · predicate filter (§8) · min-max normalize each signal
   · score = 2·norm_keyword + 2·norm_volume + 1·norm_sold · sort desc · take top 10
        │  list[TrendingItem]
        ▼
trending_cache.save()
   · SET trending:ebay:v2:ranked = json(items)        EX 10800 (3h)
   · SET trending:ebay:v2:raw    = json(raw signals)  EX 10800
        │
        ▼
index.html — Trending tab renders the ranked table:
   #  ·  Title→url  ·  Source  ·  Score  ·  Sold  ·  Sell-through
```

Note the symmetry with the existing image pipeline: `trending_fetcher` is the
network boundary (like `reverse_search`), `trending_scorer` is the filter+rank
stage (like `marketplace_parser` + `price_aggregator`), and `trending_cache` is the
persistence concern unique to this feature — backed by **Redis** (keyed, TTL'd
values) rather than a flat JSON file on disk.

---

## 4. eBay Browse API Endpoints

All three signals come from the modern **Browse API** (`buy/browse/v1`). Auth is an
OAuth 2.0 **application access token** minted from `EBAY_CLIENT_ID` +
`EBAY_CLIENT_SECRET` via the client-credentials grant; the token is cached on the
provider instance and sent as `Authorization: Bearer <token>` together with
`X-EBAY-C-MARKETPLACE-ID: EBAY_US`.

> The `lookback_days` parameter is **60**, but the Browse API returns active
> listings and a point-in-time `estimatedSoldQuantity` — it exposes **no per-sale
> date window**. So the 60-day window is a soft, client-side concept only; the
> recency gate (§8) falls back to `fetched_at`, which is always current for live
> listings.

### 4.0 OAuth token — client-credentials grant

|                |                                                                                                              |
| -------------- | ------------------------------------------------------------------------------------------------------------ |
| **Endpoint**   | `POST https://api.ebay.com/identity/v1/oauth2/token`                                                         |
| **Headers**    | `Content-Type: application/x-www-form-urlencoded`, `Authorization: Basic base64(CLIENT_ID:CLIENT_SECRET)`    |
| **Body**       | `grant_type=client_credentials&scope=https://api.ebay.com/oauth/api_scope`                                   |
| **Returns**    | `{ "access_token": "...", "expires_in": 7200 }` — application token, valid ~2h, cached per provider instance |

### 4.1 Keyword/rank signal — `item_summary/search` (Best Match)

|                |                                                                                                              |
| -------------- | ------------------------------------------------------------------------------------------------------------ |
| **Endpoint**   | `GET https://api.ebay.com/buy/browse/v1/item_summary/search`                                                 |
| **Key params** | `q=<seed query>`, `limit=<max_results>` — **no `sort`**, so eBay's default **Best Match** (relevance/popularity) ordering applies |
| **Returns**    | `itemSummaries[]`, each with `itemId`, `title`, `itemWebUrl`. The item's **position** within a seed query is its rank (1 = top). Items appearing under multiple seeds keep their best (lowest) rank; the scorer inverts rank (§7). |

> The provider iterates a curated list of **seed queries**
> (`DEFAULT_SEED_QUERIES` in `trending_fetcher.py`: *electronics, sneakers, trading
> cards, video games, watches, collectibles, home, toys*), because Browse search
> requires a query string. `KeywordSignal` now also carries `title` and `url` so
> the ranked output links straight to the live listing.

### 4.2 Sold-volume signal — `getItem` `estimatedSoldQuantity`

|                |                                                                                                              |
| -------------- | ------------------------------------------------------------------------------------------------------------ |
| **Endpoint**   | `GET https://api.ebay.com/buy/browse/v1/item/{item_id}` (item_id **URL-encoded**, e.g. `v1%7C123%7C0`)       |
| **Returns**    | `estimatedAvailabilities[0].estimatedSoldQuantity` — units sold for the listing. Raw volume signal (replaces the retired watch count). No special access required. |

### 4.3 Sell-through signal — `getItem` sold / (sold + available)

|                |                                                                                                              |
| -------------- | ------------------------------------------------------------------------------------------------------------ |
| **Endpoint**   | Same `getItem` call as §4.2 — the response is **memoized per item id**, so it is fetched once even though two signals consume it. |
| **Returns**    | `sold_rate = estimatedSoldQuantity / (estimatedSoldQuantity + estimatedAvailableQuantity)`, in `[0,1]`. `SoldSignal.last_sold` is **always `None`** (Browse exposes no per-sale timestamps). |

**Querying order:** `fetch_keyword_signals` runs the seed searches first to obtain
the candidate `item_ids` (plus titles/urls). Those `item_ids` are passed into
`fetch_volume_signals` and `fetch_sold_signals`, which **share one memoized
`getItem` call per id**, so all three signals are keyed to the same candidate set.

---

## 5. Data Model

All trending dataclasses live in **`src/models.py`** alongside `ProcessedImage`,
`ParsedListing`, and `PriceReport`. They use the same `@dataclass` +
`from __future__ import annotations` style as the rest of the file.

```python
# ── Trending feature signals ──────────────────────────────────────────────────

@dataclass
class KeywordSignal:
    item_id:    str       # eBay itemId (or "" for pure-keyword rows)
    keyword:    str       # the seed search term that surfaced this item
    rank:       int       # Best Match position within its seed query; 1 = top, lower is stronger
    fetched_at: datetime  # when this signal was pulled (UTC)
    title:      str = ""  # item display title from the search result
    url:        str = ""  # itemWebUrl from the search result


@dataclass
class VolumeSignal:
    item_id:       str       # eBay itemId
    title:         str       # item display title
    sold_quantity: int       # estimatedSoldQuantity from Browse getItem (units sold)
    fetched_at:    datetime  # UTC


@dataclass
class SoldSignal:
    item_id:     str             # eBay itemId
    title:       str             # item display title
    sold_count:  int             # estimatedSoldQuantity (units sold)
    total_count: int             # estimatedSoldQuantity + estimatedAvailableQuantity
    sold_rate:   float           # sell-through = sold_count / total_count, [0,1]; 0.0 if total_count == 0
    last_sold:   datetime | None # always None on the Browse backend (no per-sale timestamps)
    fetched_at:  datetime        # UTC


# ── Final ranked output row ───────────────────────────────────────────────────

@dataclass
class TrendingItem:
    item_id:       str          # eBay itemId — primary key joining all three signals
    title:         str          # display title
    url:           str          # https:// link to the eBay listing
    source:        str          # marketplace name, e.g. "eBay"
    rank:          int          # final position in the trending list, 1 (top) – 10
    score:         float        # final weighted score (sum of weighted norms), range [0, 5]
    # raw signal values, carried through for display / debugging:
    keyword_rank:  int | None   # None when the keyword signal was missing
    sold_quantity: int | None   # units sold (estimatedSoldQuantity); None when missing
    sold_rate:     float | None # sell-through; None when the sold signal was missing
    # normalized [0,1] components, for transparency in the UI / tests:
    norm_keyword:  float        # 0.0 when signal missing (graceful degradation)
    norm_volume:   float        # normalized sold_quantity
    norm_sold:     float        # normalized sell-through
```

### `TrendingProvider` protocol (also in `models.py`)

```python
class TrendingProvider(Protocol):
    """Contract for any trending-items backend (eBay first, others later).

    Each method fetches one raw signal over a lookback window. The scorer consumes
    the three signal lists; it never depends on a concrete provider. Mirrors the
    ReverseSearchProvider pattern: orchestration depends on this protocol, not on
    EbayTrendingProvider.
    """

    def fetch_keyword_signals(self, lookback_days: int) -> list[KeywordSignal]: ...

    def fetch_volume_signals(
        self, item_ids: list[str], lookback_days: int
    ) -> list[VolumeSignal]: ...

    def fetch_sold_signals(
        self, item_ids: list[str], lookback_days: int
    ) -> list[SoldSignal]: ...
```

---

## 6. Module Breakdown

### 6.1 `src/trending_fetcher.py` — eBay `TrendingProvider` implementation

**Responsibility:** the network boundary for trending data. Mints the OAuth token,
calls the Browse endpoints, and maps raw JSON into `KeywordSignal` / `VolumeSignal`
/ `SoldSignal`. No filtering, no scoring, no normalization — that belongs to the
scorer (same separation as `reverse_search` vs `marketplace_parser`).

**Public interface:**

```python
# module constants
_OAUTH_URL    = "https://api.ebay.com/identity/v1/oauth2/token"
_BROWSE_BASE  = "https://api.ebay.com/buy/browse/v1"
_SCOPE        = "https://api.ebay.com/oauth/api_scope"
_MARKETPLACE  = "EBAY_US"
DEFAULT_SEED_QUERIES = ["electronics", "sneakers", "trading cards", "video games",
                        "watches", "collectibles", "home", "toys"]

class EbayTrendingProvider:
    """Backed by the eBay Browse API; auth via OAuth client-credentials."""

    def __init__(
        self,
        client_id: str | None = None,       # None -> EBAY_CLIENT_ID env var
        client_secret: str | None = None,   # None -> EBAY_CLIENT_SECRET env var
        client: httpx.Client | None = None,
        timeout: float = 30.0,
        max_results: int = 10,              # results pulled per seed query
        seed_queries: list[str] | None = None,
        marketplace: str = _MARKETPLACE,
    ) -> None: ...

    def fetch_keyword_signals(self, lookback_days: int) -> list[KeywordSignal]: ...
    def fetch_volume_signals(self, item_ids: list[str], lookback_days: int) -> list[VolumeSignal]: ...
    def fetch_sold_signals(self, item_ids: list[str], lookback_days: int) -> list[SoldSignal]: ...
```

**Conventions (mirroring the existing `SerpApiSearcher`):**

- `client_id` / `client_secret` `=None` means "fall back to the environment"; an
  explicit `""` is honored as an (invalid) key so `_validate_key()` can be tested.
- `_validate_key()` requires **both** id and secret, else raises
  `EnvironmentError("EBAY_CLIENT_ID / EBAY_CLIENT_SECRET not set in .env")`.
- `_get_token()` mints the application token once and caches it on the instance.
- HTTP non-200 raises `RuntimeError(f"eBay {status}: {body[:300]}")`.
- `getItem` responses are memoized per item id (`_item_detail`); a `getItem`
  **404/error skips that candidate** (returns `None`) instead of failing the fetch.
- Item ids are URL-encoded before being placed in the `getItem` path.
- If an `httpx.Client` is injected it is reused; otherwise a short-lived
  `httpx.Client(timeout=self._timeout)` is opened per request.

### 6.2 `src/trending_scorer.py` — normalization + weighted scoring

**Responsibility:** pure, network-free transformation. Takes the three raw signal
lists, applies predicate filters, min-max normalizes each signal, computes the
weighted score, sorts, and returns the top 10 `TrendingItem` rows. The analog of
`price_aggregator.rank_by_price` — deterministic and trivially testable.

**Public interface:**

```python
# weights are module-level constants so tests and docs reference one source
KEYWORD_WEIGHT = 2.0
VOLUME_WEIGHT  = 2.0
SOLD_WEIGHT    = 1.0
TOP_N          = 10

def score_trending(
    keyword_signals: list[KeywordSignal],
    volume_signals:  list[VolumeSignal],
    sold_signals:    list[SoldSignal],
    top_n: int = TOP_N,
) -> list[TrendingItem]:
    """Filter, normalize, weight, rank → top-N TrendingItem list (rank 1..N)."""

# internal helpers (private):
def _min_max(values: list) -> list[float]: ...   # per-signal [0,1] normalization
def _passes_predicate(sold_quantity, sold_rate, keyword_rank,
                      last_sold, fetched_at, now, lookback_days=60) -> bool: ...
```

### 6.3 `src/trending_cache.py` — Redis-backed cache read/write

**Responsibility:** persist and retrieve the computed trending list in **Redis**
with a 3-hour key TTL. Owns the only knowledge of the Redis key layout, the JSON
encoding, the warm-refresh buffer, and the concurrency lock. No eBay or scoring
logic lives here.

**Why Redis (not a flat JSON file):** native key expiry (`SET ... EX 10800`); a
cache shared across all gunicorn workers; atomic `SET NX EX` for the single-flight
lock — no temp-file/`os.replace` dance.

**Public interface:**

```python
REDIS_URL             = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
SCHEMA_VER            = "v2"            # bumped from v1 on the Browse migration
TTL_SECONDS           = 3 * 60 * 60    # 3 hours
REFRESH_FLOOR_SECONDS = 15 * 60        # warm-refresh when remaining TTL < 15 min
LOCK_TTL_SECONDS      = 60             # lock self-expires so a crash can't wedge it

def _key(part, marketplace="ebay") -> str:        # -> "trending:ebay:v2:<part>"
def load(client, marketplace="ebay") -> tuple[list[TrendingItem] | None, int]
def save(client, items, keyword_signals, volume_signals, sold_signals, marketplace="ebay") -> None
def acquire_lock(client, marketplace="ebay") -> bool
def release_lock(client, marketplace="ebay") -> None
```

### 6.4 Orchestration helper — `get_trending()` (in `trending_scorer.py`)

A thin orchestrator wires the three layers together; the Flask route calls it. It
depends on the **`TrendingProvider` protocol**, not on `EbayTrendingProvider`:

```python
def get_trending(provider, client, lookback_days=60) -> list[TrendingItem]:
    items, ttl = trending_cache.load(client)
    if items is not None:                         # cache HIT
        if ttl < trending_cache.REFRESH_FLOOR_SECONDS:
            _maybe_refresh(provider, client, lookback_days)   # warm refresh, single-flighted
        return items                              # serve warm data immediately, never blocks
    return _fetch_and_cache(provider, client, lookback_days)  # cache MISS

def _fetch_and_cache(provider, client, lookback_days):
    if not trending_cache.acquire_lock(client):   # lost the single-flight race
        items, _ = trending_cache.load(client)    # another worker may have just filled it
        if items is not None:
            return items
    try:
        kw  = provider.fetch_keyword_signals(lookback_days)
        ids = [k.item_id for k in kw if k.item_id]
        v   = provider.fetch_volume_signals(ids, lookback_days)
        s   = provider.fetch_sold_signals(ids, lookback_days)
        items = score_trending(kw, v, s)
        trending_cache.save(client, items, kw, v, s)
        return items
    finally:
        trending_cache.release_lock(client)       # LOCK_TTL is the backstop if missed
```

`_maybe_refresh()` is the same fetch+score+save body, guarded by the lock and run
inline; it refreshes the cache without blocking the request that triggered it.

---

## 7. Scoring Algorithm

Given the three raw signal lists, produce the top 10 ranked `TrendingItem`s.

**Step 0 — Join signals by `item_id`.** Build a candidate set keyed by `item_id`.
Each candidate may have any subset of the three signals (graceful degradation, §8).
`title`/`url` are lifted off whichever signal carries them (`KeywordSignal` for url).

**Step 1 — Apply predicate filters (§8).** Drop noise candidates _before_
normalization so the min/max bounds reflect only surviving candidates.

**Step 2 — Derive raw signal values.**

- `keyword_raw` — invert rank so higher = more trending:
  `keyword_raw = (max_rank + 1) - rank`, where `max_rank` is the largest rank among
  survivors (a #1-ranked candidate gets the highest raw value). Missing → `None`.
- `volume_raw = sold_quantity` (missing → `None`).
- `sold_raw = sold_rate` in `[0,1]` (missing → `None`).

**Step 3 — Min-max normalize each signal independently to `[0, 1]`.**

```
norm(v) = (v - min) / (max - min)     if max > min
norm(v) = 1.0                          if max == min  (all equal, all present)
```

- A **missing** signal normalizes to **`0.0`** (graceful degradation — a candidate
  is never penalized below zero; present signals still drive its rank).
- If *all* candidates miss a signal, it contributes `0.0` for everyone and drops
  out of the ranking.

**Step 4 — Weighted sum.**

```
score = (KEYWORD_WEIGHT × norm_keyword) + (VOLUME_WEIGHT × norm_volume) + (SOLD_WEIGHT × norm_sold)
      = (2 × norm_keyword) + (2 × norm_volume) + (1 × norm_sold)
```

Score range is `[0, 5]` (max when a candidate tops every signal: `2 + 2 + 1`).

**Step 5 — Sort and slice.** Sort by `score` descending; ties broken by
`sold_quantity` descending, then `item_id` ascending for determinism. Assign
`rank = 1..N` and return the first `TOP_N` (10) as `TrendingItem`s, populating both
the raw values and the `norm_*` components for UI transparency.

**Worked micro-example (2 candidates, all signals present):**

```
Candidate A: keyword rank 1, sold_quantity 500, sell-through 0.40
Candidate B: keyword rank 2, sold_quantity 100, sell-through 0.10

keyword_raw:  A=(2+1)-1=2,  B=(2+1)-2=1       → norm: A=1.0, B=0.0
volume_raw:   A=500, B=100   (min100,max500)  → norm: A=1.0, B=0.0
sold_raw:     A=0.40, B=0.10 (min.10,max.40)  → norm: A=1.0, B=0.0

score(A) = 2*1.0 + 2*1.0 + 1*1.0 = 5.0   → rank 1
score(B) = 2*0.0 + 2*0.0 + 1*0.0 = 0.0   → rank 2
```

---

## 8. Predicate Filters

Applied in `trending_scorer._passes_predicate()` **before** normalization. Signature:
`_passes_predicate(sold_quantity, sold_rate, keyword_rank, last_sold, fetched_at, now, lookback_days=60)`.
A candidate must pass **all** gates to be scored:

1. **Noise gate.** Drop a candidate only if it has **no positive signal at all** —
   `sold_quantity == 0` **AND** `sold_rate == 0.0` **AND** `keyword_rank is None`.
   > **v2 change:** the gate now also keeps an item that was **surfaced by Best
   > Match search** (has a `keyword_rank`). Because Browse's `estimatedSoldQuantity`
   > is often absent/zero, appearing in the top search results is itself treated as
   > a trending signal — otherwise the list could collapse to empty on items that
   > lack sold data.
2. **Recency gate.** Activity must be within `lookback_days` (60). The most-recent
   activity timestamp is `last_sold or fetched_at`. On the Browse backend
   `last_sold` is always `None`, so this resolves to `fetched_at` (current for live
   listings) and effectively always passes.
3. **Data-presence gate (graceful degradation).** Keep the item only if it has data
   for **at least one** of the three signals (`sold_quantity`, `sold_rate`, or
   `keyword_rank`). Missing one or two is fine — those normalize to `0.0` (§7).

> Order matters: filtering precedes normalization so the min/max bounds reflect
> only real, surviving candidates — otherwise a single noise row could skew the
> normalization range.

---

## 9. Caching Strategy (Redis)

**Why:** eBay APIs are rate-limited; trending data changes slowly. A 3-hour cache
keeps the UI snappy and well within rate limits. Redis gives native key expiry, a
cache shared across all gunicorn workers, and atomic primitives for single-flight
fetching.

**Backing store:** Redis via `REDIS_URL` (§2). Persistence (RDB/AOF) is configured
on the Redis container so the cache reads back **warm on startup** — see
`dockerfile_plan.md` §4.2.

### 9.1 Keys, values, and the 3-hour TTL

```
trending:<marketplace>:<schema_version>:<part>

trending:ebay:v2:ranked    # JSON-encoded list[TrendingItem] served to the UI
trending:ebay:v2:raw       # JSON of the three raw signal lists (offline re-scoring / fixtures)
trending:ebay:v2:lock      # single-flight fetch lock (§9.4)
```

Both data keys are written with **`SET ... EX 10800`** (3h). On `GET /trending`:

- key present (TTL > 0) → **cache hit**, return it, no eBay calls;
- key absent (TTL == -2) → **cache miss**, fetch + score + `SET EX` + return.

### 9.2 Warm-refresh buffer (refresh when TTL < 15 min)

`load()` returns the key's **remaining TTL** alongside the value. On a cache hit, if
`remaining_ttl < REFRESH_FLOOR_SECONDS` (15 min), the orchestrator single-flights a
refresh (re-fetch, re-score, `SET EX 10800`) **while still serving the existing warm
data immediately**. So under steady traffic users never hit a cold cache. The
refresh is guarded by the lock (§9.4) so only one worker re-fetches.

### 9.3 Schema versioning via the key (the `v2` segment)

The schema version is **baked into the key name** (`trending:ebay:`**`v2`**`:...`),
not stored inside the value. If the dataclass model changes — a field
added/removed/renamed in `TrendingItem` or any signal — **bump
`trending_cache.SCHEMA_VER`**. Effect: new writes go to the new prefix; old keys are
never read again and expire on their own within 3 hours. No migration code, no
"corrupt cache" branch — a model change is a one-line key rename.

> **This is exactly what the Browse migration did:** the signal rename
> (`WatchSignal`→`VolumeSignal`, `watch_count`→`sold_quantity`, `norm_watch`→
> `norm_volume`) changed the model, so `SCHEMA_VER` went **`v1` → `v2`** and any
> stale `v1` keys simply age out.

### 9.4 Single-flight lock (with its own TTL)

On a cache miss (or warm refresh), many concurrent requests could each fire a full
fetch — a thundering herd. A Redis lock single-flights it:

```
acquire:  SET trending:ebay:v2:lock <token> NX EX 60   # NX: only the first caller wins
release:  DEL trending:ebay:v2:lock                    # best-effort, in a finally:
```

The lock has its **own TTL** so a missed unlock can't wedge the feature: if the
holder crashes before the `DEL`, the `EX 60` evaporates the lock after at most 60s.
The explicit `DEL` is the fast path; the TTL is the backstop. Losers of the lock
re-check the cache (another worker may have just populated it) and serve that; only
if it's still empty do they compute once locally for that request.

### 9.5 In-memory fallback note (multi-worker, Redis-down)

> **Known limitation — documented behavior, not a feature.** Under gunicorn each
> worker is a separate process with its **own memory**. A process-local
> last-known-good copy is **per-worker and not shared**: a freshly started/restarted
> worker has no warm copy and must do a live fetch or surface an error, so responses
> can be inconsistent across workers during a Redis outage. Acceptable for the
> dev/single-host scope (Redis is the source of truth and is normally up; its
> RDB/AOF snapshot survives restarts). Noted so it isn't mistaken for a bug.

### 9.6 Cache invalidation

- **Time-based** (primary): the 3-hour TTL (§9.1), refreshed early by the warm
  buffer (§9.2).
- **Manual:** `DEL trending:ebay:v2:ranked trending:ebay:v2:raw` (or `FLUSHDB` in
  dev) forces a refetch on the next request.
- **Schema bump:** change `SCHEMA_VER` (§9.3) — old keys age out on their own.

**Atomicity:** each `SET ... EX` is a single atomic Redis op — value and TTL land
together — so there is no partially-written/corrupt state for `load()` to choke on.

---

## 10. Flask + UI Integration

### 10.1 `GET /trending` route in `server.py`

Sits alongside `/` and `/analyze`, using the same `sys.path.insert(0, .../src)` +
lazy-import pattern and the same `try/except → jsonify({"error": ...}), 500` shape.

```python
@app.route("/trending", methods=["GET"])
def trending():
    try:
        import sys
        sys.path.insert(0, os.path.join(BASE_DIR, "src"))
        import redis, trending_scorer
        from trending_fetcher import EbayTrendingProvider

        client = redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
        items = trending_scorer.get_trending(
            EbayTrendingProvider(), client, lookback_days=60
        )
        return jsonify({
            "marketplace":   "eBay",
            "lookback_days": 60,
            "items": [
                {
                    "rank":          it.rank,
                    "title":         it.title,
                    "url":           it.url,
                    "source":        it.source,
                    "score":         round(it.score, 3),
                    "keyword_rank":  it.keyword_rank,
                    "sold_quantity": it.sold_quantity,
                    "sold_rate":     it.sold_rate,
                    "norm_keyword":  round(it.norm_keyword, 3),
                    "norm_volume":   round(it.norm_volume, 3),
                    "norm_sold":     round(it.norm_sold, 3),
                }
                for it in items
            ],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
```

`EbayTrendingProvider()` is constructed with no args, so it resolves
`EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` from the environment.

### 10.2 Trending tab in `index.html`

A tab strip at the top of `.content` toggles between **Matcher** (the existing
image-search view, default) and **Trending**. The Trending panel reuses the
existing `table` / `thead` / `tbody` styles. Columns:

| #   | Title | Source | Score | Sold | Sell-through |
| --- | ----- | ------ | ----- | ---- | ------------ |

- `#` = `rank`; **Title** links to `url` (`a.listing-link`, `target="_blank"`);
  **Source** uses the `.source-tag` chip; **Score** uses the `.score-bar` component
  (width = `score / 5`); **Sold** = `sold_quantity` (`—` when null);
  **Sell-through** = `sold_rate` rendered as a percentage (`—` when null).
- **Data load:** on first switch to the tab, `fetch('/trending')`, render into
  `#trendingBody`, and drive the `.status-bar` dot for loading/success/error. The
  response is cached client-side so re-clicking the tab doesn't refetch (the
  server-side 3-hour cache already covers staleness).

No new HTTP libraries or build step — same vanilla-JS `fetch` approach as `/analyze`.

---

## 11. Extensibility (Multi-marketplace)

eBay-first, marketplace-agnostic above the fetcher layer. **The contract** is the
`TrendingProvider` protocol (§5); `trending_scorer`, `trending_cache`, and the Flask
route depend **only** on it and the signal dataclasses — never on
`EbayTrendingProvider` (same inversion as `ReverseSearchProvider` / `SerpApiSearcher`).

**To add a marketplace (e.g. Amazon):**

1. Create `src/trending_fetcher_amazon.py` with an `AmazonTrendingProvider`
   implementing the three protocol methods, mapping its API into the same
   `KeywordSignal` / `VolumeSignal` / `SoldSignal` dataclasses.
2. Set `source="Amazon"` on items it produces (the scorer's `source="eBay"` would
   be parameterized per provider).
3. Inject it into `get_trending(provider=...)` — no change to scorer, cache, or HTML.
4. (Optional) Combine providers by merging signal lists before scoring, or keep a
   per-marketplace cache key (`trending:amazon:v2:*`) + a UI selector.

The scorer's min-max normalization is per-signal and source-agnostic, so a merged
candidate set scores correctly — though cross-marketplace volumes may need
per-source normalization if scales differ (a known future consideration, out of
scope for the eBay-only first cut).

---
---

# Version 1 — Legacy Implementation (Deprecated)

> ⛔ **Do not build on this section.** It documents the original implementation,
> which used eBay APIs that have since been **retired**. It is retained for history
> and to explain the migration. The live spec is **Version 2** above.

## L1. Why v1 was retired

v1 sourced its signals from two legacy eBay APIs on `svcs.ebay.com`, authenticated
with an **App ID** passed as a query parameter (`EBAY_APP_ID`):

- **Merchandising API** `getMostWatchedItems` — provided the keyword rank (list
  position) and the **watch count** per item.
- **Finding API** `findCompletedItems` — provided completed/sold listings, from
  which a **sold-completed rate** (`sold / total_completed`) was computed.

Both were **deprecated and shut down** by eBay. `findCompletedItems` (sold-completed
data) was access-restricted then removed for general apps; the Merchandising and
Finding services were sunset. Equally important, the **App-ID query-key auth** they
used is no longer how modern eBay APIs authenticate — current keysets are **OAuth
credentials** (Client ID / Client Secret / optional RuName). There was therefore no
"fix the key" path; the feature had to move to a current API (Browse → Version 2).

## L2. Legacy signal sources

| Signal | Legacy endpoint | Legacy auth param | Raw field |
| ------ | --------------- | ----------------- | --------- |
| Keyword rank | `https://svcs.ebay.com/MerchandisingService` `getMostWatchedItems` | `CONSUMER-ID=<EBAY_APP_ID>` | list position |
| Watch count | `https://svcs.ebay.com/MerchandisingService` `getMostWatchedItems` | `CONSUMER-ID=<EBAY_APP_ID>` | `watchCount` |
| Sold rate | `https://svcs.ebay.com/services/search/FindingService/v1` `findCompletedItems` | `SECURITY-APPNAME=<EBAY_APP_ID>` | `sellingStatus.sellingState == "EndedWithSales"` over the window |

## L3. Legacy data model

The v1 dataclasses differed from v2 only in the engagement axis and field names:

```python
@dataclass
class WatchSignal:          # → became VolumeSignal in v2
    item_id:     str
    title:       str
    watch_count: int        # → became sold_quantity
    fetched_at:  datetime

# TrendingItem (v1) had: watch_count, norm_watch   (→ sold_quantity, norm_volume)
# TrendingProvider (v1) had: fetch_watch_signals   (→ fetch_volume_signals)
# trending_scorer (v1) had: WATCH_WEIGHT           (→ VOLUME_WEIGHT)
# trending_cache (v1) used: SCHEMA_VER = "v1"       (→ "v2")
```

The v1 noise gate dropped an item when `watch_count == 0 AND sold_rate == 0` (it had
no keyword-rank escape hatch — see §8 for why v2 relaxed it).

## L4. Migration map (v1 → v2)

| Concern | v1 (deprecated) | v2 (current) |
| ------- | --------------- | ------------ |
| **Auth** | App ID as query key (`CONSUMER-ID` / `SECURITY-APPNAME`), no token | OAuth client-credentials → application Bearer token, cached |
| **Env vars** | `EBAY_APP_ID` | `EBAY_CLIENT_ID` + `EBAY_CLIENT_SECRET` (RuName not used) |
| **Host(s)** | `svcs.ebay.com` | `api.ebay.com/buy/browse/v1` + `api.ebay.com/identity/v1/oauth2/token` |
| **Keyword rank** | `getMostWatchedItems` list position | `item_summary/search` Best Match position per seed query |
| **Engagement signal** | `WatchSignal.watch_count` (watch count) | `VolumeSignal.sold_quantity` (`estimatedSoldQuantity`) |
| **Demand signal** | `SoldSignal.sold_rate` = sold/total **completed** | `SoldSignal.sold_rate` = sold / (sold + available) **sell-through** |
| **`SoldSignal.last_sold`** | parsed from completed-listing end time | always `None` (Browse has no per-sale time) |
| **`TrendingItem` fields** | `watch_count`, `norm_watch` | `sold_quantity`, `norm_volume` |
| **Protocol method** | `fetch_watch_signals` → `list[WatchSignal]` | `fetch_volume_signals` → `list[VolumeSignal]` |
| **Scorer weight const** | `WATCH_WEIGHT` | `VOLUME_WEIGHT` (still 2.0) |
| **Noise gate** | drop if watch==0 AND sold==0 | also keep items with a `keyword_rank` (§8) |
| **Cache schema key** | `trending:ebay:v1:*` | `trending:ebay:v2:*` |
| **Server JSON / UI** | `watch_count`, `norm_watch`; column "Watch" / "Sold rate" | `sold_quantity`, `norm_volume`; column "Sold" / "Sell-through" |
