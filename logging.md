# Implementation Log — Trending v3 (Vintage-Clothing Precision Filter)

Incremental changelog for implementing the **schema-v3** precision filter described
in `notes/trending_feature_arch.md` §0.8. The v2 pipeline was already complete and
green (127 passed). v3 re-scopes the feature from generic trending to **vintage
clothing only**, with category-grouped, per-category-ranked output.

Implementation order (per the doc's recommendation): `models.py` →
`trending_scorer.py` → `trending_fetcher.py` → `trending_cache.py` →
`server.py`/`index.html` → `test_setup.py`. Each step is committed only after the
full test suite (`python src/test_setup.py`) is green, per the Testing Policy.

Baseline before v3: **127 passed, 0 failed**.

---

<!-- entries appended below, newest last -->

## 1 — `models.py`: add `category` to the data model (§0.8.10)

- `TrendingItem` gains `category: str = ""` — the vintage-clothing category of the
  row (e.g. `"Denim"`), sourced from the winning seed. With per-category scoring,
  `rank` is now the within-category rank.
- `KeywordSignal` gains `category: str = ""` — set by the fetcher from
  `CATEGORY_SEED_MAP` so the category rides the existing keyword join into the
  scorer (keeps the fetcher→scorer boundary decoupled: the scorer never imports the
  fetcher's seed map).
- **Placement note:** both fields are appended *last* with a `""` default rather
  than inserted after `source` as the doc's illustrative snippet shows. Dataclass
  rules forbid a defaulted field before the existing non-default fields, and a `""`
  default matches the doc's own semantics ("empty only for legacy/uncategorized
  rows") and the existing `title`/`url`-at-end pattern on `KeywordSignal`. This also
  keeps every intermediate commit green.
- Tests: Section 12 now asserts the `category` default and a category-set
  construction for both dataclasses.
- **127 → 129 passed, 0 failed.**

## 2 — `trending_scorer.py`: two-pass filter + per-category scoring (§0.8.3–§0.8.9)

- Added `CATEGORY_TAXONOMY` (8 categories, each with inclusion-keyword lists) and
  `EXCLUDED_ITEM_TYPES` (non-garment/accessory blacklist). `leggings`/`legging` are
  in the Pants & Bottoms include list and kept; `tights` stays excluded (§0.8.4).
- `_passes_category_filter(title, category)` runs **inclusion** (loose case-folded
  substring) then **exclusion** (strict, **word-boundary** regex). The pre-compiled
  `_EXCLUDED_RE` uses `\b` anchors so `bootcut`≠`boot`, `baggy`≠`bag`, `capri`≠`cap`,
  `earring`≠`ring`; longest terms first so `bucket hat` wins over `hat`.
- `score_trending` rewritten: build candidates (category rides in on the keyword
  signal), filter, **group by category**, then normalize/weight/rank *within each
  category* (`_rank_pool`). Returns a flat `list[TrendingItem]`, each tagged with
  its category, `rank` = within-category position, top `TOP_N_PER_CATEGORY` (5) per
  category. Weights (2/2/1) and min-max mechanics unchanged — only the *scope* of
  min/max moved from global to per-category. Categories emit in taxonomy order.
- `select_enrichment_ids(keyword_signals, per_category=15)` (§0.8.7): applies the
  category filter to keyword signals, groups, and returns the top-15-by-rank ids per
  category — this is what bounds `getItem` (~15×8 ≈ 120 calls) regardless of intake.
- Orchestrator (`_fetch_and_cache`/`_maybe_refresh`) refactored to a shared
  `_fetch_and_score` that calls `select_enrichment_ids` before enrichment. The
  budgeting lives here (the orchestrator already lives in `trending_scorer`), so the
  fetcher stays a dumb network boundary and never imports the scorer — preserving the
  module-boundary rule (modules import types from `models`, never each other).
- **Design note on the doc's "limit getItem in trending_fetcher.py":** functionally
  the orchestrator decides which ids get enriched. Putting the filter-driven
  selection in the scorer (which owns `CATEGORY_TAXONOMY`) keeps the fetcher
  decoupled; coupling the fetcher to the taxonomy would violate the architecture.
- Tests: Section 13 reworked for v3 — category-aware `_kw` helper; new checks for
  off-category/excluded-accessory drops, per-category top-N, cross-category
  grouping, within-category rank, and a genuine 3-item score tie resolved by sold
  quantity. `_min_max`/`_passes_predicate` unit checks unchanged. Section 16
  orchestration passes unchanged (stub now yields categorized candidates).
- **129 → 130 passed, 0 failed.**

## 3 — `trending_fetcher.py`: 50 results/seed + category tagging (§0.8.5)

- `max_results` default `10 → 50` so each of the ~30 category seeds contributes a
  deeper candidate pool (~1,500 pre-dedup, target ~1,000 unique).
- `fetch_keyword_signals` now sets `KeywordSignal.category = CATEGORY_SEED_MAP[seed]`
  for each surfaced item. Because the existing best-rank-wins dedup rebuilds the
  whole `KeywordSignal` when a lower rank is found, the category automatically
  follows the **winning seed** — no extra bookkeeping. (`CATEGORY_SEED_MAP` and the
  v3 seed list already lived in this file from a prior doc-sync commit.)
- `getItem` budgeting is *not* done here — the orchestrator passes only the
  top-15-per-category ids (see module 2); the fetcher fetches exactly what it's given.
- Tests: Section 15 adds category-from-seed tagging and category-follows-best-rank
  on cross-seed dedup. Existing offline mock-transport checks unchanged.
- **130 → 132 passed, 0 failed.**

## 4 — `trending_cache.py`: schema key bump `v2 → v3` (§0.8.11)

- `SCHEMA_VER = "v3"`. Because the schema version is baked into the key
  (`trending:ebay:v3:ranked` / `:raw` / `:lock`), the model change (added
  `category`) is a one-line rename: new writes go to the `v3` prefix, stale `v2`
  keys are never read again and age out within the 3-hour TTL — no migration code.
- `category` added to the raw-snapshot keyword rows (`kw_row`) so the cached `:raw`
  signal dump is faithful for offline re-scoring. `TrendingItem` rows round-trip
  `category` automatically via `asdict`/`TrendingItem(**d)`.
- Tests: Section 14 sample fixtures now carry a `category`, and the round-trip check
  asserts it survives. The schema-version-in-key and old-key-not-read checks remain
  version-agnostic and pass against `v3`.
- **132 passed, 0 failed** (no net new checks; existing ones strengthened).

## 5 — `server.py` + `index.html`: emit & group by category (amends §10)

- `server.py` `/trending` JSON now includes `"category": it.category` per row.
- `index.html`:
  - Label changed to "Trending vintage clothing on eBay — by category".
  - `loadTrending()` renders a category sub-header row (`.trend-category-row`,
    `colspan=6`) whenever the category changes as it walks the server-grouped list
    (Option A from §0.8.9 — flat category-tagged rows, grouped client-side). `rank`
    is shown as the within-category position.
  - Status line reports item count *and* number of categories.
  - Added `.trend-category-row` CSS (uppercased accent-colored group header).
- No new HTTP libs / build step (same vanilla-`fetch` approach). No automated UI
  tests exist; full suite remains green and `server.py` parses clean.
- **132 passed, 0 failed.**

## 6 — `test_setup.py`: dedicated v3 filter section (§0.8.12)

- Added `=== Section 17: trending — v3 precision filter ===` with 10 checks:
  - `CATEGORY_SEED_MAP` is total over `DEFAULT_SEED_QUERIES`, every category has
    ≥1 seed, and the fetcher's map agrees with the scorer's `CATEGORY_TAXONOMY["seeds"]`.
  - Inclusion pass (garment keyword passes / off-category dropped); unknown category
    rejected.
  - Exclusion word boundaries: `bootcut`/`baggy` survive, `jacket belt` and bare
    `leather belt` dropped.
  - `leggings` kept vs `tights` dropped (the deliberate edge case).
  - Category flows through scoring into the `TrendingItem`.
  - Per-category normalization protects a low-volume category (Tops top item norms
    to 1.0 even next to a 10,000-unit Denim category).
  - `select_enrichment_ids` caps ids at 15/category and keeps the best-ranked ones.
  - `get_trending` end-to-end bounds `getItem` to ≤15 per category.
  - Cache key carries `v3` and round-trips `category`.
- **132 → 142 passed, 0 failed.** (Net +15 checks over the v2 baseline of 127.)

## 7 — live Redis smoke test + Change Log Policy

- **Online integration test against a live Redis** (closes the "not yet done" gap
  for the cache path). Ran a real `redis:7-alpine` container and exercised the v3
  orchestration against it via `experiments/live_trending_smoke.py` — an in-process
  vintage-clothing stub provider (no eBay creds needed) driving `get_trending`:
  - cache **miss** → fetch+score+save to real Redis; the two noise fixtures (a
    `jacket belt` and a `ceramic mug`) were correctly dropped by the two-pass filter;
  - real key `trending:ebay:v3:ranked` present with a 10800s (3h) TTL;
  - output grouped per category (Denim/Tops/Outerwear) with within-category ranks;
    a low-volume Tops item still scored 5.00 (per-category normalization, live);
  - cache **hit** → second call served from Redis without invoking the provider.
- **End-to-end Flask check:** started `server.py` against the same live Redis and
  `curl`ed `GET /trending` → **HTTP 200** returning the category-grouped JSON (each
  row carrying `category`), served from the warm cache — proving the
  server↔Redis↔scorer↔JSON path. The eBay network leg itself was *not* exercised
  (no `EBAY_CLIENT_ID`/`SECRET` in `.env`); the cache-hit route reaches eBay only on
  a cold/expired key.
- Added `experiments/live_trending_smoke.py` as a reusable manual harness (asserts +
  exits non-zero on failure). It is intentionally **not** part of `src/test_setup.py`
  (that suite is strictly offline; this one needs a live Redis).
- **`CLAUDE.md`:** added a **Change Log Policy** requiring a `logging.md` entry in the
  same commit as every feature/meaningful change (this entry is the first to follow
  it).
- Offline suite unchanged: **142 passed, 0 failed.**

## 8 — live end-to-end test against the REAL eBay Browse API

- After eBay OAuth credentials were added to `.env`, ran a genuine cold-cache fetch
  against the live eBay Browse API + live Redis via `experiments/live_trending_ebay.py`
  (drives the real `EbayTrendingProvider`, not a stub). This closes the eBay-network
  gap left open in module 7.
- Result: minted an application token, ran **38 seed searches across 8 categories**,
  enriched the top-15-per-category with `getItem`, and returned **40 items (8×5)** in
  **~109s** (~150 live API calls — comfortably within the 5,000/day budget). The
  per-category two-pass filter held up on real listings (denim, tees, jackets,
  dresses, etc. landed in the right buckets; accessories/off-category titles were
  excluded). Output cached at `trending:ebay:v3:ranked` (ttl 10800s); the second call
  was a cache hit in ~1ms.
- The scripts live under `experiments/` which is **gitignored** (repo scratch area),
  so they are documented here rather than committed. Recreate the run with a live
  Redis (`docker run -d -p 6379:6379 redis:7-alpine`) and
  `python experiments/live_trending_ebay.py`.
- No source changes in this step — verification only. Offline suite still
  **142 passed, 0 failed**; the v3 feature is now validated against real eBay data.

