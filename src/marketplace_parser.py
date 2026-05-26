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


def parse(serpapi_response: dict) -> list[ParsedListing]:
    """Extract and filter marketplace listings from a SerpAPI response.

    Returns listings in source order. Ranking/ordering is intentionally left
    to price_aggregator — the parser decides which listings are valid, the
    aggregator decides how to present them.
    """
    raw_matches = serpapi_response.get("visual_matches", [])
    candidates = list(filter(None, (_extract(m) for m in raw_matches)))
    return [listing for listing in candidates if _passes_filter(listing)]


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
