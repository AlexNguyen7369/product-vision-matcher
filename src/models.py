from __future__ import annotations
from dataclasses import dataclass
from typing import Protocol


@dataclass
class ProcessedImage:
    encoded: str          # base64-encoded image bytes (UTF-8 string)
    format: str           # "JPEG" | "PNG" | "WEBP"
    size: tuple[int, int]


@dataclass
class ParsedListing:
    title:       str    # product display name
    url:         str    # full https:// link to purchase page
    source:      str    # original SerpAPI "source" string, e.g. "Amazon"
    price_raw:   str    # original price string "$29.99" — preserved for display
    price_value: float  # machine-readable float 29.99 — used for ranking
    currency:    str    # currency symbol "$"


class ReverseSearchProvider(Protocol):
    """Contract for any reverse-image-search backend.

    A provider takes a ProcessedImage and returns a raw response dict in the
    Google-Lens shape that marketplace_parser.parse() consumes (a top-level
    "visual_matches" list). pipeline depends only on this protocol, never on a
    concrete searcher, so a SerpAPI backend can be swapped for a local
    embedding/FAISS backend without touching the orchestration code.
    """

    def search(self, image: ProcessedImage) -> dict: ...
