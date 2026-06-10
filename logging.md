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

