# marketplace_parser.py — Technical Breakdown

## Role in the Pipeline

```
reverse_search.py → [SerpAPI dict] → marketplace_parser.py → [list[ParsedListing]] → price_aggregator.py
```

`marketplace_parser` receives the raw SerpAPI Google Lens response and returns
a clean, sorted list of marketplace product listings. It is a pure function
module: no I/O, no network, no side effects. All logic is deterministic on
the input dict.

---

## Input Contract

The full SerpAPI Google Lens response dict (returned by `reverse_search.search()`).
The parser only reads the `"visual_matches"` key; all other top-level keys are
ignored. Each match in `visual_matches` may have these fields:

```python
{
  "title":  str,
  "link":   str,      # full URL to product page
  "source": str,      # human-readable domain, e.g. "Amazon", "Amazon.co.uk"
  "price": {          # ABSENT for non-product results (blog posts, images, etc.)
    "value":           str,    # "$29.99"
    "extracted_value": float,  # 29.99
    "currency":        str     # "$"
  },
  "thumbnail": str,   # may be absent
  "rating":    float, # may be absent
  "reviews":   int    # may be absent
}
```

---

## Output Contract

```python
list[ParsedListing]
```

Sorted **ascending by `price_value`** so `price_aggregator` can take `[:n]`
without re-sorting. Only entries that pass all three filter predicates appear.

---

## Data Model

```python
@dataclass
class ParsedListing:
    title:       str    # product display name
    url:         str    # full https:// link to purchase page
    source:      str    # original SerpAPI "source" string, e.g. "Amazon"
    price_raw:   str    # original price string "$29.99" — preserved for display
    price_value: float  # machine-readable float 29.99 — used for sorting
    currency:    str    # currency symbol "$"
```

`price_raw` and `price_value` are kept separately because:
- `price_raw` is what the user sees (correctly formatted, with symbol)
- `price_value` is what the code compares and sorts on (no string parsing downstream)

---

## Three-Step Process

```
visual_matches[]
    │
    ▼
_extract(match)        # maps each raw dict → ParsedListing | None
    │                  # returns None (not raises) for structurally invalid entries
    ▼
filter(None, ...)      # removes the Nones
    │
    ▼
_passes_filter(l)      # applies three business-logic predicates
    │
    ▼
sorted(..., key=price) # ascending by price_value
```

Separating extract from filter is deliberate: `_extract` handles *structural*
problems (missing fields, wrong types), while `_passes_filter` handles
*business logic* problems (wrong domain, untrustworthy price). They fail for
different reasons and should be diagnosable independently.

---

## Filter Technical Breakdown

`_passes_filter` applies three independent predicates. All three must be
`True` for a listing to survive. They are ordered **cheapest-to-evaluate
first** so Python's short-circuit `and` exits early on the most common
rejection cases.

### Predicate 1 — Allowlist membership check

```python
is_known_marketplace = any(
    known in listing.source.lower() for known in MARKETPLACE_SOURCES
)
```

**Why an allowlist, not a blocklist?**

SerpAPI `visual_matches` includes everything Google finds: blog posts, image
galleries, news articles, brand homepages, Reddit threads, social media. A
blocklist would require enumerating every non-marketplace domain that could
ever appear — an unbounded, constantly-growing set. An allowlist inverts the
problem: we name the domains we trust to have a structured price and a
checkout flow, and silently drop everything else.

**Why `source.lower()` and substring match, not equality?**

SerpAPI normalises `"www.amazon.com"` → `"Amazon"` for the `source` field,
but regional or decorated variants slip through: `"Amazon.co.uk"`,
`"eBay Motors"`, `"Walmart Grocery"`. Lowercasing + substring matching
(`"amazon" in "amazon.co.uk"`) handles all variants without adding extra
entries to `MARKETPLACE_SOURCES`.

**Why `any(... for known in MARKETPLACE_SOURCES)` as a generator?**

`any()` with a generator expression is **short-circuiting**: Python evaluates
`known in source` lazily and stops at the first `True`. This is O(k) where
k = len(MARKETPLACE_SOURCES), not O(k × len(source)). The `in` operator on a
string is a Boyer-Moore-Horspool substring search (C level in CPython), not a
character-by-character scan, so each individual check is fast.

**MARKETPLACE_SOURCES set:**

```python
MARKETPLACE_SOURCES = {
    "amazon", "ebay", "walmart", "etsy",
    "target", "bestbuy", "newegg", "wayfair",
}
```

