# Trending Items — Feature Architecture

> **Status:** Implemented on the modern **eBay Browse API** (OAuth client-credentials).
> Offline unit tests pass (127 passed, 0 failed). Online integration testing with
> real `EBAY_CLIENT_ID`/`EBAY_CLIENT_SECRET` and live Redis not yet done.
> **Migration note:** the original design used the now-deprecated Merchandising
> (`getMostWatchedItems`) and Finding (`findCompletedItems`) APIs. Those were
> retired by eBay, so the feature was migrated to the Browse API. The watch-count
> signal has no Browse equivalent and was replaced by **sold volume**
> (`estimatedSoldQuantity`); see §1/§3.
> **Scope:** Development feature. eBay only for the first cut; built behind a
> provider protocol so other marketplaces drop in later.

---

## 0. Change Log — Legacy APIs → Browse API (schema **v1 → v2**)

> **Read this first.** This section is the authoritative summary of *what changed
> and why* between the **original** implementation (deprecated Merchandising +
> Finding APIs) and the **current** implementation (modern Browse API). Where a
> later section still shows the old shape, this section overrides it; the
> superseded sections are mapped in **§0.7**.

### 0.1 Why the rewrite

The feature originally pulled its three signals from two **legacy** eBay APIs:

- **Merchandising API** `getMostWatchedItems` — keyword rank + watch counts.
- **Finding API** `findCompletedItems` — sold/completed-listing rate.

Both have since been **deprecated and retired** by eBay. `findCompletedItems`
(sold-completed data) was access-restricted then shut off for general apps, and
the Merchandising/Finding services were sunset. The credentials a developer is
issued today (`EBAY_CLIENT_ID` + `EBAY_CLIENT_SECRET`, optionally `EBAY_RUNAME`)
are **OAuth keys for the modern REST APIs**, not the old App-ID query-key the
legacy services used.

The feature was therefore migrated to the **Browse API** (`buy/browse/v1`), which
is current, non-deprecated, and reachable with exactly those OAuth credentials.
Because the Browse API exposes **no watch-count and no completed-sales** data, the
*watch* signal was redefined as **sold volume** and the *sold-rate* signal as
**sell-through** — both derived from data Browse *does* expose
(`estimatedSoldQuantity` / `estimatedAvailableQuantity`).

### 0.2 Before → after at a glance

| Concern | **Old (v1)** | **New (v2)** |
| ------- | ------------ | ------------ |
| **Auth** | App ID as a query param (`CONSUMER-ID` / `SECURITY-APPNAME`); no token | OAuth 2.0 **client-credentials** → application Bearer token, cached on the provider |
| **Credentials** | `EBAY_APP_ID` | `EBAY_CLIENT_ID` + `EBAY_CLIENT_SECRET` (RuName **not** used) |
| **Host** | `svcs.ebay.com` (legacy SOAP/XML-ish) | `api.ebay.com/buy/browse/v1` (REST/JSON) + `api.ebay.com/identity/v1/oauth2/token` |
| **Signal 1 — rank** | `getMostWatchedItems` list position | `item_summary/search` **Best Match** position per seed query |
| **Signal 2 — engagement** | `watchCount` (watch count) | **`estimatedSoldQuantity`** (units sold) via `getItem` |
| **Signal 3 — demand** | `findCompletedItems` sold/total **completed** rate | **sell-through** = `sold / (sold + available)` via `getItem` |
| **Weights** | `2 / 2 / 1` (keyword / watch / sold) | `2 / 2 / 1` (keyword / **volume** / sell-through) — unchanged ratio |
| **Cache schema** | `trending:ebay:**v1**:*` | `trending:ebay:**v2**:*` (model changed → key bump) |
| **Tests** | 119 passed | **127 passed, 0 failed** (net +8 checks, see §0.6) |

### 0.3 New-version architecture (data flow)

The module boundaries are **unchanged** — only the network layer
(`trending_fetcher.py`) and the signal shapes were reworked. Fetcher → scorer →
cache → Flask → UI all still hold.

```
GET /trending (server.py)
        │
        ▼
trending_cache.load()  ──hit (TTL>0)──►  return cached list[TrendingItem]
        │                                 (+ warm-refresh if TTL < 15 min, §7.2)
        │ miss / expired
        ▼
trending_fetcher.EbayTrendingProvider          (implements TrendingProvider)
   1. _get_token()              ─► POST identity/v1/oauth2/token   (cached 2h)
   2. fetch_keyword_signals(60) ─► GET  browse/v1/item_summary/search?q=<seed>
                                     · iterate DEFAULT_SEED_QUERIES
                                     · position in Best Match = rank
                                     · dedupe across seeds, keep best rank
                                     · KeywordSignal carries title + itemWebUrl
   3. fetch_volume_signals(ids) ─► GET  browse/v1/item/{id}  → estimatedSoldQuantity
   4. fetch_sold_signals(ids)   ─► GET  browse/v1/item/{id}  → sold/(sold+available)
                                     · (3) and (4) share ONE memoized getItem/id
        │  raw signal lists (KeywordSignal / VolumeSignal / SoldSignal)
        ▼
trending_scorer.score_trending()
   · join by item_id · predicate filter · min-max normalize each signal
   · score = 2·norm_keyword + 2·norm_volume + 1·norm_sold · sort · top 10
        │  list[TrendingItem]
        ▼
trending_cache.save()  ─► SET trending:ebay:v2:ranked = JSON  EX 10800 (3h)
                          SET trending:ebay:v2:raw    = JSON  EX 10800
        │
        ▼
index.html — Trending tab renders: # · Title→url · Source · Score · Sold · Sell-through
```

### 0.4 Change log by file

- **`src/trending_fetcher.py`** — *full rewrite.*
  - New OAuth client-credentials flow: `_get_token()` mints a Bearer token once and
    caches it on the instance.
  - `fetch_keyword_signals` searches a list of **seed queries**
    (`DEFAULT_SEED_QUERIES`, e.g. *electronics, sneakers, trading cards, …*),
    treats each item's Best Match position as its rank, and **dedupes across seeds
    keeping the best (lowest) rank**.
  - `fetch_volume_signals` / `fetch_sold_signals` both read `getItem`; a per-instance
    **memoization cache** (`_item_detail`) ensures **one `getItem` call per item**
    even though two signals consume it.
  - Item IDs are **URL-encoded** (`v1|123|0` → `v1%7C123%7C0`); a `getItem` 404 /
    error **skips that candidate** instead of failing the whole fetch.
  - Constructor now takes `client_id` / `client_secret` (env fallback
    `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET`), `seed_queries`, and `marketplace`;
    `max_results` default lowered `50 → 10` (per-seed page size). `_validate_key()`
    requires **both** id and secret.

