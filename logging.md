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

