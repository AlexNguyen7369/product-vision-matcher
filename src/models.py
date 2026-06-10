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


# ── Trending feature signals (one per eBay source) ────────────────────────────

@dataclass
class KeywordSignal:
    item_id:    str       # eBay itemId this keyword maps to (or "" for pure-keyword rows)
    keyword:    str       # the seed search term / category that surfaced this item
    rank:       int       # Best Match position within its seed query; 1 = top, lower is stronger
    fetched_at: datetime  # when this signal was pulled (UTC)
    title:      str = ""  # item display title from the search result
    url:        str = ""  # itemWebUrl from the search result
    category:   str = ""  # v3: vintage-clothing category of the winning seed (CATEGORY_SEED_MAP);
                          # "" when the surfacing seed has no mapped category (§0.8.5)


@dataclass
class VolumeSignal:
    item_id:       str       # eBay itemId
    title:         str       # item display title
    sold_quantity: int       # estimatedSoldQuantity from Browse getItem (units sold)
    fetched_at:    datetime  # UTC


@dataclass
class SoldSignal:
    item_id:     str            # eBay itemId
    title:       str            # item display title
    sold_count:  int            # estimatedSoldQuantity (units sold) from Browse getItem
    total_count: int            # estimatedSoldQuantity + estimatedAvailableQuantity
    sold_rate:   float          # sell-through = sold_count / total_count, in [0.0, 1.0]; 0.0 if total_count == 0
    last_sold:   datetime | None  # unused by the Browse backend (no per-sale timestamps); always None
    fetched_at:  datetime       # UTC


# ── Final ranked output row ───────────────────────────────────────────────────

@dataclass
class TrendingItem:
    item_id:       str          # eBay itemId — primary key joining all three signals
    title:         str          # display title
    url:           str          # https:// link to the eBay listing
    source:        str          # marketplace name, e.g. "eBay"
    rank:          int          # final position in the trending list, 1 (top) – 10
    score:         float        # final weighted score (un-normalized sum of weighted norms)
    keyword_rank:  int | None   # None when the keyword signal was missing
    sold_quantity: int | None   # units sold (estimatedSoldQuantity); None when missing
    sold_rate:     float | None # sell-through; None when the sold signal was missing
    norm_keyword:  float        # 0.0 when signal missing (graceful degradation)
    norm_volume:   float        # normalized sold_quantity
    norm_sold:     float        # normalized sell-through
    # v3 (§0.8.10): vintage-clothing category, e.g. "Denim". Sourced from the
    # winning seed via CATEGORY_SEED_MAP (§0.8.5). Defaults "" for legacy/
    # uncategorized rows. With per-category scoring, `rank` is the rank *within*
    # this category (1..N), not a global position.
    category:      str = ""


class TrendingProvider(Protocol):
    """Contract for any trending-items backend (eBay first, others later).

    Each method fetches one raw signal over a lookback window. The scorer
    consumes the three signal lists; it never depends on a concrete provider.
    Mirrors the ReverseSearchProvider pattern.
    """

    def fetch_keyword_signals(self, lookback_days: int) -> list[KeywordSignal]: ...

    def fetch_volume_signals(
        self, item_ids: list[str], lookback_days: int
    ) -> list[VolumeSignal]: ...

    def fetch_sold_signals(
        self, item_ids: list[str], lookback_days: int
    ) -> list[SoldSignal]: ...
