from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from models import KeywordSignal, SoldSignal, TrendingItem, VolumeSignal

if TYPE_CHECKING:
    from models import TrendingProvider

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
    # Step 0 — build candidate dicts keyed by item_id
    candidates: dict[str, dict] = {}

    for kw in keyword_signals:
        if kw.item_id:
            c = candidates.setdefault(kw.item_id, {"item_id": kw.item_id, "title": "", "url": ""})
            c["keyword_rank"] = kw.rank
            if kw.title:
                c["title"] = kw.title
            if kw.url:
                c["url"] = kw.url
            c.setdefault("fetched_at", kw.fetched_at)

    for v in volume_signals:
        c = candidates.setdefault(v.item_id, {"item_id": v.item_id, "title": v.title, "url": ""})
        c["sold_quantity"] = v.sold_quantity
        if v.title:
            c["title"] = v.title
        c.setdefault("fetched_at", v.fetched_at)

    for s in sold_signals:
        c = candidates.setdefault(s.item_id, {"item_id": s.item_id, "title": s.title, "url": ""})
        c["sold_rate"] = s.sold_rate
        c["last_sold"] = s.last_sold
        if s.title:
            c["title"] = s.title
        c.setdefault("fetched_at", s.fetched_at)

    now = datetime.now(tz=timezone.utc)

    # Step 1 — apply predicate filters
    surviving = [
        c for c in candidates.values()
        if _passes_predicate(
            c.get("sold_quantity"),
            c.get("sold_rate"),
            c.get("keyword_rank"),
            c.get("last_sold"),
            c.get("fetched_at"),
            now,
        )
    ]

    if not surviving:
        return []

    # Step 2 — derive raw signal values; invert keyword rank
    max_rank = max(
        (c["keyword_rank"] for c in surviving if "keyword_rank" in c),
        default=1,
    )
    for c in surviving:
        if "keyword_rank" in c:
            c["keyword_raw"] = float((max_rank + 1) - c["keyword_rank"])
        else:
            c["keyword_raw"] = None
        c.setdefault("sold_quantity", None)
        c.setdefault("sold_rate", None)

    # Step 3 — min-max normalize each signal independently
    kw_norms     = _min_max([c["keyword_raw"]      for c in surviving])
    volume_norms = _min_max([c.get("sold_quantity") for c in surviving])
    sold_norms   = _min_max([c.get("sold_rate")     for c in surviving])

    # Step 4 — weighted sum
    for i, c in enumerate(surviving):
        c["norm_keyword"] = kw_norms[i]
        c["norm_volume"]  = volume_norms[i]
        c["norm_sold"]    = sold_norms[i]
        c["score"] = (
            KEYWORD_WEIGHT * c["norm_keyword"]
            + VOLUME_WEIGHT * c["norm_volume"]
            + SOLD_WEIGHT  * c["norm_sold"]
        )

    # Step 5 — sort and slice
    surviving.sort(
        key=lambda c: (
            -c["score"],
            -(c.get("sold_quantity") or 0),
            c["item_id"],
        )
    )

    items = []
    for i, c in enumerate(surviving[:top_n], start=1):
        items.append(TrendingItem(
            item_id      = c["item_id"],
            title        = c.get("title") or c["item_id"],
            url          = c.get("url", ""),
            source       = "eBay",
            rank         = i,
            score        = c["score"],
            keyword_rank  = c.get("keyword_rank"),
            sold_quantity = c.get("sold_quantity"),
            sold_rate     = c.get("sold_rate"),
            norm_keyword  = c["norm_keyword"],
            norm_volume   = c["norm_volume"],
            norm_sold     = c["norm_sold"],
        ))
    return items


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


def _fetch_and_cache(provider, client, lookback_days: int) -> list[TrendingItem]:
    import trending_cache

    if not trending_cache.acquire_lock(client):
        items, _ = trending_cache.load(client)
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
        trending_cache.release_lock(client)


def _maybe_refresh(provider, client, lookback_days: int) -> None:
    import trending_cache

    if trending_cache.acquire_lock(client):
        try:
            kw  = provider.fetch_keyword_signals(lookback_days)
            ids = [k.item_id for k in kw if k.item_id]
            v   = provider.fetch_volume_signals(ids, lookback_days)
            s   = provider.fetch_sold_signals(ids, lookback_days)
            items = score_trending(kw, v, s)
            trending_cache.save(client, items, kw, v, s)
        finally:
            trending_cache.release_lock(client)
