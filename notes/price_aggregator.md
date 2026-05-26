# price_aggregator.py — Technical Breakdown

## Role in the Pipeline

`price_aggregator` is the final stage of the pipeline. It receives a
source-ordered list of valid marketplace listings from `marketplace_parser` and
produces a `PriceReport`: a sorted list of all listings plus three computed
statistics — average listing price, average sold price, and sold count.

---

## Full Pipeline Flow

```
[image file on disk]
        │
        ▼
image_processor.process_image(path)
        │  loads with PIL, validates format (JPEG/PNG/WEBP only),
        │  resizes to ≤ 1024×1024, base64-encodes the bytes
        │
        ▼
ProcessedImage(encoded: str, format: str, size: tuple)
        │
        │  encoded is a UTF-8 base64 string — safe to store in a
        │  JSON-serialisable dataclass, attach to request bodies, or log
        │
        ▼
SerpApiSearcher.search(image: ProcessedImage)
        │  base64.b64decode(image.encoded) → raw bytes
        │  multipart/form-data POST to SerpAPI Google Lens endpoint
        │  engine=google_lens, image uploaded as binary field
        │
        ▼
raw SerpAPI response dict  (returned unchanged — nothing stripped)
        │
        │  full payload handed to marketplace_parser so no field is
        │  discarded before the parser has a chance to inspect it
        │
        ▼
marketplace_parser.parse(serpapi_response)
        │
        │  ── EXTRACT ──────────────────────────────────────────────────
        │  reads visual_matches[]
        │  for each match, returns None (not raises) if:
        │    • no "price" block present                    (non-product result)
        │    • title, link, or source is empty/missing     (malformed entry)
        │  otherwise builds a ParsedListing; sold_date is parsed from
        │  the optional "date" field (ISO, US, or relative "N months ago")
        │
        │  ── FILTER (_passes_filter) ──────────────────────────────────
        │  drops listing if ANY predicate fails:
        │    1. source not in allowlist                    (non-marketplace)
        │       {"amazon","ebay","walmart","etsy",
        │        "target","bestbuy","newegg","wayfair"}
        │       — substring match, case-insensitive, handles regional
        │         variants like "Amazon.co.uk" and "eBay Motors"
        │    2. price_value == 0.0                         (unparseable/contact price)
        │    3. url scheme not http:// or https://         (javascript:, relative, empty)
        │    4. sold_date > 12 months ago                  (stale sold listing)
        │       — sold_date=None passes (undated = active listing)
        │
        │  returns list[ParsedListing] in source order (no sort here —
        │  ordering belongs to price_aggregator)
        │
        ▼
list[ParsedListing]  — valid, filtered, source-ordered
        │
        ▼
price_aggregator.aggregate(listings)
        │  separates sold (sold_date is not None) from active (sold_date is None)
        │  computes means independently so the two averages are never mixed
        │  sorts all listings ascending by price_value
        │
        ▼
PriceReport
```

---

## Input Contract

```python
list[ParsedListing]
```

Every entry has already passed all four filter predicates in `marketplace_parser`.
`price_aggregator` trusts this — it does no re-validation.

Each `ParsedListing` field used by the aggregator:

| Field | Type | Used for |
|---|---|---|
| `price_value` | `float` | sorting, mean computation |
| `sold_date` | `datetime \| None` | sold vs active split |
| `currency` | `str` | copied to `PriceReport.currency` from first listing |

---

## Output Contract

```python
@dataclass
class PriceReport:
    listings:          list[ParsedListing]  # all listings, sorted ascending by price_value
    avg_listing_price: float                # mean price_value of active listings; 0.0 if none
    avg_sold_price:    float                # mean price_value of sold listings; 0.0 if none
    sold_count:        int                  # len of sold subset
    listing_count:     int                  # len of active subset
    currency:          str                  # from first listing; "$" if input is empty
```

`avg_listing_price` and `avg_sold_price` are rounded to two decimal places via
`round(..., 2)` before being stored — consistent with monetary display
conventions and safe for direct comparison in tests.

---

## Implementation

```python
def aggregate(listings: list[ParsedListing]) -> PriceReport:
    sold   = [l for l in listings if l.sold_date is not None]
    active = [l for l in listings if l.sold_date is None]

    return PriceReport(
        listings          = rank_by_price(listings),
        avg_listing_price = _mean(active),
        avg_sold_price    = _mean(sold),
        sold_count        = len(sold),
        listing_count     = len(active),
        currency          = listings[0].currency if listings else "$",
    )

def rank_by_price(listings: list[ParsedListing]) -> list[ParsedListing]:
    return sorted(listings, key=lambda l: l.price_value)

def _mean(listings: list[ParsedListing]) -> float:
    if not listings:
        return 0.0
    return round(sum(l.price_value for l in listings) / len(listings), 2)
```