- **`src/models.py`** — signal shapes.
  - `KeywordSignal`: **added `title` and `url`** (default `""`) so the ranked output
    links straight to the live listing.
  - **`WatchSignal` → `VolumeSignal`**; field **`watch_count` → `sold_quantity`**.
  - `SoldSignal`: structurally the same, but semantics changed — `sold_count` =
    units sold, `total_count` = sold + available, `sold_rate` = sell-through,
    `last_sold` is **always `None`** (Browse has no per-sale timestamps).
  - `TrendingItem`: **`watch_count` → `sold_quantity`**, **`norm_watch` → `norm_volume`**.
  - `TrendingProvider`: **`fetch_watch_signals` → `fetch_volume_signals`** (returns
    `list[VolumeSignal]`).

- **`src/trending_scorer.py`** — wiring + one behavior change + one bug fix.
  - Constant **`WATCH_WEIGHT` → `VOLUME_WEIGHT`** (still `2.0`); internal
    `norm_watch` → `norm_volume`, tiebreaker now `sold_quantity` desc.
  - Candidate join now lifts `title` + `url` off `KeywordSignal`.
  - **Noise gate relaxed (see §0.5).**
  - **Bug fix:** `_maybe_refresh()` (warm-refresh path) referenced an undefined
    variable after the rename — a latent `NameError` that *no test exercised*. Fixed
    and now covered by a dedicated test.

- **`src/trending_cache.py`** — `SCHEMA_VER` **`v1` → `v2`**; raw-snapshot JSON now
  serializes `volume_signals` (with `sold_quantity`) and the extra `title`/`url`
  on keyword rows; `save()` param `watch_signals → volume_signals`.

- **`server.py`** — `/trending` JSON: `watch_count → sold_quantity`,
  `norm_watch → norm_volume`.

- **`index.html`** — Trending table headers **`Watch → Sold`**, **`Sold rate →
  Sell-through`**; JS cell `it.watch_count → it.sold_quantity`.

- **`CLAUDE.md`** — `.env` block documents `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET`
  and that `EBAY_RUNAME` is not required.

### 0.5 Behavioral changes worth knowing

- **Noise gate is more permissive.** Old gate dropped an item when
  `watch_count == 0 AND sold_rate == 0`. Because Browse's `estimatedSoldQuantity`
  is **often absent/zero**, the new gate also keeps an item if it was **surfaced by
  Best Match search** (i.e. has a `keyword_rank`). Net effect: appearing in the top
  search results is itself treated as a trending signal, so the list won't collapse
  to empty on items that lack sold data. `_passes_predicate` gained a
  `keyword_rank` parameter to support this.
- **Sparse sold data is expected.** On live data, some candidates will score on
  keyword rank alone (volume/sell-through normalize to `0.0` for them). Tune
  `DEFAULT_SEED_QUERIES` and the weights once real output is observed.
- **Rate/latency.** One search per seed + one `getItem` per unique candidate, all
  behind the 3-hour Redis cache, stays well within the default 5,000 calls/day.

### 0.6 Test changes (`src/test_setup.py`, sections 12–16)

Rewrote the trending sections for the new shapes and **added coverage**:
OAuth token mint-and-cache, missing-`access_token` error, search→`KeywordSignal`
mapping (incl. title/url), `getItem`→`VolumeSignal` mapping, sell-through math,
**`getItem` memoization** (volume+sold = 1 call), `getItem` 404-skip, search
non-200 error, **cross-seed rank dedup**, `KeywordSignal` title/url defaults,
`_validate_key` missing-secret, **keyword-only candidate survives the noise gate**,
and the **warm-refresh path** (regression test for the §0.4 bug fix). Result:
**127 passed, 0 failed** (was 119).

### 0.7 Which sections below are superseded

The sections that follow were written for the **original** design and are kept for
historical context. Where they conflict with §0, **§0 wins**:

| Section | Status |
| ------- | ------ |
| §1 Feature Summary, §3 eBay API Endpoints, Prerequisites | **Updated** to Browse API ✔ |
| §2 Data-flow diagram | **Superseded** — use the diagram in §0.3 (still shows `getMostWatchedItems` / Finding) |
| §4 Data Model | **Superseded** — shows `WatchSignal`/`watch_count`/`norm_watch` and `fetch_watch_signals`; see §0.4 for the real shapes |
| §5 Module Breakdown | **Superseded** — `EbayTrendingProvider` interface, `WATCH_WEIGHT`, and the legacy endpoint descriptions are pre-migration |
| §6 Scoring Algorithm | **Mostly valid** (normalize → weight → rank is unchanged) but rename `watch → volume`; the worked example's "watch 500" is now "sold_quantity 500" |
| §7 Caching | **Valid**, except key version is now `v2` not `v1` |
| §8 Predicate Filters | **Superseded** — noise gate now also keeps keyword-surfaced items (§0.5) |
| §9 Extensibility | **Valid** (protocol-based design unchanged; substitute `VolumeSignal`) |
| §10 Flask + UI | **Superseded** — route emits `sold_quantity`/`norm_volume`; UI columns are `Sold`/`Sell-through` |
| §11 Implementation Order | **Historical** — original build order; commits reflect the migration instead |

---

## Prerequisites

### Environment variables (add to `.env`)

```
EBAY_CLIENT_ID=<your_ebay_client_id>
EBAY_CLIENT_SECRET=<your_ebay_client_secret>
REDIS_URL=redis://localhost:6379/0     # local dev; use redis://redis:6379/0 inside Docker Compose
```

