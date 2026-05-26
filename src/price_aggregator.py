from __future__ import annotations
from models import ParsedListing


def rank_by_price(listings: list[ParsedListing]) -> list[ParsedListing]:
    """Return listings sorted ascending by price (cheapest first).

    Ordering lives here, not in marketplace_parser: the parser decides which
    listings are valid; the aggregator decides how to rank them. Callers that
    want the cheapest n can slice the result without re-sorting. sorted() is a
    stable Timsort and does not mutate the input list.
    """
    return sorted(listings, key=lambda listing: listing.price_value)
