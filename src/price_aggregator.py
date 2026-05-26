from __future__ import annotations
from models import ParsedListing, PriceReport


def aggregate(listings: list[ParsedListing]) -> PriceReport:
    """Aggregate a list of marketplace listings into a price report.

    Sold listings (sold_date is not None) and active listings (sold_date is
    None) are separated before computing averages, so the two means are never
    mixed. The returned PriceReport.listings is sorted ascending by price so
    callers can slice cheapest-n without re-sorting.
    """
    sold   = [l for l in listings if l.sold_date is not None]
    active = [l for l in listings if l.sold_date is None]

    avg_sold    = _mean(sold)
    avg_listing = _mean(active)
    currency    = listings[0].currency if listings else "$"

    return PriceReport(
        listings          = rank_by_price(listings),
        avg_listing_price = avg_listing,
        avg_sold_price    = avg_sold,
        sold_count        = len(sold),
        listing_count     = len(active),
        currency          = currency,
    )


def rank_by_price(listings: list[ParsedListing]) -> list[ParsedListing]:
    """Return listings sorted ascending by price (cheapest first)."""
    return sorted(listings, key=lambda l: l.price_value)


def _mean(listings: list[ParsedListing]) -> float:
    if not listings:
        return 0.0
    return round(sum(l.price_value for l in listings) / len(listings), 2)
