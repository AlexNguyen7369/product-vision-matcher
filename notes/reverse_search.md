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

`ProcessedImage` is defined in `models.py` and imported from there.
`reverse_search` no longer imports `image_processor` (which would pull in PIL,
`pathlib`, and `io`) merely to name the type — it depends on the lightweight
shared contract instead.

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
class SerpApiSearcher:
    def __init__(self, api_key: str | None = None,
                 client: httpx.Client | None = None,
                 timeout: float = 30.0) -> None: ...
    def search(self, image: ProcessedImage) -> dict: ...
```

`SerpApiSearcher` implements the `ReverseSearchProvider` protocol (`models.py`).
The API key and HTTP client are **injected** through the constructor instead of
read from module-level globals:

- `api_key=None` falls back to `SERPAPI_KEY` in `.env`; an explicit string
  (including `""`) is honoured as-is, which is what lets the key guard be
  unit-tested without touching the environment.
- An injected `client` lets a caller supply a pooled/long-lived client, or a
  test transport (`httpx.MockTransport`) so the request/response path runs
  offline. When omitted, `search` opens a short-lived client per call.

`pipeline` depends on the `ReverseSearchProvider` protocol, never on this
concrete class — a local embedding/FAISS backend implementing the same
`search(image) -> dict` method can be dropped in without changing orchestration.
All other methods are private helpers prefixed with `_`.

---

## Pseudocode

```python
_SERPAPI_URL = "https://serpapi.com/search"
_DEFAULT_TIMEOUT = 30.0                    # seconds; Google Lens can be slow


def _load_default_key() -> str | None:     # env read, only when no key injected
    load_dotenv()
    return os.getenv("SERPAPI_KEY")


class SerpApiSearcher:
    def __init__(self, api_key=None, client=None, timeout=_DEFAULT_TIMEOUT):
        # api_key=None → fall back to env; explicit "" is honoured (invalid key)
        self._api_key = api_key if api_key is not None else _load_default_key()
        self._client  = client            # injected client/transport, or None
        self._timeout = timeout

    def search(self, image: ProcessedImage) -> dict:
        self._validate_key()              # fast local check before any network
        response = self._post(image)      # the only I/O in the module
        self._check_response(response)    # raises on non-200 for a clear error
        return response.json()            # hand raw dict to marketplace_parser

    def _validate_key(self) -> None:
        if not self._api_key:
            raise EnvironmentError("SERPAPI_KEY not set in .env")

    def _post(self, image: ProcessedImage) -> httpx.Response:
        # Google Lens needs multipart/form-data with raw bytes, not base64,
        # so decode the ProcessedImage.encoded string back to bytes first.
        image_bytes = base64.b64decode(image.encoded)
        fmt   = image.format.lower()      # e.g. "jpeg"
        data  = {"engine": "google_lens", "api_key": self._api_key}
        files = {"image": (f"upload.{fmt}", image_bytes, f"image/{fmt}")}

        if self._client is not None:      # reuse injected client (pool / tests)
            return self._client.post(_SERPAPI_URL, data=data, files=files)
        with httpx.Client(timeout=self._timeout) as client:
            return client.post(_SERPAPI_URL, data=data, files=files)

    @staticmethod
    def _check_response(response: httpx.Response) -> None:
        if response.status_code != 200:
            # 300-char truncation keeps large HTML error bodies log-readable.
            raise RuntimeError(f"SerpAPI {response.status_code}: {response.text[:300]}")
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

| Check | Method | Why no live API |
|---|---|---|
| Module exposes `SerpApiSearcher` | `hasattr` | Static check |
| `_validate_key()` raises `EnvironmentError` when key is empty | `SerpApiSearcher(api_key="")` | No I/O |
| `_validate_key()` passes when key is present | `SerpApiSearcher(api_key="dummy")` | No I/O |
| `search()` returns the parsed JSON dict | inject `httpx.Client(transport=httpx.MockTransport(...))` returning 200 | Offline transport |
| `search()` raises `RuntimeError` on non-200 | same, MockTransport returning 500 | Offline transport |

Injecting the client is what makes the full request/response path testable
without a live key or network — previously only `_validate_key` could be
checked. Real SerpAPI calls remain integration-only and are excluded here.
