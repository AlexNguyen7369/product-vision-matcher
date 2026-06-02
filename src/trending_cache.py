from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from datetime import datetime, timezone

import redis as _redis

from models import KeywordSignal, SoldSignal, TrendingItem, WatchSignal

log = logging.getLogger(__name__)

REDIS_URL             = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
SCHEMA_VER            = "v1"
TTL_SECONDS           = 3 * 60 * 60       # 3 hours
REFRESH_FLOOR_SECONDS = 15 * 60            # warm-refresh when remaining TTL < 15 min
LOCK_TTL_SECONDS      = 60                 # lock self-expires so a crash can't wedge it


def _key(part: str, marketplace: str = "ebay") -> str:
    return f"trending:{marketplace}:{SCHEMA_VER}:{part}"


# ── JSON helpers ──────────────────────────────────────────────────────────────

def _dt_to_str(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _str_to_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s)


def _items_to_json(items: list[TrendingItem]) -> str:
    rows = []
    for it in items:
        d = asdict(it)
        d["last_sold"] = _dt_to_str(None)  # TrendingItem has no last_sold
        rows.append(d)
    return json.dumps(rows)


def _json_to_items(raw: str) -> list[TrendingItem]:
    rows = json.loads(raw)
    result = []
    for d in rows:
        d.pop("last_sold", None)
        result.append(TrendingItem(**d))
    return result


def _signals_to_json(
    kw: list[KeywordSignal],
    watch: list[WatchSignal],
    sold: list[SoldSignal],
) -> str:
    def kw_row(k: KeywordSignal) -> dict:
        return {"item_id": k.item_id, "keyword": k.keyword, "rank": k.rank,
                "fetched_at": _dt_to_str(k.fetched_at)}

    def w_row(w: WatchSignal) -> dict:
        return {"item_id": w.item_id, "title": w.title, "watch_count": w.watch_count,
                "fetched_at": _dt_to_str(w.fetched_at)}

    def s_row(s: SoldSignal) -> dict:
        return {"item_id": s.item_id, "title": s.title, "sold_count": s.sold_count,
                "total_count": s.total_count, "sold_rate": s.sold_rate,
                "last_sold": _dt_to_str(s.last_sold),
                "fetched_at": _dt_to_str(s.fetched_at)}

    return json.dumps({
        "keyword_signals": [kw_row(k) for k in kw],
        "watch_signals":   [w_row(w)  for w in watch],
        "sold_signals":    [s_row(s)  for s in sold],
    })


# ── Public API ────────────────────────────────────────────────────────────────

def load(client: _redis.Redis, marketplace: str = "ebay") -> tuple[list[TrendingItem] | None, int]:
    """GET trending:ebay:v1:ranked. Returns (items, remaining_ttl_seconds).

    Returns (None, -2) on miss/expired or if Redis is unreachable.
    """
    key = _key("ranked", marketplace)
    try:
        raw = client.get(key)
        if raw is None:
            return None, -2
        ttl = client.ttl(key)
        return _json_to_items(raw), ttl
    except Exception:
        log.exception("trending_cache.load: Redis error")
        return None, -2


def save(
    client: _redis.Redis,
    items: list[TrendingItem],
    keyword_signals: list[KeywordSignal],
    watch_signals:   list[WatchSignal],
    sold_signals:    list[SoldSignal],
    marketplace: str = "ebay",
) -> None:
    """SET trending:ebay:v1:ranked and trending:ebay:v1:raw, both with TTL_SECONDS."""
    try:
        client.set(_key("ranked", marketplace), _items_to_json(items), ex=TTL_SECONDS)
        client.set(
            _key("raw", marketplace),
            _signals_to_json(keyword_signals, watch_signals, sold_signals),
            ex=TTL_SECONDS,
        )
    except Exception:
        log.exception("trending_cache.save: Redis error")


def acquire_lock(client: _redis.Redis, marketplace: str = "ebay") -> bool:
    """SET NX EX lock. Returns True if this caller won the lock."""
    try:
        result = client.set(_key("lock", marketplace), "1", nx=True, ex=LOCK_TTL_SECONDS)
        return result is True
    except Exception:
        log.exception("trending_cache.acquire_lock: Redis error")
        return True  # fail-open: let the caller fetch rather than deadlock


def release_lock(client: _redis.Redis, marketplace: str = "ebay") -> None:
    """DEL the lock key (best-effort; LOCK_TTL is the backstop)."""
    try:
        client.delete(_key("lock", marketplace))
    except Exception:
        log.exception("trending_cache.release_lock: Redis error")