**EBAY_CLIENT_ID / EBAY_CLIENT_SECRET** — get from [developer.ebay.com](https://developer.ebay.com/):
- Register/login and create an app keyset to obtain a **Client ID** and **Client Secret** (production keyset).
- These two are exchanged for an **application access token** via the OAuth 2.0
  *client-credentials* grant (scope `https://api.ebay.com/oauth/api_scope`). No
  `EBAY_RUNAME` / user-consent flow is needed for public Browse search.
- The **Browse API** is available on a standard developer account (default 5,000 calls/day).

**REDIS_URL** — requires a running Redis instance:
- Local dev (Mac): `brew install redis && brew services start redis`
- Docker Compose: the compose file handles it; set `REDIS_URL=redis://redis:6379/0`

### Python dependencies (add to `requirements.txt`)

| Package | Purpose |
| ------- | ------- |
| `redis` | Python client used by `trending_cache.py` |
| `fakeredis` | In-process Redis for offline tests in `test_setup.py` |
| `httpx` | HTTP client for `trending_fetcher.py` (likely already present) |

Your existing `SERPAPI_KEY` is unaffected — the Trending feature is independent of it.

---

This document is the implementation spec for the **Trending Items** feature: a
new tab in the existing Flask UI that surfaces the top 10 trending items from
eBay, ranked by a weighted blend of three signals.

---

## 1. Feature Summary

Add a **Trending** tab to the existing Product Vision Matcher UI. The tab shows
the **top 10 trending eBay items** over a rolling **60-day window**, ranked by a
weighted score built from three independent signals:

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
buffer, see §7) so the UI does not hammer the eBay API on every page load. The
whole fetch/score path sits behind a `TrendingProvider` protocol (mirroring the
existing `ReverseSearchProvider` pattern), so Amazon and other marketplaces can
be added later without touching the scorer, cache, or Flask layers.

**Design principles inherited from the existing codebase:**

- Shared dataclasses and protocols live in `models.py`; modules import types from
  there, never from each other.
- The orchestration layer depends on the **protocol**, not the concrete eBay
  implementation, so backends are swappable.
- API keys and HTTP clients are **injected via the constructor** — no import-time
  global state — so each module is unit-testable offline.

---

## 2. Data Flow Diagram

```
                        ┌─────────────────────────────────────────────┐
                        │  GET /trending  (Flask route in server.py)   │
                        └───────────────────────┬─────────────────────┘
                                                │
                                                ▼
                              ┌───────────────────────────────────┐
                              │      trending_cache.py            │
                              │  load() — GET redis key (TTL>0?)  │
                              │  + warm-refresh if TTL < 15 min   │
                              └───────────┬───────────────┬───────┘
                                  cache HIT │               │ cache MISS / EXPIRED
                                          │               │
                  ┌───────────────────────┘               ▼
                  │                         ┌──────────────────────────────────┐
                  │                         │       trending_fetcher.py        │
                  │                         │  EbayTrendingProvider            │
                  │                         │  (implements TrendingProvider)   │
                  │                         │                                  │
                  │                         │  fetch_keyword_signals(60)  ─────┼──► eBay Merchandising API
                  │                         │  fetch_watch_signals(ids,60)─────┼──► getMostWatchedItems
                  │                         │  fetch_sold_signals(ids,60) ─────┼──► Finding findCompletedItems
                  │                         └──────────────┬───────────────────┘
                  │                                        │  raw signal lists
                  │                                        ▼
                  │                         ┌──────────────────────────────────┐
                  │                         │       trending_scorer.py         │
                  │                         │  1. predicate filter             │
                  │                         │  2. min-max normalize each signal│
                  │                         │  3. weighted sum                 │
                  │                         │  4. sort desc, take top 10       │
                  │                         └──────────────┬───────────────────┘
                  │                                        │  list[TrendingItem]
                  │                                        ▼
                  │                         ┌──────────────────────────────────┐
                  │                         │  trending_cache.save(...)        │
                  │                         │  SET trending:ebay:v1:ranked     │
                  │                         │   value=JSON  EX=10800 (3h TTL)  │
                  │                         │  (fetch guarded by a SET NX lock │
                  │                         │   with its own TTL — see §7)     │
                  │                         └──────────────┬───────────────────┘
                  │                                        │
                  └────────────────┬───────────────────────┘
                                  ▼
                  ┌────────────────────────────────┐
                  │  JSON: list[TrendingItem] (10)  │
                  └───────────────┬─────────────────┘
                                  ▼
                  ┌────────────────────────────────┐
                  │   index.html — Trending tab     │
                  │   renders ranked table          │
                  └────────────────────────────────┘
```

