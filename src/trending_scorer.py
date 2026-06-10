from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from models import KeywordSignal, SoldSignal, TrendingItem, VolumeSignal

if TYPE_CHECKING:
    from models import TrendingProvider

# Weights are module-level constants so tests and docs reference one source.
KEYWORD_WEIGHT = 2.0
VOLUME_WEIGHT  = 2.0
SOLD_WEIGHT    = 1.0

# v3 (§0.8): output is grouped per category, top-N within each category, not a
# single global top-10. getItem enrichment is bounded to the top candidates per
# category (§0.8.7) so the API cost is decoupled from the raw candidate volume.
TOP_N_PER_CATEGORY  = 5
GETITEM_PER_CATEGORY = 15

# ── v3 precision filter: vintage-clothing category taxonomy (§0.8.2) ───────────
#
# Each category lists the *inclusion* keywords a candidate's title must contain to
# be confirmed as that kind of garment (the seed surfaced it, but Best Match can
# drift). Inclusion lists are deliberately generous — synonyms, brand names, and
# hyphen/space variants — because a false inclusion only lets a borderline garment
# compete within its own category, whereas a false exclusion silently deletes a
# real listing. Precision is recovered by the strict exclusion pass below.
# The "seeds" mirror trending_fetcher.CATEGORY_SEED_MAP; they live here so the two
# halves stay in sync (a test asserts the mapping is total in both directions).
CATEGORY_TAXONOMY: dict[str, dict[str, list[str]]] = {
    "Hoodies & Sweatshirts": {
        "seeds": [
            "boxy hoodie vintage", "oversized crewneck vintage",
            "vintage zip up hoodie", "vintage pullover hoodie",
        ],
        "include": ["hoodie", "hooded", "sweatshirt", "crewneck", "crew neck",
                    "pullover", "sweater", "jumper"],
    },
    "Denim": {
        "seeds": [
            "flare jeans vintage", "wide leg jeans vintage", "baggy jeans vintage",
            "vintage Levi's", "mom jeans vintage", "vintage straight leg jeans",
            "carpenter jeans vintage",
        ],
        # "flare" dropped — it leaks across categories (flare dresses/skirts/
        # trousers); genuine flare jeans still match on "jeans"/"denim".
        "include": ["jeans", "denim", "levi", "levis", "wrangler", "lee",
                    "bootcut", "baggy", "mom jean", "carpenter"],
    },
    "Tops": {
        "seeds": [
            "vintage band tee", "vintage graphic tee", "vintage oversized t-shirt",
            "vintage polo shirt", "vintage rugby shirt", "vintage crop top",
        ],
        # Bare "top" dropped — too generic; replaced with the specific top styles so
        # only genuine top-garments match, not every "...top" word.
        "include": ["tee", "t-shirt", "tshirt", "t shirt", "shirt", "jersey",
                    "crop top", "tank top", "tube top", "halter top",
                    "polo", "rugby", "blouse", "tank"],
    },
    "Outerwear": {
        "seeds": [
            "vintage varsity jacket", "vintage denim jacket", "vintage leather jacket",
            "vintage windbreaker", "vintage coach jacket", "vintage bomber jacket",
        ],
        # Compound coats (peacoat/raincoat/trench) listed explicitly: word-boundary
        # matching means bare "coat" no longer reaches inside them.
        "include": ["jacket", "coat", "overcoat", "peacoat", "raincoat", "trench",
                    "windbreaker", "bomber", "varsity", "parka", "anorak", "blazer"],
    },
    "Pants & Bottoms": {          # non-denim trousers
        "seeds": [
            "vintage cargo pants", "vintage corduroy pants",
            "vintage track pants", "vintage pleated trousers",
        ],
        # "leggings"/"legging" are kept (a genuine outerwear bottom) — see the
        # leggings-vs-tights decision in §0.8.4. "tights" stays on the blacklist.
        # "cords" dropped — collides with the non-garment sense (cables) and is
        # already covered by "corduroy".
        "include": ["pants", "trousers", "cargo", "corduroy",
                    "chino", "slacks", "track pant", "leggings", "legging"],
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

# Non-garment / accessory item types (§0.8.4). A surviving candidate is dropped if
# its title names one of these. Matched on **word boundaries** (not bare substring)
# so "bootcut" ≠ "boot", "baggy" ≠ "bag", "capri" ≠ "cap", "earring" ≠ "ring", etc.
EXCLUDED_ITEM_TYPES: set[str] = {
    "belt", "belts",
    "shoe", "shoes", "boot", "boots", "sneaker", "sneakers",
    "heel", "heels", "loafer", "sandal",
    "bag", "purse", "handbag", "backpack", "tote", "clutch", "wallet",
    "hat", "cap", "caps", "beanie", "bucket hat", "visor",
    "scarf", "scarves", "bandana", "gloves", "mittens",
    "jewelry", "jewellery", "necklace", "bracelet", "ring",
    "earring", "earrings", "brooch", "pin", "pendant",
    "watch", "watches",
    "sunglasses", "glasses", "eyewear",
    "keychain", "lanyard", "patch", "sticker", "keyring",
    "socks", "sock",
    # Hosiery / undergarment-adjacent — distinct from the kept "leggings" (§0.8.4).
    "tights", "pantyhose", "stockings", "nylons",
}

# Pre-compile one alternation, longest terms first so multi-word phrases ("bucket
# hat") win over their parts. \b anchors keep short collision-prone terms honest.
_EXCLUDED_RE = re.compile(
    r"\b(?:" + "|".join(
        re.escape(t) for t in sorted(EXCLUDED_ITEM_TYPES, key=len, reverse=True)
    ) + r")\b"
)


def _compile_include(words: list[str]) -> "re.Pattern[str]":
    """Build a word-boundary, plural-tolerant inclusion matcher for one category.

    Inclusion used to be a bare substring test (``kw in title``), which silently
    misfires on collisions — "top" ⊂ laptop, "set" ⊂ corset, "lee" ⊂ fleece,
    "cords" ⊂ records, "tee" ⊂ canteen. Matching on word boundaries kills that whole
    class of false-positive while the trailing ``(?:e?s)?`` keeps recall on plurals
    ("jean"→"jeans", "chino"→"chinos", "tee"→"tees", "mom jean"→"mom jeans"). This
    mirrors the exclusion pass's boundary discipline on the include side.
    """
    alts = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
    return re.compile(r"\b(?:" + alts + r")(?:e?s)?\b", re.IGNORECASE)


_INCLUDE_RE: dict[str, "re.Pattern[str]"] = {
    cat: _compile_include(spec["include"]) for cat, spec in CATEGORY_TAXONOMY.items()
}


def _passes_category_filter(title: str, category: str) -> bool:
    """Two-pass filter (§0.8.3): inclusion whitelist, then exclusion blacklist.

    Pass 1 (inclusion, word-boundary + plural-tolerant): the title must contain >= 1
    of the category's include keywords as a whole word — confirms it really is that
    kind of garment without tripping on substrings (see _compile_include).
    Pass 2 (exclusion, strict word-boundary): drop titles whose dominant noun is a
    non-garment (a "jacket belt" passes inclusion on "jacket" but dies on "belt").
    A candidate must pass both; an unknown/empty category fails immediately.
    """
    if category not in _INCLUDE_RE:
        return False
    t = title.casefold()
    if not _INCLUDE_RE[category].search(t):
        return False           # Pass 1: not the promised garment type
    if _EXCLUDED_RE.search(t):
        return False           # Pass 2: a non-garment item type dominates
    return True


def score_trending(
    keyword_signals: list[KeywordSignal],
    volume_signals:  list[VolumeSignal],
    sold_signals:    list[SoldSignal],
    top_n: int = TOP_N_PER_CATEGORY,
) -> list[TrendingItem]:
    """Filter, group by category, normalize/rank *within* each category (§0.8.8).

    Returns a flat ``list[TrendingItem]`` where every row carries its ``category``
    and ``rank`` is the within-category position (1..top_n). Categories appear in
    ``CATEGORY_TAXONOMY`` order. Scores are not comparable across categories — each
    category's top item normalizes to ~max independently (intended; output is
    grouped by category in the UI).
    """
    # Step 0 — build candidate dicts keyed by item_id, carrying the category from
    # the keyword signal (the only signal that knows it, §0.8.5).
    candidates: dict[str, dict] = {}

    for kw in keyword_signals:
        if kw.item_id:
            c = candidates.setdefault(
                kw.item_id, {"item_id": kw.item_id, "title": "", "url": "", "category": ""}
            )
            c["keyword_rank"] = kw.rank
            c["category"] = kw.category
            if kw.title:
                c["title"] = kw.title
            if kw.url:
                c["url"] = kw.url
            c.setdefault("fetched_at", kw.fetched_at)

    for v in volume_signals:
        c = candidates.setdefault(
            v.item_id, {"item_id": v.item_id, "title": v.title, "url": "", "category": ""}
        )
        c["sold_quantity"] = v.sold_quantity
        if v.title:
            c["title"] = v.title
        c.setdefault("fetched_at", v.fetched_at)

    for s in sold_signals:
        c = candidates.setdefault(
            s.item_id, {"item_id": s.item_id, "title": s.title, "url": "", "category": ""}
        )
        c["sold_rate"] = s.sold_rate
        c["last_sold"] = s.last_sold
        if s.title:
            c["title"] = s.title
        c.setdefault("fetched_at", s.fetched_at)

    now = datetime.now(tz=timezone.utc)

    # Step 1 — two-pass category filter + predicate gates, grouped by category.
    pools: dict[str, list[dict]] = defaultdict(list)
    for c in candidates.values():
        cat = c.get("category") or ""
        if not _passes_category_filter(c.get("title") or "", cat):
            continue
        if not _passes_predicate(
            c.get("sold_quantity"),
            c.get("sold_rate"),
            c.get("keyword_rank"),
            c.get("last_sold"),
            c.get("fetched_at"),
            now,
        ):
            continue
        pools[cat].append(c)

    # Steps 2–5 — normalize, weight, rank *within each category* independently.
    items: list[TrendingItem] = []
    for category in CATEGORY_TAXONOMY:          # deterministic category ordering
        pool = pools.get(category)
        if pool:
            items.extend(_rank_pool(pool, category, top_n))
    return items


def _rank_pool(pool: list[dict], category: str, top_n: int) -> list[TrendingItem]:
    """Normalize each signal within `pool`, weight, sort, take top-N (rank 1..N)."""
    # Step 2 — derive raw signal values; invert keyword rank (higher = better).
    max_rank = max(
        (c["keyword_rank"] for c in pool if "keyword_rank" in c),
        default=1,
    )
    for c in pool:
        if "keyword_rank" in c:
            c["keyword_raw"] = float((max_rank + 1) - c["keyword_rank"])
        else:
            c["keyword_raw"] = None
        c.setdefault("sold_quantity", None)
        c.setdefault("sold_rate", None)

    # Step 3 — min-max normalize each signal independently, within this pool only.
    kw_norms     = _min_max([c["keyword_raw"]       for c in pool])
    volume_norms = _min_max([c.get("sold_quantity") for c in pool])
    sold_norms   = _min_max([c.get("sold_rate")     for c in pool])

    # Step 4 — weighted sum (weights unchanged from v2).
    for i, c in enumerate(pool):
        c["norm_keyword"] = kw_norms[i]
        c["norm_volume"]  = volume_norms[i]
        c["norm_sold"]    = sold_norms[i]
        c["score"] = (
            KEYWORD_WEIGHT * c["norm_keyword"]
            + VOLUME_WEIGHT * c["norm_volume"]
            + SOLD_WEIGHT  * c["norm_sold"]
        )

    # Step 5 — sort, slice, assign within-category rank.
    pool.sort(
        key=lambda c: (
            -c["score"],
            -(c.get("sold_quantity") or 0),
            c["item_id"],
        )
    )

    out: list[TrendingItem] = []
    for i, c in enumerate(pool[:top_n], start=1):
        out.append(TrendingItem(
            item_id       = c["item_id"],
            title         = c.get("title") or c["item_id"],
            url           = c.get("url", ""),
            source        = "eBay",
            rank          = i,
            score         = c["score"],
            keyword_rank  = c.get("keyword_rank"),
            sold_quantity = c.get("sold_quantity"),
            sold_rate     = c.get("sold_rate"),
            norm_keyword  = c["norm_keyword"],
            norm_volume   = c["norm_volume"],
            norm_sold     = c["norm_sold"],
            category      = category,
        ))
    return out


def select_enrichment_ids(
    keyword_signals: list[KeywordSignal],
    per_category: int = GETITEM_PER_CATEGORY,
) -> list[str]:
    """Pick the item_ids worth a getItem call: top-N by rank *per category* (§0.8.7).

    Applies the two-pass category filter to the keyword signals (the seed surfaced
    them; the filter confirms the garment type), groups survivors by category, and
    keeps the best `per_category` by keyword rank. This bounds getItem at
    ~`per_category × #categories` regardless of how many raw candidates were pulled.
    """
    by_cat: dict[str, list[KeywordSignal]] = defaultdict(list)
    for kw in keyword_signals:
        if not kw.item_id:
            continue
        if not _passes_category_filter(kw.title or "", kw.category or ""):
            continue
        by_cat[kw.category].append(kw)

    ids: list[str] = []
    for sigs in by_cat.values():
        sigs.sort(key=lambda k: k.rank)         # lowest rank = best = most relevant
        ids.extend(k.item_id for k in sigs[:per_category])
    return ids


def _min_max(values: list) -> list[float]:
    """Normalize a list of optional floats to [0, 1].

    None → 0.0 (missing signal). If all present values are equal, they all
    map to 1.0. Returns a list of the same length as the input.
    """
    present = [(i, v) for i, v in enumerate(values) if v is not None]
    result = [0.0] * len(values)
    if not present:
        return result

    raw_vals = [v for _, v in present]
    mn, mx = min(raw_vals), max(raw_vals)

    for i, v in present:
        if mx > mn:
            result[i] = (v - mn) / (mx - mn)
        else:
            result[i] = 1.0
    return result


def _passes_predicate(
    sold_quantity,
    sold_rate,
    keyword_rank,
    last_sold,
    fetched_at,
    now: datetime,
    lookback_days: int = 60,
) -> bool:
    """Return True if the candidate passes all three predicate gates."""
    # Gate 1: noise gate — drop only if there is no positive signal at all
    # (no units sold, zero sell-through, and not surfaced by Best Match search).
    sq = sold_quantity or 0
    sr = sold_rate or 0.0
    if sq == 0 and sr == 0.0 and keyword_rank is None:
        return False

    # Gate 2: recency gate — must have activity within lookback_days
    cutoff = now - timedelta(days=lookback_days)
    activity_ts = last_sold or fetched_at
    if activity_ts is not None:
        ts = activity_ts if activity_ts.tzinfo else activity_ts.replace(tzinfo=timezone.utc)
        if ts < cutoff:
            return False

    # Gate 3: data-presence gate — must have at least one signal present
    return (sold_quantity is not None) or (sold_rate is not None) or (keyword_rank is not None)


# ── Orchestration helper ──────────────────────────────────────────────────────

def get_trending(
    provider: TrendingProvider,
    client,  # redis.Redis — typed loosely to avoid import at module level
    lookback_days: int = 60,
) -> list[TrendingItem]:
    """Cache-aware orchestrator: load from Redis or fetch+score+save."""
    import trending_cache

    items, ttl = trending_cache.load(client)

    if items is not None:
        if ttl < trending_cache.REFRESH_FLOOR_SECONDS:
            _maybe_refresh(provider, client, lookback_days)
        return items

    return _fetch_and_cache(provider, client, lookback_days)


def _fetch_and_score(provider, lookback_days: int):
    """Shared fetch+score body. getItem is bounded to top-N per category (§0.8.7).

    Returns (items, keyword_signals, volume_signals, sold_signals).
    """
    kw  = provider.fetch_keyword_signals(lookback_days)
    ids = select_enrichment_ids(kw)
    v   = provider.fetch_volume_signals(ids, lookback_days)
    s   = provider.fetch_sold_signals(ids, lookback_days)
    items = score_trending(kw, v, s)
    return items, kw, v, s


def _fetch_and_cache(provider, client, lookback_days: int) -> list[TrendingItem]:
    import trending_cache

    if not trending_cache.acquire_lock(client):
        items, _ = trending_cache.load(client)
        if items is not None:
            return items
    try:
        items, kw, v, s = _fetch_and_score(provider, lookback_days)
        trending_cache.save(client, items, kw, v, s)
        return items
    finally:
        trending_cache.release_lock(client)


def _maybe_refresh(provider, client, lookback_days: int) -> None:
    import trending_cache

    if trending_cache.acquire_lock(client):
        try:
            items, kw, v, s = _fetch_and_score(provider, lookback_days)
            trending_cache.save(client, items, kw, v, s)
        finally:
            trending_cache.release_lock(client)
