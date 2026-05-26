from __future__ import annotations
from models import ParsedListing

MARKETPLACE_SOURCES = {
    "amazon", "ebay", "walmart", "etsy",
    "target", "bestbuy", "newegg", "wayfair",
}


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
    )


def _passes_filter(listing: ParsedListing) -> bool:
    is_known_marketplace = any(
        known in listing.source.lower() for known in MARKETPLACE_SOURCES
    )
    has_valid_price = listing.price_value > 0
    has_valid_url   = listing.url.startswith(("http://", "https://"))
    return is_known_marketplace and has_valid_price and has_valid_url