### Why sold and active are separated before computing means

Mixing sold prices with active listing prices produces a meaningless average.
A product with three active listings at $30 and two sold comps at $20 does not
have an "average price" of $26 — the two numbers answer different questions:

- `avg_sold_price` answers: *what did buyers actually pay?*
- `avg_listing_price` answers: *what are sellers currently asking?*

The gap between the two is itself a signal — a wide gap often indicates
negotiation room or slow-moving inventory.

### Why `rank_by_price` is a separate public function

`pipeline.py` may want to re-rank a subset of listings (e.g. after filtering
to a single source) without running the full aggregation. Exposing it
separately keeps both callsites clean.

### Why `sorted()` and not `.sort()`

`sorted()` returns a new list; `.sort()` mutates in place. The input list
belongs to the caller (`marketplace_parser`'s return value) and must not be
mutated. Returning a new list also means callers can hold references to both
the original and the sorted copy if needed.

### Why `_mean` returns `0.0` for an empty list

Two alternatives:
- Return `None` — forces every caller to handle `None`, adds noise to types
- Raise `ZeroDivisionError` — crashes on a legitimate, recoverable state
  (a product with no sold comps is normal, not an error)

`0.0` is unambiguous because `price_value > 0` is enforced by the filter —
a real price can never be `0.0`. So `avg_sold_price == 0.0` always and only
means "no sold listings found."

---

## Sold Date Detection

`sold_date` is populated by `marketplace_parser._parse_date()`, not by the
aggregator. The aggregator only reads the field.

`_parse_date` handles three SerpAPI date formats:

| Format | Example | Parser |
|---|---|---|
| ISO date | `"2025-01-15"` | `datetime.strptime("%Y-%m-%d")` |
| US date | `"Jan 15, 2025"` or `"January 15, 2025"` | strptime with `%b`/`%B` formats |
| Relative | `"3 months ago"`, `"1 year ago"` | regex + `timedelta` arithmetic |

Unrecognised strings return `None` (treated as active listing). Missing `"date"`
field returns `None` (no date = undated active listing, passes filter).

### 12-month staleness filter

`_is_within_12_months(sold_date)`:
- `None` → `True` (undated listing, assumed current)
- `sold_date < now - 365 days` → `False` (dropped by `_passes_filter`)
- Otherwise → `True`

This runs inside `marketplace_parser`, not `price_aggregator`. By the time
listings reach the aggregator, all stale sold entries are already gone.

---

## Edge Cases

| Input | Behaviour |
|---|---|
| Empty list | `PriceReport` with all zeros and `currency="$"` |
| All active listings | `avg_sold_price=0.0`, `sold_count=0` |
| All sold listings | `avg_listing_price=0.0`, `listing_count=0` |
| Single listing | `listings=[that listing]`, averages = that price, counts = 0 or 1 |
| Equal prices | `sorted()` is stable — original relative order preserved |

---

## Filter Predicate Summary (marketplace_parser)

For reference, the four predicates that every listing must pass before reaching
the aggregator:

| # | Predicate | Drop condition | Example dropped |
|---|---|---|---|
| 1 | Allowlist membership | source not in known marketplaces | Blogger, Reddit, brand homepages |
| 2 | Price sanity | `price_value == 0.0` | "Contact for price", unparseable price strings |
| 3 | URL scheme guard | not `http://` or `https://` | `javascript:void(0)`, relative paths, empty strings |
| 4 | Recency guard | `sold_date` older than 12 months | eBay completed listings from 2 years ago |

Predicates are evaluated with short-circuit `and` in cheapest-first order.
All four must be `True` for a listing to be returned.

---

## Test Coverage (Section 10, test_setup.py)

| Check | Fixture | Asserts |
|---|---|---|
| Returns `PriceReport` | 2 active + 2 sold | `isinstance(report, PriceReport)` |
| `avg_listing_price` | active: $20, $30 | `== 25.00` |
| `avg_sold_price` | sold: $15, $25 | `== 20.00` |
| `sold_count` | 2 sold listings | `== 2` |
| `listing_count` | 2 active listings | `== 2` |
| Sorted ascending | 4 mixed listings | `prices == sorted(prices)` |
| No sold listings | active only | `avg_sold_price == 0.0`, `sold_count == 0` |
| All sold | sold only | `avg_listing_price == 0.0`, `listing_count == 0` |
| Empty input | `[]` | all zeros, `currency == "$"` |
| Currency field | listing with `"€"` | `report.currency == "€"` |
