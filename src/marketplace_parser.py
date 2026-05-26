from __future__ import annotations
import re
from datetime import datetime, timedelta, timezone
from models import ParsedListing

MARKETPLACE_SOURCES = {
    "amazon", "ebay", "walmart", "etsy",
    "target", "bestbuy", "newegg", "wayfair",
}

_DATE_FORMATS = (
    "%Y-%m-%d",
    "%b %d, %Y",
    "%B %d, %Y",
    "%m/%d/%Y",
    "%d %b %Y",
)

_RELATIVE_RE = re.compile(r"(\d+)\s+(day|week|month|year)s?\s+ago", re.IGNORECASE)

# Scoring scale: covers 3 pages of Google Lens results (up to ~180 matches).
# position 1 -> score 10, position 180 -> score 1, clamped to [1, 10].
_MAX_POSITION = 180


def parse(serpapi_response: dict) -> list[ParsedListing]:
    """Extract, score, and filter marketplace listings from a SerpAPI response.

    Scoring and filtering are separate concerns:
      - _score() assigns a 1-10 similarity score from SerpAPI position.
      - _passes_filter() applies four hard gates unchanged from the original design.
    Returned listings are in source order; ranking is left to price_aggregator.
    """
    raw_matches = serpapi_response.get("visual_matches", [])
    results = []
    for index, match in enumerate(raw_matches):
        listing = _extract(match)
        if listing is None:
            continue
        listing.similarity_score = _score(match, index)
        if _passes_filter(listing):
            results.append(listing)
    return results


def _score(match: dict, index: int) -> int:
    """Map SerpAPI position to a 1-10 visual similarity score.

    position 1 (top Google Lens result) -> 10
    position _MAX_POSITION              -> 1
    Falls back to list index+1 when position is absent (older fixtures omit it).
    """
    position = match.get("position") or (index + 1)
    score = 10 - round((position - 1) / (_MAX_POSITION - 1) * 9)
    return max(1, min(10, score))


def _extract(match: dict) -> ParsedListing | None:
    price_block = match.get("price")
    if not price_block:
        return None

    title  = match.get("title",  "").strip()
    url    = match.get("link",   "").strip()
    source = match.get("source", "").strip()

    if not title or not url or not source:
        return None

    return ParsedListing(
        title       = title,
        url         = url,
        source      = source,
        price_raw   = price_block.get("value", ""),
        price_value = price_block.get("extracted_value", 0.0),
        currency    = price_block.get("currency", "$"),
        sold_date   = _parse_date(match.get("date", "")),
    )


def _passes_filter(listing: ParsedListing) -> bool:
    is_known_marketplace = any(
        known in listing.source.lower() for known in MARKETPLACE_SOURCES
    )
    has_valid_price = listing.price_value > 0
    has_valid_url   = listing.url.startswith(("http://", "https://"))
    is_recent       = _is_within_12_months(listing.sold_date)
    return is_known_marketplace and has_valid_price and has_valid_url and is_recent


def _is_within_12_months(sold_date: datetime | None) -> bool:
    """Return True when sold_date is recent or unknown (None = no date on listing)."""
    if sold_date is None:
        return True
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=365)
    return sold_date >= cutoff


def _parse_date(date_str: str) -> datetime | None:
    """Parse a SerpAPI date string into a UTC datetime, or None if unparseable.

    Handles relative strings ("3 months ago"), ISO dates ("2024-01-15"),
    and common US formats ("Jan 15, 2024").
    """
    if not date_str:
        return None

    m = _RELATIVE_RE.search(date_str)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        now = datetime.now(tz=timezone.utc)
        if unit == "day":
            return now - timedelta(days=n)
        if unit == "week":
            return now - timedelta(weeks=n)
        if unit == "month":
            return now - timedelta(days=n * 30)
        if unit == "year":
            return now - timedelta(days=n * 365)

    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(date_str.strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    return None
