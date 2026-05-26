# reverse_search.py — Technical Plan & Breakdown

## Role in the Pipeline

```
image_processor.py → [ProcessedImage] → reverse_search.py → [SerpAPI dict] → marketplace_parser.py
```

`reverse_search` is the network boundary of the system. It takes the image
that `image_processor` prepared and exchanges it with the external SerpAPI
Google Lens endpoint. Everything it returns is raw — no filtering, no
reshaping. That responsibility belongs to `marketplace_parser`.

---

## Input Contract

```python
@dataclass
class ProcessedImage:
    encoded: str          # base64-encoded image bytes (UTF-8 string)
    format:  str          # "JPEG" | "PNG" | "WEBP"
    size:    tuple[int, int]
```

`reverse_search` only uses `encoded` and `format`. `size` is carried through
the dataclass for debugging but is not sent to SerpAPI.

---

## Output Contract

Returns the **raw SerpAPI Google Lens response dict** unchanged:

```python
{
  "search_metadata":  { "id": str, "status": str, ... },
  "search_parameters": { "engine": "google_lens", ... },
  "visual_matches": [
    {
      "position": int,
      "title":    str,
      "link":     str,          # full product URL
      "source":   str,          # human-readable domain, e.g. "Amazon"
      "thumbnail": str,         # image CDN URL (may be absent)
      "price": {                # absent for non-product results
        "value":           str,   # "$29.99"
        "extracted_value": float, # 29.99
        "currency":        str    # "$"
      },
      "rating":  float,         # may be absent
      "reviews": int            # may be absent
    },
    ...
  ],
  "knowledge_graph": { ... },   # may be absent
  "image_sources":  [ ... ]     # may be absent
}
```

The dict is passed directly into `marketplace_parser.parse()`. Returning it
raw means no SerpAPI fields are discarded before the parser has a chance to
inspect them — useful when debugging why a listing was or wasn't found.

---

## Public Surface

```python
def search(image: ProcessedImage) -> dict
```

That is the only public symbol. All other functions are private helpers
prefixed with `_`.

---

## Pseudocode

```python
SERPAPI_KEY = os.getenv("SERPAPI_KEY")   # loaded from .env at module import
_SERPAPI_URL = "https://serpapi.com/search"
_TIMEOUT     = 30.0                       # seconds; Google Lens can be slow


def search(image: ProcessedImage) -> dict:
    _validate_key()           # fast local check before touching the network
    response = _post(image)   # the only I/O in the module
    _check_response(response) # raises on any non-200 so caller gets a clear error
    return response.json()    # hand raw dict to marketplace_parser


def _validate_key() -> None:
    # Checked here rather than at module load so the error surfaces at call
    # time (test_setup checks this explicitly).
    if not SERPAPI_KEY:
        raise EnvironmentError("SERPAPI_KEY not set in .env")


def _post(image: ProcessedImage) -> httpx.Response:
    # SerpAPI Google Lens does not accept base64 strings in the request body.
    # It expects multipart/form-data with the raw bytes in the "image" field.
    # We therefore decode the base64 string back to bytes before uploading.
    image_bytes = base64.b64decode(image.encoded)
    mime = f"image/{image.format.lower()}"   # e.g. "image/jpeg"

    with httpx.Client(timeout=_TIMEOUT) as client:
        return client.post(
            _SERPAPI_URL,
            data  = {"engine": "google_lens", "api_key": SERPAPI_KEY},
            files = {"image": (f"upload.{image.format.lower()}", image_bytes, mime)},
        )
    # httpx.Client is used as a context manager so the connection pool is
    # released immediately after the response is received.


def _check_response(response: httpx.Response) -> None:
    if response.status_code != 200:
        raise RuntimeError(
            f"SerpAPI {response.status_code}: {response.text[:300]}"
        )
    # 300-char truncation: SerpAPI error bodies can be large HTML pages;
    # truncating keeps the exception message readable in logs.
```

---

## Technical Breakdown

### 1. Why base64 decode before upload?

`image_processor` encodes the image to base64 so it is safe to store in a
JSON-serialisable dataclass and attach to request bodies that expect text.
But SerpAPI Google Lens requires a binary file upload via
`multipart/form-data` — it does not accept base64 in the form body.

The round-trip is therefore:
```
PIL Image → bytes → base64 string (ProcessedImage.encoded)
                         ↓ here
              base64.b64decode → raw bytes → multipart upload
```

`base64.b64decode` is O(n) in the length of the string and allocates exactly
`(3/4) * len(encoded)` bytes. For a 1024×1024 JPEG this is typically < 300 KB
so the overhead is negligible.

### 2. httpx over requests

`httpx` is already in `requirements.txt`. It has native async support (useful
if the pipeline ever becomes concurrent), a cleaner timeout API, and enforces
`Content-Type` multipart boundaries correctly when the `files=` kwarg is used.

### 3. Timeout strategy

`_TIMEOUT = 30.0` covers the full response — Google Lens can take several
seconds for large image uploads. Setting a single float applies the same
value to connect + read combined (httpx default). If needed this can be split:
```python
httpx.Timeout(connect=5.0, read=30.0)
```

### 4. Why return the raw dict, not a typed dataclass?

`marketplace_parser` already owns the contract for what fields are required
and how to handle missing ones. Wrapping the response in a typed dataclass
here would duplicate that contract in two places. If SerpAPI adds or renames
fields, only `marketplace_parser` needs to change.

---

## Error States

| Condition | Exception raised | Where |
|---|---|---|
| `SERPAPI_KEY` missing in `.env` | `EnvironmentError` | `_validate_key()` |
| SerpAPI returns non-200 | `RuntimeError` | `_check_response()` |
| Network timeout | `httpx.TimeoutException` | `_post()` (propagates) |
| Invalid base64 in ProcessedImage | `binascii.Error` | `_post()` (propagates) |

None of these are caught inside `reverse_search` — they propagate to
`pipeline.py`, which is the appropriate place to decide on retry logic.

---

## Test Plan (section 6 in test_setup.py)

| Check | Method | Why no API call |
|---|---|---|
| Module imports with `search` and `SERPAPI_KEY` | `hasattr` | Static check |
| `_validate_key()` raises `EnvironmentError` when key is `None` | Temporarily set `SERPAPI_KEY = None` | No I/O |
| `_validate_key()` passes when key is a non-empty string | Set `SERPAPI_KEY = "dummy"` | No I/O |

Real HTTP calls are integration tests and require a live key + network. They
are intentionally excluded from `test_setup.py`.