Note the symmetry with the existing image pipeline: `trending_fetcher` is the
network boundary (like `reverse_search`), `trending_scorer` is the
filter+rank stage (like `marketplace_parser` + `price_aggregator`), and
`trending_cache` is the new persistence concern unique to this feature — now
backed by **Redis** (keyed, TTL'd values) rather than a flat JSON file on disk.

---

## 3. eBay API Endpoints

All three signals come from the modern **Browse API** (`buy/browse/v1`). Auth is an
OAuth 2.0 **application access token** minted from `EBAY_CLIENT_ID` +
`EBAY_CLIENT_SECRET` via the client-credentials grant; the token is cached on the
provider instance and sent as `Authorization: Bearer <token>` with
`X-EBAY-C-MARKETPLACE-ID: EBAY_US`.

```
# add to .env
EBAY_CLIENT_ID=<your_ebay_client_id>
EBAY_CLIENT_SECRET=<your_ebay_client_secret>
REDIS_URL=redis://localhost:6379/0     # in-container (compose): redis://redis:6379/0
```

> **All three are secret/config values and live in `.env`**, which is already
> gitignored (`.gitignore` lists `.env`) and `.dockerignore`d (see
> `dockerfile_plan.md` §2) — never committed, never baked into an image layer.
> They are read at runtime exactly like `SERPAPI_KEY`, injected via `env_file`
> under compose. `EBAY_RUNAME` is **not** used (no user-consent flow).

> The `lookback_days` parameter is **60**. The Browse API returns active listings
> and a point-in-time `estimatedSoldQuantity`; it exposes no per-sale date window,
> so the 60-day window is a soft client-side concept only (the recency gate in
> §8 falls back to `fetched_at`, which is always current for live listings).

### 3.0 OAuth token — client-credentials grant

|                |                                                                                                              |
| -------------- | ------------------------------------------------------------------------------------------------------------ |
| **Endpoint**   | `POST https://api.ebay.com/identity/v1/oauth2/token`                                                         |
| **Headers**    | `Content-Type: application/x-www-form-urlencoded`, `Authorization: Basic base64(CLIENT_ID:CLIENT_SECRET)`    |
| **Body**       | `grant_type=client_credentials&scope=https://api.ebay.com/oauth/api_scope`                                   |
| **Returns**    | `{ "access_token": "...", "expires_in": 7200 }` — an application token valid 2h. Cached per provider instance. |

### 3.1 Keyword/rank signal — `item_summary/search` (Best Match)

|                |                                                                                                              |
| -------------- | ------------------------------------------------------------------------------------------------------------ |
| **Endpoint**   | `GET https://api.ebay.com/buy/browse/v1/item_summary/search`                                                 |
| **Key params** | `q=<seed query>`, `limit=<max_results>` (default Best Match sort — eBay's relevance/popularity ordering)     |
| **Returns**    | `itemSummaries[]` with `itemId`, `title`, `itemWebUrl`. The item's **position** within each seed query is its rank (1 = top). Items appearing under multiple seeds keep their best (lowest) rank; the scorer inverts rank (see §6). |

> The provider iterates a curated list of seed queries (`DEFAULT_SEED_QUERIES` in
> `trending_fetcher.py`, e.g. *electronics, sneakers, trading cards, …*), since
> Browse search requires a query. `KeywordSignal` now also carries `title` and
> `url` so the ranked output links straight to the live listing.

### 3.2 Sold-volume signal — `getItem` `estimatedSoldQuantity`

|                |                                                                                                              |
| -------------- | ------------------------------------------------------------------------------------------------------------ |
| **Endpoint**   | `GET https://api.ebay.com/buy/browse/v1/item/{item_id}` (item_id URL-encoded)                                |
| **Returns**    | `estimatedAvailabilities[0].estimatedSoldQuantity` — units sold for the listing. This is the raw volume signal (replaces the retired watch count). No special access required. |

### 3.3 Sell-through signal — `getItem` sold / (sold + available)

|                |                                                                                                              |
| -------------- | ------------------------------------------------------------------------------------------------------------ |
| **Endpoint**   | Same `getItem` call as §3.2 (response is memoized per item so it is fetched once).                           |
| **Returns**    | `sold_rate = estimatedSoldQuantity / (estimatedSoldQuantity + estimatedAvailableQuantity)`, in `[0,1]`. `SoldSignal.last_sold` is always `None` (Browse exposes no per-sale timestamps). |

**Querying order:** `fetch_keyword_signals` runs the seed searches first to obtain
the candidate `item_ids` (plus titles/urls). Those `item_ids` are passed into
`fetch_volume_signals` and `fetch_sold_signals`, which share one memoized
`getItem` call per id, so all three signals are keyed to the same candidate set.

---

## 4. Data Model

All new dataclasses are added to **`src/models.py`** alongside the existing
`ProcessedImage`, `ParsedListing`, and `PriceReport`. They follow the same
`@dataclass` + `from __future__ import annotations` style already in the file.

```python
# ── Trending feature signals (one per eBay source) ────────────────────────────

@dataclass
class KeywordSignal:
    item_id:   str            # eBay itemId this keyword maps to (or "" for pure-keyword rows)
    keyword:   str            # the trending search term / category label
    rank:      int            # 1 = most trending; lower is stronger
    fetched_at: datetime      # when this signal was pulled (UTC)


@dataclass
class WatchSignal:
    item_id:    str           # eBay itemId
    title:      str           # item display title
    watch_count: int          # raw watch count from getMostWatchedItems
    fetched_at:  datetime     # UTC


@dataclass
class SoldSignal:
    item_id:     str          # eBay itemId
    title:       str          # item display title
    sold_count:  int          # number of completed-with-sale listings in the window
    total_count: int          # total completed listings in the window (sold + unsold)
    sold_rate:   float        # sold_count / total_count, in [0.0, 1.0]; 0.0 if total_count == 0
    last_sold:   datetime | None  # most recent sale within the window; None if no sales
    fetched_at:  datetime     # UTC


# ── Final ranked output row ───────────────────────────────────────────────────

@dataclass
class TrendingItem:
    item_id:        str        # eBay itemId — primary key joining all three signals
    title:          str        # display title
    url:            str        # https:// link to the eBay listing
    source:         str        # marketplace name, e.g. "eBay" (forward-looking, multi-market)
    rank:           int        # final position in the trending list, 1 (top) – 10
    score:          float      # final weighted score (un-normalized sum of weighted norms)
    # raw signal values, carried through for display / debugging:
    keyword_rank:   int | None  # None when the keyword signal was missing
    watch_count:    int | None  # None when the watch signal was missing
    sold_rate:      float | None  # None when the sold signal was missing
    # normalized [0,1] components, for transparency in the UI / tests:
    norm_keyword:   float       # 0.0 when signal missing (graceful degradation)
    norm_watch:     float
    norm_sold:      float
```

### `TrendingProvider` protocol (also in `models.py`)

```python
class TrendingProvider(Protocol):
    """Contract for any trending-items backend (eBay first, others later).

    Each method fetches one raw signal over a lookback window. The scorer
    consumes the three signal lists; it never depends on a concrete provider.
    Mirrors the ReverseSearchProvider pattern: orchestration depends on this
    protocol, not on EbayTrendingProvider.
    """

    def fetch_keyword_signals(self, lookback_days: int) -> list[KeywordSignal]: ...

    def fetch_watch_signals(
        self, item_ids: list[str], lookback_days: int
    ) -> list[WatchSignal]: ...

    def fetch_sold_signals(
        self, item_ids: list[str], lookback_days: int
    ) -> list[SoldSignal]: ...
```

---

## 5. Module Breakdown

### 5.1 `src/trending_fetcher.py` — eBay `TrendingProvider` implementation

**Responsibility:** The network boundary for trending data. Talks to the three
eBay endpoints, maps raw JSON into `KeywordSignal` / `WatchSignal` / `SoldSignal`
dataclasses. No filtering, no scoring, no normalization — that belongs to the
scorer (same separation as `reverse_search` vs `marketplace_parser`).

**Public interface:**

```python
class EbayTrendingProvider:
    """Trending-items backend backed by eBay Merchandising + Finding APIs.

    Implements the TrendingProvider protocol (see models.py).
    App ID and HTTP client are injected so the provider can be unit-tested
    offline with an httpx.MockTransport (no network in test_setup.py).
    """

    def __init__(
        self,
        app_id: str | None = None,      # None -> fall back to EBAY_APP_ID env var
        client: httpx.Client | None = None,
        timeout: float = 30.0,
        max_results: int = 50,
    ) -> None: ...

    def fetch_keyword_signals(self, lookback_days: int) -> list[KeywordSignal]: ...
    def fetch_watch_signals(self, item_ids: list[str], lookback_days: int) -> list[WatchSignal]: ...
    def fetch_sold_signals(self, item_ids: list[str], lookback_days: int) -> list[SoldSignal]: ...
```

**Conventions to match the existing `SerpApiSearcher`:**

- `app_id=None` means "fall back to the environment"; an explicit `""` is honored
  as an (invalid) key so a `_validate_key()` guard can be unit-tested.
- A `_validate_key()` raises `EnvironmentError("EBAY_APP_ID not set in .env")`.
- HTTP non-200 raises `RuntimeError(f"eBay {status}: {body[:300]}")`.
- If the client is injected, reuse it; otherwise open a short-lived
  `httpx.Client(timeout=self._timeout)` inside each public method.

### 5.2 `src/trending_scorer.py` — normalization + weighted scoring

**Responsibility:** Pure, network-free transformation. Takes the three raw signal
lists, applies predicate filters, min-max normalizes each signal, computes the
weighted score, sorts, and returns the top 10 `TrendingItem` rows. This is the
analog of `price_aggregator.rank_by_price` — deterministic and trivially
testable with in-process fixtures.

**Public interface:**

```python
# weights are module-level constants so tests and docs reference one source
KEYWORD_WEIGHT = 2.0
WATCH_WEIGHT   = 2.0
SOLD_WEIGHT    = 1.0
TOP_N          = 10

def score_trending(
    keyword_signals: list[KeywordSignal],
    watch_signals:   list[WatchSignal],
    sold_signals:    list[SoldSignal],
    top_n: int = TOP_N,
) -> list[TrendingItem]:
    """Filter, normalize, weight, rank → top-N TrendingItem list (rank 1..N)."""

# internal helpers (private):
def _min_max(values: list[float]) -> dict[key, float]: ...   # per-signal normalization
def _passes_predicate(item_id, watch_count, sold_rate, last_active) -> bool: ...
```

### 5.3 `src/trending_cache.py` — Redis-backed cache read/write

**Responsibility:** Persist and retrieve the computed trending list in **Redis**
with a 3-hour key TTL. Owns the only knowledge of the Redis key layout, the JSON
value encoding, the warm-refresh buffer, and the concurrency lock. No eBay or
scoring logic lives here.

**Why Redis instead of a flat JSON file:**

- **Native TTL** — Redis expires keys for us (`SET ... EX 10800`); no
  timestamp-vs-`now` arithmetic, no stale file lingering on disk.
- **Shared across workers** — under gunicorn (`-w 4`, see `dockerfile_plan.md`
  §3) all worker processes hit one Redis, so the cache is computed once and
  shared, instead of each worker keeping its own on-disk/in-process copy.
- **Atomic primitives** — `SET NX EX` gives us the single-flight lock (below)
  for free; no temp-file/`os.replace` dance.

**Key naming (versioned):**

```
trending:<marketplace>:<schema_version>:<part>

trending:ebay:v1:ranked       # the JSON-encoded list[TrendingItem] served to the UI
trending:ebay:v1:raw          # the raw signal snapshot (for offline re-scoring)
trending:ebay:v1:lock         # single-flight fetch lock (see §7)
```

The embedded **`v1`** is a **schema version baked into the key**. If the
dataclass model changes (a field added/removed/renamed in `TrendingItem` or any
signal), bump it to `v2` — old `v1` keys are simply never read again and expire
on their own. No migration, no "corrupt cache" branch: a model change is a key
rename. (This replaces the old JSON `"version": 1` field.)

**Public interface:**

```python
import os, redis

REDIS_URL    = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
SCHEMA_VER   = "v1"                              # bump on dataclass changes
TTL_SECONDS  = 3 * 60 * 60                       # 3 hours
REFRESH_FLOOR_SECONDS = 15 * 60                  # warm-refresh when remaining TTL < 15 min
LOCK_TTL_SECONDS = 60                            # lock self-expires so a crash can't wedge it

def _key(part: str, marketplace: str = "ebay") -> str:
    return f"trending:{marketplace}:{SCHEMA_VER}:{part}"

def load(client: redis.Redis) -> tuple[list[TrendingItem] | None, int]:
    """GET trending:ebay:v1:ranked. Returns (items, remaining_ttl_seconds).
    - (items, ttl)  on hit, where ttl = client.ttl(key) so the caller can decide
                    whether to trigger a warm refresh (ttl < REFRESH_FLOOR_SECONDS).
    - (None, -2)    on miss/expired (Redis TTL of -2 == key does not exist).
    Returns (None, -2) and logs if Redis is unreachable (caller falls through to
    a live fetch — there is no on-disk fallback, see §7 on in-memory fallback)."""

def save(
    client: redis.Redis,
    items: list[TrendingItem],
    keyword_signals: list[KeywordSignal],
    watch_signals:   list[WatchSignal],
    sold_signals:    list[SoldSignal],
    marketplace: str = "ebay",
) -> None:
    """SET trending:ebay:v1:ranked = json(items)  EX=TTL_SECONDS, and likewise
    trending:ebay:v1:raw = json(raw signals). Single round-trip per key; Redis
    applies the TTL atomically, so there is no partially-written/corrupt state."""

def acquire_lock(client: redis.Redis, marketplace: str = "ebay") -> bool:
    """SET trending:ebay:v1:lock = <token>  NX EX=LOCK_TTL_SECONDS.
    Returns True if this worker won the right to fetch from eBay, False if another
    worker already holds it. The EX makes the lock self-healing: if the holder
    crashes before releasing, the lock expires after LOCK_TTL_SECONDS instead of
    blocking all future fetches forever (see §7)."""

def release_lock(client: redis.Redis, marketplace: str = "ebay") -> None:
    """DEL the lock key once the fetch+save completes (best-effort; the LOCK_TTL
    is the backstop if this DEL is missed)."""
```

### 5.4 Orchestration helper

A thin orchestrator wires the three together. It can live as a `get_trending()`
function in `trending_scorer.py` or a small new function; the Flask route calls
it. It depends on the **`TrendingProvider` protocol**, not on
`EbayTrendingProvider`:

```python
def get_trending(
    provider: TrendingProvider,
    client: redis.Redis,
    lookback_days: int = 60,
) -> list[TrendingItem]:
    items, ttl = trending_cache.load(client)

    if items is not None:
        # Cache HIT. If the key is close to expiry, kick a warm refresh so the
        # next user never waits on a cold eBay round-trip (see §7). The refresh
        # is single-flighted by the lock, and we still serve the current (warm)
        # data immediately — refresh does not block this request.
        if ttl < trending_cache.REFRESH_FLOOR_SECONDS:
            _maybe_refresh(provider, client, lookback_days)  # best-effort, async/inline
        return items

    # Cache MISS / EXPIRED → fetch, but guard with a single-flight lock so a
    # thundering herd of requests doesn't fire N concurrent eBay fetches.
    return _fetch_and_cache(provider, client, lookback_days)


def _fetch_and_cache(provider, client, lookback_days) -> list[TrendingItem]:
    if not trending_cache.acquire_lock(client):
        # Another worker is already fetching. Briefly re-check the cache; if it's
        # populated by the time we look, serve that. Otherwise compute locally
        # this once (no stale fallback exists — see §7).
        items, _ = trending_cache.load(client)
        if items is not None:
            return items
    try:
        kw  = provider.fetch_keyword_signals(lookback_days)
        ids = [k.item_id for k in kw if k.item_id]
        w   = provider.fetch_watch_signals(ids, lookback_days)
        s   = provider.fetch_sold_signals(ids, lookback_days)
        items = trending_scorer.score_trending(kw, w, s)
        trending_cache.save(client, items, kw, w, s)
        return items
    finally:
        trending_cache.release_lock(client)   # LOCK_TTL is the backstop if this is missed
```

---

## 6. Scoring Algorithm

Given the three raw signal lists, produce the top 10 ranked `TrendingItem`s.

**Step 0 — Join signals by `item_id`.**
Build a candidate set keyed by `item_id`. Each candidate may have any subset of
the three signals present (graceful degradation, see Section 8).

**Step 1 — Apply predicate filters (Section 8).**
Drop noise candidates _before_ normalization so the min/max bounds are computed
only over surviving candidates.

**Step 2 — Derive each candidate's raw signal values.**

- `keyword_raw` — invert rank so higher = more trending:
  `keyword_raw = (max_rank + 1) - rank`, where `max_rank` is the largest rank
  among surviving candidates. (A candidate ranked #1 gets the highest raw value.)
  If no keyword signal: treated as missing.
- `watch_raw = watch_count` (missing → treated as missing).
- `sold_raw = sold_rate` in `[0,1]` (missing → treated as missing).

**Step 3 — Min-max normalize each signal independently to `[0, 1]`.**

For a signal with values `v` over surviving candidates:

```
norm(v) = (v - min) / (max - min)     if max > min
norm(v) = 1.0                          if max == min  (all equal, all non-missing)
```

Edge cases:

- A **missing** signal for a candidate normalizes to **`0.0`** (graceful
  degradation — a candidate is never penalized below zero, and present signals
  still drive its rank).
- If _all_ candidates are missing a given signal, that signal contributes `0.0`
  for everyone and effectively drops out of the ranking — the remaining signals
  decide the order.

**Step 4 — Weighted sum.**

```
score = (KEYWORD_WEIGHT × norm_keyword)
      + (WATCH_WEIGHT   × norm_watch)
      + (SOLD_WEIGHT    × norm_sold)

# with the agreed weights:
score = (2 × norm_keyword) + (2 × norm_watch) + (1 × norm_sold)
```

Score range is `[0, 5]` (max when a candidate tops every normalized signal:
`2×1 + 2×1 + 1×1 = 5`).

**Step 5 — Sort and slice.**
Sort candidates by `score` descending; ties broken by `watch_count` descending,
then `item_id` ascending for determinism. Assign `rank = 1..N` and return the
first `TOP_N` (10) as `TrendingItem`s, populating both the raw values and the
`norm_*` components for UI transparency.

**Worked micro-example (2 candidates, all signals present):**

```
Candidate A: keyword rank 1, watch 500, sold_rate 0.40
Candidate B: keyword rank 2, watch 100, sold_rate 0.10

keyword_raw:  A=(2+1)-1=2,  B=(2+1)-2=1     → norm: A=1.0, B=0.0
watch_raw:    A=500, B=100  (min100,max500) → norm: A=1.0, B=0.0
sold_raw:     A=0.40,B=0.10 (min.10,max.40) → norm: A=1.0, B=0.0

score(A) = 2*1.0 + 2*1.0 + 1*1.0 = 5.0   → rank 1
score(B) = 2*0.0 + 2*0.0 + 1*0.0 = 0.0   → rank 2
```

---

## 7. Caching Strategy (Redis)

**Why:** eBay APIs are rate-limited and slow; trending data changes slowly. A
3-hour cache keeps the UI snappy and well within rate limits. Redis (rather than
a flat JSON file) gives us native key expiry, a cache shared across all gunicorn
workers, and atomic primitives for single-flight fetching.

**Backing store:** Redis, reached via `REDIS_URL` from `.env` (§3). Persistence
(RDB/AOF snapshots) is configured on the Redis container so the cache is **read
back warm on startup** rather than cold — see `dockerfile_plan.md` §4.2.

### 7.1 Keys, values, and the 3-hour TTL

- **Ranked list:** `trending:ebay:v1:ranked` → JSON-encoded `list[TrendingItem]`.
- **Raw snapshot:** `trending:ebay:v1:raw` → JSON of the three raw signal lists,
  so the scorer can be re-run/re-tuned offline and so `test_setup.py` has
  realistic fixtures (same rationale as the old `raw_signals` block).
- Both are written with **`SET ... EX 10800`** (3 hours). Redis evicts them
  automatically when the TTL hits zero — **expiry within a 3-hour window is the
  store's job**, not application timestamp math. On `GET /trending`:
  - key present (TTL > 0) → **cache hit**, return it, no eBay calls;
  - key absent (TTL == -2, expired/evicted) → **cache miss**, fetch + score +
    `SET EX` + return.

### 7.2 Warm-refresh buffer (refresh before expiry, when TTL < 15 min)

A plain TTL means the *one* unlucky request that arrives the moment the key
expires eats the full cold eBay round-trip. To keep data **warm**, `load()`
returns the key's **remaining TTL** alongside the value, and the orchestrator
proactively refreshes *before* expiry:

```
on cache HIT:
    if remaining_ttl < REFRESH_FLOOR_SECONDS (15 min):
        single-flight a background refresh   # re-fetch, re-score, SET EX 10800
    return the currently-cached (still-warm) data immediately   # never blocks
```

So during the last 15 minutes of a key's life, the first request to notice
triggers a refresh that resets the TTL back to 3 hours, while still serving the
existing data with zero added latency. Users effectively never hit a cold cache
under steady traffic. The refresh is guarded by the same lock (§7.4) so only one
worker actually re-fetches.

### 7.3 Schema versioning via the key (the `v1` segment)

The schema version is **embedded in the key name** (`trending:ebay:**v1**:...`)
rather than stored as a `"version"` field inside the value. If the dataclass
model changes — a field added to or removed from `TrendingItem` or any signal —
**bump the segment to `v2`** in `trending_cache.SCHEMA_VER`. Effect:

- new writes go to `trending:ebay:v2:*`;
- old `v1:*` keys are never read again and **expire on their own** within 3 hours;
- no migration code, no "is this old shape compatible?" branch, no corrupt-cache
  handling. A model change is a one-line key rename.

This replaces the old JSON `"version": 1` field and its `load()`-rejects-old-shape
logic.

### 7.4 Single-flight lock (with its own TTL)

On a cache miss (or a warm refresh), many concurrent requests could otherwise
each fire a full three-call eBay fetch — a thundering herd. A Redis lock
single-flights it:

```
acquire:  SET trending:ebay:v1:lock <token> NX EX 60
          → NX: only the first caller sets it (wins the fetch)
          → EX 60: the lock SELF-EXPIRES after LOCK_TTL_SECONDS
release:  DEL trending:ebay:v1:lock   (best-effort, in a finally:)
```

The **lock has its own TTL** precisely so a missed unlock can't wedge the
feature: if the worker holding the lock **crashes, is killed, or times out before
the `DEL`**, the lock would otherwise stay set forever and *no* worker could ever
refresh the cache again. The `EX 60` guarantees the lock evaporates after at most
60 seconds, after which a future request can re-acquire and fetch. The explicit
`DEL` in the `finally:` is the fast path; the TTL is the safety backstop.

Losers of the lock briefly re-check the cache (another worker may have just
populated it) and serve that; only if it's still empty do they compute once
locally for that request.

### 7.5 In-memory fallback note (multi-worker, Redis-down)

> **Note / known limitation — not a feature, just documented behavior.**
>
> Under gunicorn each worker is a separate process with its **own memory**. If a
> worker keeps a last-known-good copy of the trending list in a process-local
> variable as a fallback for when Redis is unreachable, that fallback is **per
> worker and not shared**: worker A may have a warm in-memory copy while worker B,
> **freshly started (or restarted) while Redis is down, has no in-memory data at
> all** — it never populated its local copy, so it has nothing stale to serve and
> must either do a live eBay fetch or surface an error.
>
> Consequences to keep in mind:
> - Responses can be **inconsistent across workers** during a Redis outage (one
>   worker serves cached data, another serves freshly-fetched or empty).
> - A just-started worker has **no warm state** — the in-memory fallback only
>   helps workers that were alive long enough to have cached a copy before Redis
>   went down.
>
> This is acceptable for the dev/single-host scope (Redis is the source of truth
> and is normally up; its RDB/AOF snapshot survives restarts). It's noted here so
> the behavior isn't mistaken for a bug, and so a future "shared warm fallback"
> (e.g. a secondary store, or pinning workers) is a conscious decision rather than
> a surprise.

### 7.6 Cache invalidation

- **Time-based** (primary): the 3-hour key TTL (§7.1), refreshed early by the
  warm buffer (§7.2).
- **Manual:** `DEL trending:ebay:v1:ranked trending:ebay:v1:raw` (or `FLUSHDB` in
  dev) forces a refetch on the next request.
- **Schema bump:** change `SCHEMA_VER` to `v2` (§7.3) — old keys age out on their
  own.

**Atomicity:** each `SET ... EX` is a single atomic Redis operation — value and
TTL land together — so there is no partially-written/corrupt state for `load()`
to choke on (this replaces the old temp-file + `os.replace` dance).

---

## 8. Predicate Filters

Applied in `trending_scorer._passes_predicate()` **before** normalization. A
candidate must pass **all** gates to be scored:

1. **Noise gate:** Exclude items with **`watch_count == 0` AND `sold_rate == 0`**
   (i.e. zero on both engagement signals → noise).
2. **Recency gate:** Include only items **active or sold within the last 60 days**.
   Concretely: the item's most recent activity timestamp (last watch refresh /
   `last_sold` / listing end time) must be `>= now - 60 days`. Items whose only
   data falls outside the window are dropped. (Mirrors the
   `_is_within_12_months` recency gate already in `marketplace_parser`, but with a
   60-day cutoff.)
3. **Data-presence gate (graceful degradation):** Keep the item only if it has
   data for **at least one** of the three signals. An item missing all three is
   dropped; an item missing one or two is kept, with the missing signal(s)
   normalizing to `0.0` (Section 6, Step 3).

> Order matters: filtering precedes normalization so the min/max bounds reflect
> only real, surviving candidates — otherwise a single noise row could skew the
> normalization range.

---

## 9. Extensibility Design (Multi-marketplace)

The feature is built eBay-first but marketplace-agnostic above the fetcher layer.

**The contract** is the `TrendingProvider` protocol in `models.py` (Section 4).
`trending_scorer`, `trending_cache`, and the Flask route depend **only** on this
protocol and the signal dataclasses — never on `EbayTrendingProvider`. This is
the same inversion the codebase already uses for `ReverseSearchProvider` /
`SerpApiSearcher`.

**To add a new marketplace (e.g. Amazon) later:**

1. Create `src/trending_fetcher_amazon.py` with an `AmazonTrendingProvider` class
   implementing the three `TrendingProvider` methods, mapping Amazon's API into
   the same `KeywordSignal` / `WatchSignal` / `SoldSignal` dataclasses.
2. Set `source="Amazon"` on the items it produces.
3. Inject it into `get_trending(provider=...)` — no change to the scorer, cache,
   or HTML.
4. (Optional) Combine multiple providers by fetching from each and merging signal
   lists before scoring, or keep a per-marketplace cache file
   (`trending_cache_amazon.json`) and a marketplace selector in the UI.

The scorer's min-max normalization is per-signal and source-agnostic, so a merged
multi-marketplace candidate set scores correctly without changes — though
cross-marketplace watch counts may need per-source normalization if scales differ
(documented here as a known future consideration, out of scope for the eBay-only
first cut).

---

## 10. Flask Integration

### New route in `server.py`

Add a `GET /trending` route alongside the existing `/` and `/analyze`. It follows
the same `sys.path.insert(0, .../src)` + lazy-import pattern already used in
`/analyze`, and the same `try/except → jsonify({"error": ...}), 500` error shape.

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
            "marketplace": "eBay",
            "lookback_days": 60,
            "items": [
                {
                    "rank":        it.rank,
                    "title":       it.title,
                    "url":         it.url,
                    "source":      it.source,
                    "score":       round(it.score, 3),
                    "keyword_rank": it.keyword_rank,
                    "watch_count":  it.watch_count,
                    "sold_rate":    it.sold_rate,
                    "norm_keyword": round(it.norm_keyword, 3),
                    "norm_watch":   round(it.norm_watch, 3),
                    "norm_sold":    round(it.norm_sold, 3),
                }
                for it in items
            ],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
```

### UI tab placement in `index.html`

The current layout is a two-column `<main>`: a `.sidebar` (upload) on the left and
a `.content` (results) on the right. Add a **tab strip** at the top of `.content`
with two tabs: **Matcher** (the existing image-search view, default) and
**Trending** (new).

- **Tab strip:** a small `.tabs` bar of two buttons styled with the existing
  `--accent` / `--muted` / `--border` tokens. The active tab gets the accent
  underline; matches the existing uppercase `.section-label` typography.
- **Trending panel:** a new `<div id="trendingPanel">` (hidden until the tab is
  selected). Reuse the existing `table` / `thead` / `tbody` styles. Columns:

  | #   | Title | Source | Score | Watch | Sold rate |
  | --- | ----- | ------ | ----- | ----- | --------- |
  - `#` = `rank`, Title links to `url` (`a.listing-link`, `target="_blank"`),
    Source uses the existing `.source-tag` chip, Score / Watch / Sold-rate are
    plain numeric cells. Optionally reuse the `.sim-bar` component to visualize the
    normalized score.

- **Data load:** on first switch to the Trending tab, `fetch('/trending')`,
  render into `#trendingPanel tbody`, and use the existing `.status-bar` dot for
  loading/success/error states. Cache the response client-side so re-clicking the
  tab doesn't refetch (server-side 3-hour cache already covers staleness).
- The existing `#emptyState` / `#results` toggling stays scoped to the Matcher
  tab; switching tabs shows/hides the two panels via a `.hidden` class.

No new HTTP libraries or build step — same vanilla-JS `fetch` approach already in
the file.

---

## 11. Implementation Order

Build bottom-up so each layer is testable before the next is added. **Per the
project Testing Policy, add checks to `src/test_setup.py` and run
`python src/test_setup.py` (expect `N passed, 0 failed`) after each step, and per
the Git Commit Policy, commit after each green step with a one-sentence message.**

1. **Data model** — add `KeywordSignal`, `WatchSignal`, `SoldSignal`,
   `TrendingItem`, and the `TrendingProvider` protocol to `src/models.py`.
   _Tests:_ dataclass construction + field defaults.
   _Commit:_ `add trending signal dataclasses and TrendingProvider protocol to models`

2. **Scorer** — `src/trending_scorer.py`: predicate filter, min-max normalization,
   weighted sum, top-10 ranking. Pure/offline — easiest to test first.
   _Tests:_ normalization edge cases (`max==min`, single candidate), missing-signal
   degradation, predicate noise/recency gates, the worked example from Section 6,
   tie-breaking determinism.
   _Commit:_ `add trending scorer with min-max normalization and weighted ranking`

3. **Cache** — `src/trending_cache.py`: `load` / `save` / `acquire_lock` /
   `release_lock` against Redis (`SET ... EX`, versioned keys, lock with its own
   TTL). Add `redis` to `requirements.txt`. No `data/cache/` directory needed —
   Redis is the store.
   _Tests:_ round-trip save→load and TTL/warm-refresh logic against
   **`fakeredis`** (in-process, no network); lock single-flight (`acquire` twice
   → second returns `False`); schema-version bump means an old-version key isn't
   read.
   _Commit:_ `add redis-backed trending cache with versioned keys, 3h ttl, and single-flight lock`

4. **Fetcher** — `src/trending_fetcher.py`: `EbayTrendingProvider` against the
   three eBay endpoints, app-id injection, error handling.
   _Tests:_ JSON→dataclass mapping using captured fixture responses via
   `httpx.MockTransport`; `_validate_key()` guard with `app_id=""`. No real network.
   _Commit:_ `add ebay trending provider implementing TrendingProvider protocol`

5. **Orchestration + Flask route** — wire `get_trending(provider, client, ...)`
   (cache lookup → warm-refresh check → single-flight fetch) and add
   `GET /trending` to `server.py`.
   _Tests:_ `get_trending()` with a fake provider (in-process stub) + `fakeredis`
   end-to-end through scorer + cache, asserting top-10 order, a cache hit skips
   the provider, and TTL < 15 min triggers a refresh.
   _Commit:_ `add /trending flask route backed by ebay trending provider`

6. **UI tab** — add the Trending tab + panel + fetch logic to `index.html`.
   _Tests:_ none in `test_setup.py` (front-end); verify manually by running the
   Flask app and switching tabs.
   _Commit:_ `add trending tab to the ui`

> **Note on `CLAUDE.md`:** it currently describes `pipeline.py` as "the only
> remaining stub," but `pipeline.py` is in fact fully implemented. Worth updating
> that line when this feature lands, and adding the new modules + `EBAY_APP_ID`
> requirement to the architecture section.
