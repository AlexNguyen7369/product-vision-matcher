from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass
class ProcessedImage:
    encoded: str          # base64-encoded image bytes (UTF-8 string)
    format: str           # "JPEG" | "PNG" | "WEBP"
    size: tuple[int, int]


@dataclass
class ParsedListing:
    title:       str            # product display name
    url:         str            # full https:// link to purchase page
    source:      str            # original SerpAPI "source" string, e.g. "Amazon"
    price_raw:   str            # original price string "$29.99" — preserved for display
    price_value: float          # machine-readable float 29.99 — used for ranking
    currency:    str            # currency symbol "$"
    sold_date:        datetime | None = field(default=None)  # parsed from SerpAPI "date" field; None = active listing
    similarity_score: int          = field(default=1)     # 1 (weakest) – 10 (best visual match), from SerpAPI position


@dataclass
class PriceReport:
    listings:          list[ParsedListing]  # all valid listings sorted ascending by price
    avg_listing_price: float                # mean price of active (unsold) listings; 0.0 if none
    avg_sold_price:    float                # mean price of sold listings; 0.0 if none
    sold_count:        int                  # number of sold listings
    listing_count:     int                  # number of active listings
    currency:          str                  # currency symbol taken from first listing


class ReverseSearchProvider(Protocol):
    """Contract for any reverse-image-search backend.

    A provider takes a ProcessedImage and returns a raw response dict in the
    Google-Lens shape that marketplace_parser.parse() consumes (a top-level
    "visual_matches" list). pipeline depends only on this protocol, never on a
    concrete searcher, so a SerpAPI backend can be swapped for a local
    embedding/FAISS backend without touching the orchestration code.
    """

    def search(self, image: ProcessedImage) -> dict: ...
