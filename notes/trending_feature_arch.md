# Trending Items — Feature Architecture

> **Status:** Core pipeline (v2) and the v3 precision filter (vintage clothing
> categories, two-pass filtering, per-category scoring) are both **fully implemented
> and tested (142 passed, 0 failed)**. Online integration testing with real
> credentials and live Redis has not been done.

---

## v3 implementation status (see §0.8 for full design)

The v3 precision filter is implemented; see `logging.md` for the incremental change
log. Summary of what landed, per file:

- **`models.py`** — added `category: str` to `TrendingItem` (§0.8.10) and to
  `KeywordSignal` (so the category rides the keyword join into the scorer without
  coupling the scorer to the fetcher's seed map). ✅
- **`trending_fetcher.py`** — `max_results` `10 → 50` per seed; each `KeywordSignal`
  is tagged with its category from `CATEGORY_SEED_MAP`, following the best-ranked
  (winning) seed on cross-seed dedup (§0.8.5). ✅
- **`trending_scorer.py`** — added `CATEGORY_TAXONOMY` + `EXCLUDED_ITEM_TYPES`;
  `_passes_category_filter` runs the two-pass inclusion/exclusion filter
  (§0.8.3–§0.8.4, exclusion is word-boundary matched); `score_trending` normalizes and
  ranks **within each category** and returns a flat category-tagged list
  (§0.8.8–§0.8.9). The top-15-per-category `getItem` budgeting (§0.8.7) is driven by
  `select_enrichment_ids` from the orchestrator (keeps the fetcher decoupled from the
  taxonomy). ✅
- **`trending_cache.py`** — `SCHEMA_VER` `"v2" → "v3"` (§0.8.11); `category` added to
  the raw keyword snapshot. ✅
- **`server.py` / `index.html`** — `/trending` emits `category`; the UI groups rows
  by category with per-category sub-headers (§0.8.9, Option A). ✅
- **`test_setup.py`** — Section 17 covers the v3 filter surface (§0.8.12). ✅

**Data flow summary (once v3 is implemented):**

- `fetch_keyword_signals` runs ~30 category-specific seed queries (e.g. `"flare jeans vintage"`,
  `"boxy hoodie vintage"`) and tags each result with its category via `CATEGORY_SEED_MAP`
- `fetch_volume_signals` / `fetch_sold_signals` call `getItem` for the top 15 per category only
- `score_trending` runs two-pass filtering (include if title matches category keywords → exclude
  if title contains a non-clothing term like `belt`, `shoe`, `bag`) then normalizes and ranks
  within each category independently — output is a flat `list[TrendingItem]` where each item
  carries a `category` field and `rank` is within-category
- Cache stores the result under `trending:ebay:v3:ranked`; UI groups rows by `category`

**Recommended implementation order:** `models.py` → `trending_scorer.py` (pure/offline,
easiest to test) → `trending_fetcher.py` → `trending_cache.py` → `server.py`/`index.html`

---

> **Migration note:** the original design used the now-deprecated Merchandising
> (`getMostWatchedItems`) and Finding (`findCompletedItems`) APIs. Those were
> retired by eBay, so the feature was migrated to the Browse API. The watch-count
> signal has no Browse equivalent and was replaced by **sold volume**
> (`estimatedSoldQuantity`); see §1/§3.

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

> **Later revision (v2 → v3).** A subsequent precision pass re-scoped the feature
> from generic trending to **vintage clothing only**, with category-grouped output.
> The deltas below are layered *on top of* the v2 row above; see **§0.8** for the
> full design.

| Concern | **v2 (generic trending)** | **v3 (vintage-clothing precision filter)** |
| ------- | ------------------------- | ------------------------------------------ |
| **Seeds** | 2 broad terms (`clothes`, `vintage clothing`) | ~30 **category-specific** vintage seeds (`CATEGORY_SEED_MAP`), §0.8 |
| **Candidate volume** | a few hundred | **top ~1,000** across all seeds (≈30 seeds × ~50 results, pre-dedup) |
| **Filtering** | noise gate only | **two-pass**: category whitelist (inclusion) → non-clothing blacklist (exclusion), §0.8 |
| **Grouping** | flat top-10 | **per-category top-N** (e.g. top 5 × 8 categories), §0.8 |
| **`getItem` budget** | one per unique candidate (hundreds) | **top 15 per category only** (~120 calls), §0.8 |
| **Normalization** | global min-max | **per-category** min-max (a crop top doesn't compete with flare jeans), §0.8 |
| **Model** | `TrendingItem` (no category) | `TrendingItem` **+ `category: str`** |
| **Cache schema** | `trending:ebay:**v2**:*` | `trending:ebay:**v3**:*` (model changed → key bump) |

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

> **v3 supersessions (see §0.8).** The precision-filter pass re-scopes several
> sections beyond the v2 migration:
>
> | Section | Status under v3 |
> | ------- | --------------- |
> | §1 Feature Summary | **Superseded** — feature is now **vintage clothing**, output is **per-category top-N**, not a flat top-10 (§0.8) |
> | §3.1 Keyword signal | **Superseded** — the example seeds (`electronics, sneakers, …`) are replaced by the §0.8 category taxonomy; seeds now drive category assignment |
> | §4 Data Model | **Amended** — `TrendingItem` gains `category: str` (§0.8); other v2 amendments still apply |
> | §6 Scoring Algorithm | **Amended** — min-max normalization is now **per-category**, not global (§0.8); the join/weight/rank mechanics are otherwise unchanged |
> | §7 Caching | **Amended** — key version is now **`v3`** (§0.8), e.g. `trending:ebay:v3:ranked` |
> | §8 Predicate Filters | **Superseded** — the noise gate is replaced by the §0.8 two-pass inclusion/exclusion filter (category whitelist → non-clothing blacklist) |
> | §10 Flask + UI | **Amended** — the route/UI now emit and render a `category` field and group rows per category (§0.8) |

---

## 0.8  Precision Filter — Vintage Clothing Category Taxonomy (Schema v3)

> **Read §0 first, then this.** This section re-scopes the feature from *generic
> trending* to **vintage clothing only**, and changes the output from a flat
> top-10 to **per-category top-N**. Where it conflicts with §1–§10 (and with the
> v2 deltas in §0.1–§0.7), **§0.8 wins**. The network/auth layer (OAuth, Browse
> endpoints, the memoized `getItem`) and the module boundaries are **unchanged** —
> this is a *filtering, grouping, and scoring-scope* change, not a rewrite.

### 0.8.1 Why category-specific seeds beat generic seeds

The v2 design used two broad seeds (`clothes`, `vintage clothing`) and ranked the
union globally. That has three problems for a vintage-clothing-focused product:

1. **Low precision.** A broad `clothes` Best-Match page is dominated by whatever
   eBay's relevance model surfaces that day — often new/fast-fashion items, plus
   non-clothing (belts, bags, shoes) that eBay files under apparel-adjacent
   categories. There is no signal telling us *what kind* of garment each result is.
2. **No grouping.** A flat top-10 mixes a hoodie, a pair of jeans, and a dress with
   no way to browse "show me trending flare jeans." Users of a vintage-clothing
   tool think in **garment categories**, not a global leaderboard.
3. **Unfair cross-category competition.** Global min-max normalization makes a
   high-volume staple (e.g. `vintage Levi's`) crowd out an entire low-volume but
   genuinely-trending niche (e.g. `vintage crop top`). The popular category wins
   every slot.

**The fix: make the seed itself the category label.** Each seed is a *specific*
vintage-garment query (`flare jeans vintage`, `boxy hoodie vintage`, …). Because
a seed is category-specific, **every candidate it surfaces inherits that seed's
category for free** — no separate classifier, no ML, no eBay category-ID mapping.
This is the cheapest possible "classifier": the query *is* the label. Grouping,
precision, and per-category scoring all fall out of this one decision.

**Trade-offs we accept:**

- A garment can legitimately match multiple seeds (a `vintage denim jacket` also
  matches `vintage Levi's` if it's Levi's branded). We resolve this with a
  **best-rank-wins** tie-break (§0.8.5): the category of the seed under which the
  item ranked highest is assigned. This is deterministic and favors the query the
  item is *most* relevant to.
- Seeds are a curated, hand-maintained list. New vintage trends (a new silhouette)
  require adding a seed + a `CATEGORY_SEED_MAP` entry. We accept manual curation in
  exchange for precision and zero classifier infrastructure.
- The query text can still surface off-category noise (a `flare jeans vintage`
  search may return a belt that mentions "flare"). That residue is caught by the
  **second-pass exclusion blacklist** (§0.8.4), not by the seed alone.

### 0.8.2 Category taxonomy (`CATEGORY_TAXONOMY`)

Eight categories, each with its seed queries and an **inclusion keyword list** (the
whitelist a title must hit — see §0.8.4). Seeds live in
`trending_fetcher.DEFAULT_SEED_QUERIES`; the seed→category map is
`trending_fetcher.CATEGORY_SEED_MAP`. The inclusion keywords are a scorer-side
concern (proposed `trending_scorer.CATEGORY_TAXONOMY`); they are listed here so the
two halves stay in sync.

```python
# Proposed structure (scorer-side). Seeds mirror CATEGORY_SEED_MAP in the fetcher.
CATEGORY_TAXONOMY = {
    "Hoodies & Sweatshirts": {
        "seeds": [
            "boxy hoodie vintage", "oversized crewneck vintage",
            "vintage zip up hoodie", "vintage pullover hoodie",
        ],
        # title must contain >= 1 of these (case-insensitive substring)
        "include": ["hoodie", "hooded", "sweatshirt", "crewneck", "crew neck",
                    "pullover", "sweater", "jumper"],
    },
    "Denim": {
        "seeds": [
            "flare jeans vintage", "wide leg jeans vintage", "baggy jeans vintage",
            "vintage Levi's", "mom jeans vintage", "vintage straight leg jeans",
            "carpenter jeans vintage",
        ],
        "include": ["jeans", "denim", "levi", "levis", "wrangler", "lee",
                    "flare", "bootcut", "baggy", "mom jean", "carpenter"],
        # NOTE: "denim jacket"/"denim skirt" titles are claimed by Outerwear/Skirts
        # respectively via best-rank-wins (§0.8.5), not double-counted here.
    },
    "Tops": {
        "seeds": [
            "vintage band tee", "vintage graphic tee", "vintage oversized t-shirt",
            "vintage polo shirt", "vintage rugby shirt", "vintage crop top",
        ],
        "include": ["tee", "t-shirt", "tshirt", "t shirt", "shirt", "top",
                    "polo", "rugby", "blouse", "tank"],
    },
    "Outerwear": {
        "seeds": [
            "vintage varsity jacket", "vintage denim jacket", "vintage leather jacket",
            "vintage windbreaker", "vintage coach jacket", "vintage bomber jacket",
        ],
        "include": ["jacket", "coat", "windbreaker", "bomber", "varsity",
                    "parka", "anorak", "blazer", "overcoat"],
    },
    "Pants & Bottoms": {          # non-denim trousers
        "seeds": [
            "vintage cargo pants", "vintage corduroy pants",
            "vintage track pants", "vintage pleated trousers",
        ],
        "include": ["pants", "trousers", "cargo", "corduroy", "cords",
                    "chino", "slacks", "track pant"],
    },
    "Dresses": {
        "seeds": [
            "vintage slip dress", "vintage mini dress",
            "vintage maxi dress", "vintage sundress",
        ],
        "include": ["dress", "sundress", "gown", "frock"],
    },
    "Skirts": {
        "seeds": [
            "vintage denim skirt", "vintage mini skirt",
            "vintage midi skirt", "vintage pleated skirt",
        ],
        "include": ["skirt", "skort"],
    },
    "Sets": {
        "seeds": [
            "vintage matching set", "vintage tracksuit", "vintage two piece set",
        ],
        "include": ["set", "tracksuit", "two piece", "two-piece", "co-ord",
                    "coord", "matching set"],
    },
}
```

> **Keyword-list design notes.** Inclusion lists are deliberately *generous*
> (synonyms, common brand names, hyphen/space variants of the same word) so we
> don't drop a genuine garment on a wording quirk. Precision is recovered by the
> exclusion pass (§0.8.4), which is the safer place to be strict: a false
> *inclusion* merely lets a borderline garment compete within its category, while a
> false *exclusion* silently deletes a real listing. The two passes are tuned with
> that asymmetry in mind — **loose include, strict exclude**.

### 0.8.3 Two-pass filtering algorithm

Each candidate (already tagged with a `category` from its best seed, §0.8.5) runs
through two ordered gates *before* scoring. A candidate must pass **both**.

```
for each candidate c with assigned category K = c.category:
    title = c.title.casefold()

    # ── Pass 1: inclusion (category whitelist) ───────────────────────────────
    # The title must contain >= 1 keyword from K's include-list. This confirms the
    # item really is the kind of garment the seed promised (the seed surfaced it,
    # but Best Match can drift). Reject if it matches none.
    if not any(kw in title for kw in CATEGORY_TAXONOMY[K]["include"]):
        drop(c); continue

    # ── Pass 2: exclusion (non-clothing blacklist) ───────────────────────────
    # Drop anything whose title names a non-garment item type, even if it slipped
    # past Pass 1 (e.g. "leather jacket belt", "denim skirt + matching bag").
    if any(bad in title for bad in EXCLUDED_ITEM_TYPES):
        drop(c); continue

    keep(c)
```

**Ordering rationale.** Inclusion first, exclusion second. Inclusion is the cheap,
high-selectivity gate (most off-category noise dies here); exclusion is the
targeted clean-up for accessory contamination that *shares* a garment keyword
(a "jacket **belt**" passes the `jacket` include but must die on the `belt`
exclude). Running exclusion second means it always gets the final say.

> **Word-boundary caution.** Naïve substring matching has the classic
> `"scarf"` ⊂ `"scarface"`, `"ring"` ⊂ `"earring/herringbone"`, `"cap"` ⊂
> `"capri/escape"` collision risk. The implementation MUST match on **word
> boundaries** (token/`\b` match, not bare `in`) for the short, collision-prone
> exclusion terms. The pseudo-code uses `in` for readability only; see the
> per-term notes in §0.8.4.

### 0.8.4 `EXCLUDED_ITEM_TYPES` (non-clothing blacklist) + rationale

These are item *types* that are not garments (or are accessory categories the
product intentionally omits). Each is dropped on a **word-boundary** match.

| Excluded term(s) | Why excluded | Edge case / boundary note |
| ---------------- | ------------ | ------------------------- |
| `belt` | Accessory, not a garment. | Watch `"belted"` (a coat *can* be belted) — do **not** strip belted dresses/coats; match the standalone noun `belt`/`belts` only, and only when no garment keyword dominates. |
| `shoe`, `shoes`, `boot`, `boots`, `sneaker`, `sneakers`, `heel`, `heels`, `loafer`, `sandal` | Footwear — explicitly out of scope. | `"bootcut"` (a denim cut) must **not** trip `boot` — word-boundary match is mandatory here. `"heel"` similarly must not hit `"wheeler"`. |
| `bag`, `purse`, `handbag`, `backpack`, `tote`, `clutch`, `wallet` | Bags/carry goods — accessories. | `"baggy"` (jeans) must **not** trip `bag` — boundary match required. |
| `hat`, `cap`, `beanie`, `bucket hat`, `visor` | Headwear — accessory. | `"cap"` collides with `capri`, `escape`, `caps` brand tags → boundary match; prefer matching `cap`/`caps`/`baseball cap`. |
| `scarf`, `scarves`, `bandana`, `gloves`, `mittens` | Cold-weather accessories. | `"scarf"` ⊂ `"scarface"` → boundary match. |
| `jewelry`, `jewellery`, `necklace`, `bracelet`, `ring`, `earring`, `earrings`, `brooch`, `pin`, `pendant` | Jewelry — accessory. | `"ring"` ⊂ `"earring"`, `"herringbone"`, `"spring"` → boundary match is critical here. Consider dropping bare `ring`/`pin` and relying on `necklace`/`earring`/`bracelet` if collisions prove noisy. |
| `watch`, `watches` | Timepiece — accessory. | `"watch"` appears in marketing copy ("must-watch"); rare in titles but boundary-match anyway. |
| `sunglasses`, `glasses`, `eyewear` | Eyewear — accessory. | Low collision risk. |
| `keychain`, `lanyard`, `patch`, `sticker`, `keyring` | Merch/novelty, not wearable garments. | `"patch"` can appear on a varsity jacket ("chenille **patch**"); if it over-deletes, scope the exclusion to *standalone* patch listings (title starts with / is dominated by `patch`). |
| `socks`, `sock` | Hosiery accessory, sub-garment; out of scope for the trending grid. | Distinct from `leggings` (see below). |
| `tights`, `pantyhose`, `stockings`, `nylons` | **Excluded.** Hosiery / undergarment-adjacent — not an outerwear garment people browse as "trending vintage clothing." Closer to socks than to pants. | Contrast with `leggings` below — this is the deliberate edge-case split. |

**The `leggings` decision (deliberately *not* excluded).** `leggings` are a genuine
outerwear bottom (athleisure/streetwear staple), unlike `tights`/`pantyhose` which
are hosiery worn *under* other garments. We therefore **keep** `leggings` and
**exclude** `tights`. To make `leggings` actually rankable rather than orphaned,
add it to the **Pants & Bottoms** inclusion list (`"leggings"`, `"legging"`) — and,
if desired, a `"leggings vintage"` seed mapped to that category. The principle: the
blacklist is for *non-garments and sub-garments worn underneath*, not for "tight"
silhouettes. `tights` ≈ hosiery → out; `leggings` ≈ trousers → in.

> **General principle for the blacklist.** Exclude an item only when its title's
> *dominant noun* is a non-garment. A garment that merely *mentions* an accessory
> ("hoodie with matching beanie", "dress with belt") is still a garment and should
> survive — which is why the boundary-matched exclusion runs *after* a positive
> inclusion match, and why ambiguous terms (`patch`, `pin`, `belt`) are scoped to
> standalone/dominant occurrences rather than any-substring.

### 0.8.5 Category assignment + cross-seed dedup (best-rank-wins)

A single item can surface under multiple seeds (cross-seed dedup already exists in
`fetch_keyword_signals`, keeping the best/lowest rank). v3 extends that join to
also carry the **category of the winning seed**:

```
for each (seed, position, item) in all seed searches:
    K = CATEGORY_SEED_MAP[seed]
    if item.id unseen OR position < best[item.id].rank:
        best[item.id] = {rank: position, category: K, seed: seed, ...}
# → each unique item ends up tagged with the category of the seed under which it
#   ranked highest. Deterministic; favors the most-relevant query.
```

This keeps category assignment free (it rides the existing dedup) and resolves the
multi-seed ambiguity from §0.8.1 without a tiebreak table.

### 0.8.6 API rate budget

Per refresh cycle:

| Stage | Calls |
| ----- | ----- |
| OAuth token mint | ~1 (cached 2h, often 0 on warm cycles) |
| `item_summary/search` — one per seed | **~30** (1 per seed; was 20 in the original estimate, scaled to the full taxonomy) |
| `getItem` — **only top 15 per category** (§0.8.7), ~8 categories | **~120** |
| **Total per refresh** | **~150** |

Cap on refreshes: the 3-hour cache (§7) ⇒ at most **8 refreshes/day**.
`8 × ~150 ≈ 1,200 calls/day` — comfortably under the default **5,000 calls/day**
Browse limit, with headroom for the OAuth mint and ad-hoc manual refreshes. The
dominant cost is `getItem`; §0.8.7 is what keeps it bounded.

> If the taxonomy grows, the budget scales as `seeds + 15×categories` per refresh.
> Even doubling to ~60 seeds / 16 categories (`60 + 240 = 300`/refresh →
> `2,400`/day) stays under the cap.

### 0.8.7 `getItem` optimization — top 15 per category only

Naïvely calling `getItem` for every unique candidate (~1,000) would blow the rate
budget (1,000 × 8 refreshes = 8,000/day > limit) and add latency for items that
will never make the per-category top-N anyway.

**Optimization:** after the keyword search + dedup + two-pass filter, **sort each
category's surviving candidates by keyword rank (lowest = best) and fetch `getItem`
only for the top 15 per category.** Rationale:

- The volume/sell-through signals (`getItem`) only matter for items that are
  *already* near the top on keyword rank — a candidate ranked #480 in its category
  isn't going to win a top-5 slot even with perfect sold data.
- Keyword rank is a strong prior (it's eBay's own Best Match popularity ordering),
  so the top-15-by-rank pool reliably contains the eventual top-N.
- This bounds `getItem` at `15 × ~8 = ~120` calls regardless of how many raw
  candidates we pulled — decoupling the API cost from the 1,000-candidate intake.

Candidates outside the top-15 still appear in the raw snapshot (and can score on
keyword rank alone if they somehow surface), but they are not enriched with
sold/volume data. This is the v3 analog of "keyword-surfaced items still count"
(§0.5), scoped per category.

### 0.8.8 Per-category scoring (normalize within category, not globally)

The v2 scorer min-max normalized each signal **globally** across all candidates.
v3 normalizes **within each category**:

```
for each category K:
    pool = surviving candidates with category == K
    for each signal (keyword / volume / sell-through):
        min-max normalize over `pool` only        # NOT over all categories
    score = 2·norm_keyword + 2·norm_volume + 1·norm_sold   # weights unchanged
    rank pool by score desc; take top-N (e.g. 5)
```

**Why per-category.** Global normalization let a high-volume category (e.g. `Denim`,
where `vintage Levi's` moves huge units) dominate the absolute scale, so a
genuinely-trending but lower-volume `Tops` item normalized to near-zero and never
ranked. Normalizing *within* a category means each garment competes only against
its peers — "is this a trending **crop top**?" is answered relative to other crop
tops, not relative to Levi's. The **weights (2/2/1) and the min-max mechanics are
unchanged** (§6); only the *scope* of the min/max bounds changes from global to
per-category.

> Consequence: scores are **not comparable across categories** (each category's top
> item scores ~5.0). That's intended — the output is grouped by category, so
> cross-category score comparison is meaningless. If a future "global trending"
> view is wanted, keep a second global-normalized pass; don't reinterpret the
> per-category scores.

### 0.8.9 Output shape — per-category top-N

Output changes from a flat `list[TrendingItem]` (top 10) to **grouped, top-N per
category**. Two equivalent encodings; pick one and keep the cache/UI consistent:

```python
# Option A — flat list, every row carries its category (simplest; minimal churn):
list[TrendingItem]            # each TrendingItem now has .category; rows for all
                              # categories concatenated, ~N×8 rows. UI groups by
                              # .category. Scoring/rank are per-category (§0.8.8),
                              # so .rank is the rank *within* its category.

# Option B — explicit grouping:
dict[str, list[TrendingItem]] # {"Denim": [...top N...], "Tops": [...], ...}
```

**Recommendation: Option A** (flat list of category-tagged rows). It is the
smallest change to the existing `list[TrendingItem]` contract that the cache,
`/trending` route, and JSON shape already speak — the UI does the grouping
client-side off `.category`. `rank` becomes the **within-category** rank (1..N per
category).

### 0.8.10 `TrendingItem` model change (`+ category`)

Add one field to the `TrendingItem` dataclass in `models.py`:

```python
@dataclass
class TrendingItem:
    item_id:  str
    title:    str
    url:      str
    source:   str
    category: str        # NEW (v3): vintage-clothing category, e.g. "Denim".
                         # Sourced from the winning seed via CATEGORY_SEED_MAP
                         # (§0.8.5). Empty "" only for legacy/uncategorized rows.
    rank:     int        # v3: rank *within* the item's category (1..N), not global
    score:    float
    # ... existing raw + norm_* fields unchanged ...
```

This is a model change, so it **forces a cache key bump** (§0.8.11). The
`/trending` route and `index.html` add a `category` column / grouping (amends §10).

### 0.8.11 Cache schema bump → `v3`

Per §7.3, any `TrendingItem`/signal field change is a one-line key rename. Adding
`category` (§0.8.10) ⇒ bump `trending_cache.SCHEMA_VER` from `"v2"` to `"v3"`:

```
trending:ebay:v3:ranked    # JSON list[TrendingItem] (category-tagged, per-cat top-N)
trending:ebay:v3:raw       # raw signal snapshot (now includes per-candidate category)
trending:ebay:v3:lock      # single-flight fetch lock (unchanged mechanics)
```

Old `v2:*` keys are never read again and expire on their own within the 3-hour TTL
— no migration, no corrupt-cache branch (§7.3).

### 0.8.12 Test surface (to add in `src/test_setup.py`, when implemented)

Per the Testing Policy, the v3 implementation must add a
`=== Section N: trending — v3 precision filter ===` block covering at minimum:

- `CATEGORY_SEED_MAP` is total over `DEFAULT_SEED_QUERIES` (every seed maps to a
  category; every category in `CATEGORY_TAXONOMY` has ≥1 seed) — the two halves
  stay in sync.
- Inclusion pass: a title with a category keyword passes; a title with none is
  dropped.
- Exclusion pass + word boundaries: `"vintage bootcut jeans"` survives (no `boot`
  false-positive), `"vintage leather belt"` is dropped, `"baggy jeans"` survives
  (no `bag` false-positive), `"vintage cargo pants leggings"` survives, a
  standalone `"vintage tights"` is dropped.
- `leggings` kept vs `tights` dropped (the deliberate edge case).
- Best-rank-wins category assignment when an item matches two seeds.
- Per-category normalization: a low-volume category's top item still normalizes to
  ~1.0 within its pool (not crushed by a high-volume category).
- `getItem` is called for ≤15 items per category.
- Cache round-trips a `category` field and the key carries `v3`.

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
