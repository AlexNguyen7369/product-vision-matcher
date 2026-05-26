# pipeline.py — Technical Breakdown

## Role in the Pipeline

`pipeline.py` is the top-level orchestrator. It chains the four implemented
modules in sequence and returns a `PriceReport`. It also exposes `format_report()`
for human-readable terminal output, and a `__main__` entry point so the pipeline
can be run directly from the command line.

---

## Public Surface

```python
def run(image_path: str, searcher: ReverseSearchProvider) -> PriceReport
def format_report(report: PriceReport) -> str
```

`run()` depends on the `ReverseSearchProvider` protocol, not the concrete
`SerpApiSearcher` class, so any conforming backend (SerpAPI, local FAISS, mock)
can be injected without changing the orchestration code.

---

## Data Flow

```
[image file on disk]
        │
        ▼
image_processor.process_image(image_path)
        │  validates format (JPEG/PNG/WEBP), resizes to ≤1024×1024,
        │  base64-encodes the bytes
        ▼
ProcessedImage
        │
        ▼
searcher.search(processed)          ← injected ReverseSearchProvider
        │  SerpApiSearcher: multipart POST to SerpAPI Google Lens
        │  returns raw response dict unchanged
        ▼
raw dict  {"visual_matches": [...]}
        │
        ▼
marketplace_parser.parse(raw)
        │  extracts valid ParsedListings, drops non-marketplace / bad-price /
        │  bad-URL / stale-sold entries
        ▼
list[ParsedListing]
        │
        ▼
price_aggregator.aggregate(listings)
        │  splits active vs sold, computes means independently,
        │  sorts all listings ascending by price
        ▼
PriceReport
```

---

## format_report() Output

`format_report(report)` returns a multiline string suitable for `print()`.

```
────────────────────────────────────────────
  Active listings  : 3
  Avg listing price: $29.50
  Sold listings    : 2
  Avg sold price   : $22.00
────────────────────────────────────────────
  All listings (cheapest first):
        $19.99  eBay          Blue Widget Lite
        $29.99  Amazon        Blue Widget Pro
        $38.50  Walmart       Blue Widget Deluxe
    [sold]  $20.00  eBay          Blue Widget (sold)
    [sold]  $24.00  Amazon        Blue Widget (sold)
```

- Active listings have no tag; sold listings are prefixed with `[sold]`.
- All listings appear in a single sorted block (cheapest first) regardless of
  active vs sold status — the summary rows above the divider give the split.
- If there are no valid listings, the block prints `No valid listings found.`

---

## CLI Usage

```powershell
# From the project root (virtual environment active)
python src/pipeline.py data/uploads/my_product.jpg
```

This constructs a `SerpApiSearcher` (reads `SERPAPI_KEY` from `.env`), runs the
full pipeline, and prints a formatted report to stdout.

---

## Why `run()` takes a `ReverseSearchProvider`, not a path to a `.env` key

Hard-coding `SerpApiSearcher()` inside `run()` would couple the orchestrator to
one network backend and make offline testing require a live API key. Injecting
the provider means:

- Tests pass a `_MockProvider` that returns fixture data — no network, no key.
- A future local-embedding backend (FAISS) slots in by implementing `.search()`.
- The `__main__` block constructs the real searcher exactly once, at the entry
  point, which is the only place where environment details belong.

---

## Implementation

```python
def run(image_path: str, searcher: ReverseSearchProvider) -> PriceReport:
    processed = image_processor.process_image(image_path)
    raw       = searcher.search(processed)
    listings  = marketplace_parser.parse(raw)
    return price_aggregator.aggregate(listings)
```

Four lines; no branching. Each stage has its own module with its own tests.
`pipeline.py` only orchestrates — it adds no logic of its own.

---

## Edge Cases

| Input | Behaviour |
|---|---|
| Image fails format validation | `image_processor.process_image` raises `ValueError` before network call |
| SerpAPI returns non-200 | `SerpApiSearcher.search` raises `RuntimeError` |
| Zero valid listings after filter | `aggregate([])` returns a zero-valued `PriceReport` |
| All listings are sold | `avg_listing_price=0.0`, `listing_count=0` |
| No sold listings | `avg_sold_price=0.0`, `sold_count=0` |

---

## Test Coverage (Section 11, test_setup.py)

All checks use a `_MockProvider` that returns the Section 7 fixture (2 valid
active listings: Amazon $29.99, eBay $24.50). No network calls.

| Check | Asserts |
|---|---|
| `run()` returns `PriceReport` | `isinstance(report, PriceReport)` |
| `listing_count` | `== 2` |
| `avg_listing_price` | `== round((24.50 + 29.99) / 2, 2)` = 27.25 |
| No sold listings | `sold_count == 0`, `avg_sold_price == 0.0` |
| Listings sorted ascending | `prices == sorted(prices)` |
| Empty provider | zero-valued `PriceReport` returned cleanly |
| `format_report()` key fields | avg prices and counts present in output |
| `format_report()` empty message | `"No valid listings found"` in output |
| `format_report()` sold tag | `"[sold]"` present for listing with `sold_date` |