Using a `set` means membership checks on `MARKETPLACE_SOURCES` itself are
O(1) hash lookups — though here we iterate over it, so membership in the set
doesn't speed up the inner loop. The set is appropriate for semantic clarity
(unordered, no duplicates) even if iteration order doesn't matter.

---

### Predicate 2 — Price sanity check

```python
has_valid_price = listing.price_value > 0
```

`extracted_value` can be `0.0` in two legitimate error cases:

1. The price string was present but unparseable by SerpAPI
   (e.g. `"Contact for price"`, `"See in cart"`).
2. `_extract` defaulted to `0.0` because `extracted_value` was absent from
   the price block.

A listing with price `0.0` cannot be meaningfully ranked or compared, so it
is dropped. We do not check `is not None` because `_extract` already
guarantees `price_value` is a float via the `.get("extracted_value", 0.0)`
default — `None` can never reach this predicate.

---

### Predicate 3 — URL scheme guard

```python
has_valid_url = listing.url.startswith(("http://", "https://"))
```

SerpAPI occasionally returns:
- Relative paths: `"/products/123"`
- JavaScript pseudo-URLs: `"javascript:void(0)"` (sponsored slots)
- Empty strings: `""` (malformed entries)

These cannot be fetched by `price_aggregator` and would cause a silent failure
or a `MissingSchema` exception deep downstream. Checking the scheme here is
cheaper than letting `httpx` raise an exception later in the pipeline.

`str.startswith(tuple)` checks both schemes in one call — Python compiles this
to two `memcmp`-style comparisons, which is faster than `url.startswith("http://") or url.startswith("https://")`.

---

## Extraction Error Handling: None vs Raise

`_extract` returns `None` instead of raising an exception for malformed entries:

```python
def _extract(match: dict) -> ParsedListing | None:
    price_block = match.get("price")
    if not price_block:
        return None       # ← None, not ValueError

    title  = match.get("title",  "").strip()
    url    = match.get("link",   "").strip()
    source = match.get("source", "").strip()

    if not title or not url or not source:
        return None       # ← None, not ValueError
    ...
```

**Why not raise?** A single malformed entry in a 20-result SerpAPI response
should not abort the entire parse. Real SerpAPI responses contain mixed
content (sponsored results, knowledge graph entries, partially formed records)
and partial failures are expected. `filter(None, ...)` removes the `None`
values silently, and the valid entries are returned.

**When would we raise instead?** If the *entire response* is malformed (e.g.
`visual_matches` is not a list), that is a structural contract violation that
should propagate — but `serpapi_response.get("visual_matches", [])` handles
that gracefully by defaulting to an empty list.

---

## Sort Order

```python
return sorted(valid, key=lambda l: l.price_value)
```

Ascending order (cheapest first) is chosen because:
- `price_aggregator` will likely want to present the cheapest options first
- `price_aggregator` can slice `[:n]` without re-sorting
- Descending order can be achieved with `reversed()` at zero allocation cost

`sorted()` returns a new list (stable sort, Timsort, O(n log n)). The input
`valid` list is not mutated.

---

## Full Pseudocode

```python
MARKETPLACE_SOURCES = {
    "amazon", "ebay", "walmart", "etsy",
    "target", "bestbuy", "newegg", "wayfair",
}

@dataclass
class ParsedListing:
    title: str; url: str; source: str
    price_raw: str; price_value: float; currency: str


def parse(serpapi_response: dict) -> list[ParsedListing]:
    raw_matches = serpapi_response.get("visual_matches", [])
    candidates  = list(filter(None, (_extract(m) for m in raw_matches)))
    valid       = [l for l in candidates if _passes_filter(l)]
    return sorted(valid, key=lambda l: l.price_value)


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
```

---

## Edge Cases Handled

| Input | Behaviour |
|---|---|
| `visual_matches` key absent from response | `[]` returned (`.get` default) |
| Match has no `"price"` key | `_extract` returns `None` → dropped |
| Match has empty title, url, or source | `_extract` returns `None` → dropped |
| `extracted_value` is `0.0` | `_passes_filter` rejects (zero price) |
| `source` is `"Amazon.co.uk"` | passes (substring match `"amazon" in "amazon.co.uk"`) |
| `link` is `"javascript:void(0)"` | `_passes_filter` rejects (scheme guard) |
| All matches fail filter | `[]` returned |
