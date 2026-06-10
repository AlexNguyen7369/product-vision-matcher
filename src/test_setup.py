import sys
import tempfile
import base64

# Force UTF-8 stdout/stderr so the suite runs identically on Windows (default
# cp1252 console) and macOS/Linux (UTF-8). Without this, the Unicode arrows (→)
# in the check labels below raise UnicodeEncodeError on a stock Windows console.
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        _reconfigure(encoding="utf-8")

# ── helpers ──────────────────────────────────────────────────────────────────

_passed = 0
_failed = 0

def run_check(label: str, fn):
    global _passed, _failed
    try:
        fn()
        print(f"  [PASS] {label}")
        _passed += 1
    except Exception as e:
        print(f"  [FAIL] {label}: {e}")
        _failed += 1

def _make_jpeg(size=(200, 200)) -> str:
    from PIL import Image
    img = Image.new("RGB", size, color=(100, 150, 200))
    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    img.save(tmp.name, format="JPEG")
    tmp.close()
    return tmp.name

def _make_bmp(size=(200, 200)) -> str:
    from PIL import Image
    img = Image.new("RGB", size, color=(100, 150, 200))
    tmp = tempfile.NamedTemporaryFile(suffix=".bmp", delete=False)
    img.save(tmp.name, format="BMP")
    tmp.close()
    return tmp.name

# ── section 1: environment ────────────────────────────────────────────────────

print("\n=== Section 1: Environment ===")

def check_torch():
    import torch
    print(f"         torch {torch.__version__}", end="  ")

def check_cv2():
    import cv2
    print(f"cv2 {cv2.__version__}", end="  ")

def check_pil():
    import PIL
    print(f"PIL {PIL.__version__}", end="  ")

def check_faiss():
    import faiss
    print(f"faiss ok", end="  ")

def check_transformers():
    import transformers
    print(f"transformers {transformers.__version__}")

def check_serpapi_key():
    from dotenv import load_dotenv
    import os
    load_dotenv()
    key = os.getenv("SERPAPI_KEY")
    assert key, "SERPAPI_KEY not set in .env"

run_check("torch import", check_torch)
run_check("cv2 import", check_cv2)
run_check("PIL import", check_pil)
run_check("faiss import", check_faiss)
run_check("transformers import", check_transformers)
run_check("SERPAPI_KEY loaded", check_serpapi_key)

# ── section 2: image_processor happy path ────────────────────────────────────

print("\n=== Section 2: image_processor — happy path ===")

from image_processor import process_image
from models import ProcessedImage, ParsedListing

def check_returns_processed_image():
    path = _make_jpeg()
    result = process_image(path)
    assert isinstance(result, ProcessedImage), f"expected ProcessedImage, got {type(result)}"

def check_encoded_is_base64():
    path = _make_jpeg()
    result = process_image(path)
    assert result.encoded, "encoded field is empty"
    base64.b64decode(result.encoded)  # raises if invalid

def check_format_is_supported():
    path = _make_jpeg()
    result = process_image(path)
    assert result.format in {"JPEG", "PNG", "WEBP"}, f"unexpected format: {result.format}"

def check_size_is_valid_tuple():
    path = _make_jpeg()
    result = process_image(path)
    assert isinstance(result.size, tuple) and len(result.size) == 2
    assert result.size[0] > 0 and result.size[1] > 0

run_check("returns ProcessedImage", check_returns_processed_image)
run_check("encoded is valid base64", check_encoded_is_base64)
run_check("format in supported set", check_format_is_supported)
run_check("size is valid (w, h) tuple", check_size_is_valid_tuple)

# ── section 3: resize enforcement ────────────────────────────────────────────

print("\n=== Section 3: image_processor — resize enforcement ===")

def check_large_image_resized():
    path = _make_jpeg(size=(2000, 2000))
    result = process_image(path)
    assert result.size[0] <= 1024 and result.size[1] <= 1024, \
        f"image not resized: {result.size}"

run_check("2000x2000 image resized to <=1024x1024", check_large_image_resized)

# ── section 4: format validation ─────────────────────────────────────────────

print("\n=== Section 4: image_processor — format rejection ===")

def check_bmp_raises():
    path = _make_bmp()
    try:
        process_image(path)
        raise AssertionError("expected ValueError for BMP, got none")
    except ValueError:
        pass  # expected

run_check("BMP input raises ValueError", check_bmp_raises)

# ── section 5: (pipeline promoted to section 11) ─────────────────────────────

print("\n=== Section 5: Module presence check ===")

# ── section 6: reverse_search — searcher & request path ───────────────────────
#
# No check hits the real SerpAPI network. The key guard is exercised by
# constructing a SerpApiSearcher with an explicit key; the full request /
# response path is exercised offline via httpx.MockTransport, so no live key
# or network is required. Injectable key + client is what makes this testable.

print("\n=== Section 6: reverse_search — searcher & request path ===")

import httpx
import reverse_search as _rs
from reverse_search import SerpApiSearcher

def _tiny_image() -> ProcessedImage:
    encoded = base64.b64encode(b"not-a-real-jpeg").decode("utf-8")
    return ProcessedImage(encoded=encoded, format="JPEG", size=(1, 1))

def check_rs_import():
    assert hasattr(_rs, "SerpApiSearcher"), "missing: SerpApiSearcher"

def check_rs_missing_key_raises():
    searcher = SerpApiSearcher(api_key="")   # explicit empty key, no env fallback
    try:
        searcher._validate_key()
        raise AssertionError("expected EnvironmentError, got none")
    except EnvironmentError:
        pass

def check_rs_key_present_does_not_raise():
    SerpApiSearcher(api_key="dummy-key-for-test")._validate_key()  # should not raise

def _two_step_handler(serpapi_response: dict):
    """MockTransport handler for the upload-then-search two-request flow.

    POST to catbox host  -> returns a fake public URL string.
    GET  to serpapi host -> returns the given JSON dict.
    """
    def handler(request):
        if request.url.host == "serpapi.com":
            return httpx.Response(200, json=serpapi_response)
        return httpx.Response(200, text="https://litter.catbox.moe/test.png")
    return handler

def check_rs_search_returns_json_offline():
    client = httpx.Client(transport=httpx.MockTransport(_two_step_handler({"visual_matches": []})))
    searcher = SerpApiSearcher(api_key="dummy", client=client)
    result = searcher.search(_tiny_image())
    assert result == {"visual_matches": []}, f"unexpected payload: {result}"

def check_rs_non_200_raises_offline():
    def handler(request):
        if request.url.host == "serpapi.com":
            return httpx.Response(500, text="upstream error")
        return httpx.Response(200, text="https://litter.catbox.moe/test.png")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    searcher = SerpApiSearcher(api_key="dummy", client=client)
    try:
        searcher.search(_tiny_image())
        raise AssertionError("expected RuntimeError on non-200, got none")
    except RuntimeError:
        pass

def check_rs_upload_failure_raises():
    def handler(request):
        return httpx.Response(503, text="service unavailable")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    searcher = SerpApiSearcher(api_key="dummy", client=client)
    try:
        searcher.search(_tiny_image())
        raise AssertionError("expected RuntimeError on upload failure, got none")
    except RuntimeError:
        pass

def check_rs_pagination_merges_pages():
    """Two SerpAPI pages are merged into a single visual_matches list."""
    page_calls = [0]
    def handler(request):
        if request.url.host != "serpapi.com":
            return httpx.Response(200, text="https://litter.catbox.moe/test.png")
        idx = page_calls[0]
        page_calls[0] += 1
        if idx == 0:
            return httpx.Response(200, json={
                "visual_matches": [{"position": 1, "title": "A"}],
                "serpapi_pagination": {"next": "https://serpapi.com/search.json?page=2"},
            })
        return httpx.Response(200, json={"visual_matches": [{"position": 21, "title": "B"}]})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    searcher = SerpApiSearcher(api_key="dummy", client=client, max_pages=2)
    result = searcher.search(_tiny_image())
    assert len(result.get("visual_matches", [])) == 2, \
        f"expected 2 merged matches, got {len(result.get('visual_matches', []))}"

def check_rs_single_page_when_no_next():
    """When serpapi_pagination.next is absent, only one page is fetched."""
    page_calls = [0]
    def handler(request):
        if request.url.host != "serpapi.com":
            return httpx.Response(200, text="https://litter.catbox.moe/test.png")
        page_calls[0] += 1
        return httpx.Response(200, json={"visual_matches": [{"position": 1, "title": "A"}]})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    searcher = SerpApiSearcher(api_key="dummy", client=client, max_pages=3)
    result = searcher.search(_tiny_image())
    assert len(result.get("visual_matches", [])) == 1
    assert page_calls[0] == 1, f"expected 1 SerpAPI call, got {page_calls[0]}"

run_check("reverse_search exposes SerpApiSearcher", check_rs_import)
run_check("_validate_key raises EnvironmentError when key is empty", check_rs_missing_key_raises)
run_check("_validate_key passes when key is present", check_rs_key_present_does_not_raise)
run_check("search() upload-then-search flow returns parsed JSON via MockTransport", check_rs_search_returns_json_offline)
run_check("search() raises RuntimeError on non-200 SerpAPI response", check_rs_non_200_raises_offline)
run_check("search() raises RuntimeError on upload failure", check_rs_upload_failure_raises)
run_check("search() merges two pages of visual_matches via pagination", check_rs_pagination_merges_pages)
run_check("search() fetches only one page when serpapi_pagination.next is absent", check_rs_single_page_when_no_next)

# ── section 7: marketplace_parser — filter, extraction & sort ─────────────────
#
# All checks use in-process fixture data so no network is required.
# FIXTURE covers five distinct cases:
#   [0] Amazon listing, valid price $29.99   → should survive, sorted 2nd
#   [1] eBay listing, valid price $24.50     → should survive, sorted 1st
#   [2] Non-marketplace (Blogger), no price  → dropped in _extract (no price block)
#   [3] Amazon listing, price = 0.0          → dropped in _passes_filter (zero price)
#   [4] Amazon listing, javascript: URL      → dropped in _passes_filter (bad scheme)

print("\n=== Section 7: marketplace_parser — filter, extraction & sort ===")

from datetime import datetime, timedelta, timezone
from marketplace_parser import parse, _parse_date, _is_within_12_months

_FIXTURE = {
    "visual_matches": [
        {
            "title": "Blue Widget Pro",
            "link": "https://www.amazon.com/dp/B001",
            "source": "Amazon",
            "price": {"value": "$29.99", "extracted_value": 29.99, "currency": "$"},
        },
        {
            "title": "Blue Widget",
            "link": "https://www.ebay.com/itm/12345",
            "source": "eBay",
            "price": {"value": "$24.50", "extracted_value": 24.50, "currency": "$"},
        },
        {
            "title": "Unrelated Blog Post",
            "link": "https://www.blogger.com/post/123",
            "source": "Blogger",
        },
        {
            "title": "Contact For Price Item",
            "link": "https://www.amazon.com/dp/B003",
            "source": "Amazon",
            "price": {"value": "$0", "extracted_value": 0.0, "currency": "$"},
        },
        {
            "title": "Sponsored Slot",
            "link": "javascript:void(0)",
            "source": "Amazon",
            "price": {"value": "$19.99", "extracted_value": 19.99, "currency": "$"},
        },
    ]
}

def check_returns_only_valid_listings():
    results = parse(_FIXTURE)
    assert len(results) == 2, f"expected 2 listings, got {len(results)}"

def check_all_results_are_parsed_listing():
    results = parse(_FIXTURE)
    for r in results:
        assert isinstance(r, ParsedListing), f"expected ParsedListing, got {type(r)}"

def check_non_marketplace_filtered():
    results = parse(_FIXTURE)
    sources = {r.source for r in results}
    assert "Blogger" not in sources, "non-marketplace source leaked through filter"

def check_zero_price_filtered():
    results = parse(_FIXTURE)
    assert all(r.price_value > 0 for r in results), "zero-price listing leaked through filter"

def check_bad_url_scheme_filtered():
    results = parse(_FIXTURE)
    assert all(r.url.startswith(("http://", "https://")) for r in results), \
        "non-http URL leaked through filter"

def check_no_price_block_dropped():
    result = parse({"visual_matches": [{"title": "X", "link": "https://x.com", "source": "Amazon"}]})
    assert result == [], "entry with no price block should produce empty list"

def check_empty_response():
    assert parse({}) == [], "empty response should return empty list"

def check_parsed_listing_fields():
    results = parse(_FIXTURE)
    ebay = next(r for r in results if "ebay" in r.source.lower())
    assert ebay.title == "Blue Widget"
    assert ebay.price_raw == "$24.50"
    assert ebay.currency == "$"

run_check("only 2 of 5 fixtures survive filter", check_returns_only_valid_listings)
run_check("all results are ParsedListing instances", check_all_results_are_parsed_listing)
run_check("non-marketplace source (Blogger) filtered out", check_non_marketplace_filtered)
run_check("zero-price entry filtered out", check_zero_price_filtered)
run_check("javascript: URL filtered out", check_bad_url_scheme_filtered)
run_check("entry with no price block produces empty list", check_no_price_block_dropped)
run_check("empty serpapi response returns empty list", check_empty_response)
run_check("ParsedListing fields match fixture values", check_parsed_listing_fields)

# ── date filtering ────────────────────────────────────────────────────────────

def _dated_match(date_str: str) -> dict:
    return {
        "title": "Test Item",
        "link": "https://www.amazon.com/dp/B999",
        "source": "Amazon",
        "price": {"value": "$10.00", "extracted_value": 10.0, "currency": "$"},
        "date": date_str,
    }

def check_old_listing_dropped():
    old = datetime.now(tz=timezone.utc) - timedelta(days=400)
    date_str = old.strftime("%Y-%m-%d")
    result = parse({"visual_matches": [_dated_match(date_str)]})
    assert result == [], f"listing older than 12 months should be dropped, got {result}"

def check_recent_listing_kept():
    recent = datetime.now(tz=timezone.utc) - timedelta(days=30)
    date_str = recent.strftime("%Y-%m-%d")
    result = parse({"visual_matches": [_dated_match(date_str)]})
    assert len(result) == 1, f"listing from 30 days ago should be kept, got {result}"

def check_no_date_listing_kept():
    result = parse({"visual_matches": [_dated_match("")]})
    assert len(result) == 1, "listing with no date should pass through (undated = unknown age)"

def check_parse_date_iso():
    d = _parse_date("2025-06-15")
    assert isinstance(d, datetime), "ISO date string should parse to datetime"
    assert d.year == 2025 and d.month == 6 and d.day == 15

def check_parse_date_us_format():
    d = _parse_date("Jan 15, 2025")
    assert isinstance(d, datetime), "US date string should parse to datetime"
    assert d.month == 1 and d.day == 15

def check_parse_date_relative_months():
    d = _parse_date("3 months ago")
    assert isinstance(d, datetime), "relative date string should parse to datetime"
    expected = datetime.now(tz=timezone.utc) - timedelta(days=90)
    assert abs((d - expected).total_seconds()) < 5

def check_parse_date_relative_year():
    d = _parse_date("1 year ago")
    assert isinstance(d, datetime)
    expected = datetime.now(tz=timezone.utc) - timedelta(days=365)
    assert abs((d - expected).total_seconds()) < 5

def check_parse_date_invalid_returns_none():
    assert _parse_date("not a date at all") is None
    assert _parse_date("") is None

def check_is_within_12_months_none():
    assert _is_within_12_months(None) is True, "None sold_date should pass through"

def check_is_within_12_months_old():
    old = datetime.now(tz=timezone.utc) - timedelta(days=400)
    assert _is_within_12_months(old) is False, "date > 12 months ago should fail"

def check_is_within_12_months_recent():
    recent = datetime.now(tz=timezone.utc) - timedelta(days=10)
    assert _is_within_12_months(recent) is True, "date 10 days ago should pass"

run_check("listing older than 12 months is dropped", check_old_listing_dropped)
run_check("listing from 30 days ago is kept", check_recent_listing_kept)
run_check("listing with no date passes through", check_no_date_listing_kept)
run_check("_parse_date handles ISO format", check_parse_date_iso)
run_check("_parse_date handles US month format", check_parse_date_us_format)
run_check("_parse_date handles relative 'N months ago'", check_parse_date_relative_months)
run_check("_parse_date handles relative '1 year ago'", check_parse_date_relative_year)
run_check("_parse_date returns None for unparseable strings", check_parse_date_invalid_returns_none)
run_check("_is_within_12_months(None) returns True", check_is_within_12_months_none)
run_check("_is_within_12_months rejects date > 365 days ago", check_is_within_12_months_old)
run_check("_is_within_12_months accepts date 10 days ago", check_is_within_12_months_recent)

# ── section 7b: marketplace_parser — similarity scoring ──────────────────────
#
# _score() uses SerpAPI position (1 = best visual match) to produce a 1-10 score.
# Uses a separate fixture with explicit position values so scoring checks are
# independent of _FIXTURE (which is shared with Sections 9 and 11).
#
# SCORE_FIXTURE:
#   [0] position=1  Amazon $20 -> score 10 (best match)
#   [1] position=90 eBay   $20 -> score 5  (mid-range)
#
# INDEX_FIXTURE: no position field -> fallback to list index
#   [0] index 0 -> higher score than [1] index 1

from marketplace_parser import _score

_SCORE_FIXTURE = {
    "visual_matches": [
        {
            "title": "High Sim Widget", "link": "https://www.amazon.com/dp/B100",
            "source": "Amazon",
            "price": {"value": "$20.00", "extracted_value": 20.0, "currency": "$"},
            "position": 1,
        },
        {
            "title": "Low Sim Widget", "link": "https://www.ebay.com/itm/99999",
            "source": "eBay",
            "price": {"value": "$20.00", "extracted_value": 20.0, "currency": "$"},
            "position": 90,
        },
    ]
}

_INDEX_FIXTURE = {
    "visual_matches": [
        {
            "title": "First Match", "link": "https://www.amazon.com/dp/B200",
            "source": "Amazon",
            "price": {"value": "$30.00", "extracted_value": 30.0, "currency": "$"},
        },
        {
            "title": "Second Match", "link": "https://www.ebay.com/itm/88888",
            "source": "eBay",
            "price": {"value": "$30.00", "extracted_value": 30.0, "currency": "$"},
        },
    ]
}

def check_score_top_position_is_10():
    score = _score({"position": 1}, 0)
    assert score == 10, f"position 1 should score 10, got {score}"

def check_score_last_position_is_1():
    score = _score({"position": 180}, 0)
    assert score == 1, f"position 180 should score 1, got {score}"

def check_score_reflects_position_order():
    results = parse(_SCORE_FIXTURE)
    high = next(r for r in results if "High" in r.title)
    low  = next(r for r in results if "Low"  in r.title)
    assert high.similarity_score > low.similarity_score, (
        f"position-1 entry should outscore position-90: {high.similarity_score} vs {low.similarity_score}"
    )

def check_score_in_range():
    results = parse(_SCORE_FIXTURE)
    for r in results:
        assert 1 <= r.similarity_score <= 10, f"score out of range: {r.similarity_score}"

def check_score_index_fallback():
    results = parse(_INDEX_FIXTURE)
    assert len(results) == 2
    first  = next(r for r in results if "First"  in r.title)
    second = next(r for r in results if "Second" in r.title)
    assert first.similarity_score >= second.similarity_score, (
        f"index-0 entry should score >= index-1: {first.similarity_score} vs {second.similarity_score}"
    )

def check_default_similarity_score():
    listing = ParsedListing(
        title="x", url="https://amazon.com", source="Amazon",
        price_raw="$10", price_value=10.0, currency="$",
    )
    assert listing.similarity_score == 1, "default similarity_score must be 1"

run_check("score: position 1 maps to 10", check_score_top_position_is_10)
run_check("score: position 180 maps to 1", check_score_last_position_is_1)
run_check("score: lower position entry scores higher than higher position", check_score_reflects_position_order)
run_check("score: all values in [1, 10]", check_score_in_range)
run_check("score: fallback to list index when position absent", check_score_index_fallback)
run_check("ParsedListing default similarity_score is 1", check_default_similarity_score)

# ── section 8: agent_review — tool unit tests ─────────────────────────────────
#
# Tests the tool-implementation layer of agent_review without calling the
# Claude API or hitting the network. Each function under test is imported
# directly so the agent loop itself is never invoked.

print("\n=== Section 8: agent_review — tool implementations ===")

# agent_review.py lives at the project root, not in src/ — add root to sys.path
import pathlib as _pl
_proj_root = str(_pl.Path(__file__).parent.parent)
if _proj_root not in sys.path:
    sys.path.insert(0, _proj_root)

def _import_agent():
    import agent_review as _ar
    return _ar

def check_ar_import():
    _ar = _import_agent()
    for symbol in ("run_agent", "dispatch", "TOOLS", "_tool_read_source_file",
                   "_tool_scan_scalability", "_tool_browser_check_url"):
        assert hasattr(_ar, symbol), f"missing: {symbol}"

def check_read_existing_file():
    _ar = _import_agent()
    content = _ar._tool_read_source_file("src/image_processor.py")
    assert "ProcessedImage" in content, "expected ProcessedImage in image_processor.py"

def check_read_missing_file():
    _ar = _import_agent()
    result = _ar._tool_read_source_file("src/does_not_exist.py")
    assert result.startswith("ERROR:"), f"expected ERROR prefix, got: {result[:60]}"

def check_scan_scalability_finds_httpx():
    _ar = _import_agent()
    result = _ar._tool_scan_scalability("src/reverse_search.py")
    assert isinstance(result, str), "scan_scalability must return a string"
    assert "httpx" in result.lower() or "Client" in result, (
        "expected a sync httpx.Client finding in reverse_search.py"
    )

def check_scan_scalability_missing_file():
    _ar = _import_agent()
    result = _ar._tool_scan_scalability("src/does_not_exist.py")
    assert result.startswith("ERROR:"), f"expected ERROR prefix, got: {result[:60]}"

def check_scan_scalability_clean_file():
    _ar = _import_agent()
    result = _ar._tool_scan_scalability("src/marketplace_parser.py")
    assert isinstance(result, str)
    # marketplace_parser.py has no httpx or while-True — should be clean
    assert "No scalability concerns" in result or "!" not in result

def check_dispatch_unknown_tool():
    _ar = _import_agent()
    result = _ar.dispatch("nonexistent_tool", {})
    assert result.startswith("ERROR:"), f"expected ERROR prefix, got: {result[:60]}"

def check_browser_rejects_bad_scheme():
    _ar = _import_agent()
    result = _ar._tool_browser_check_url("javascript:void(0)")
    assert result.startswith("ERROR:"), f"expected ERROR for bad URL scheme, got: {result[:60]}"

def check_browser_rejects_relative_url():
    _ar = _import_agent()
    result = _ar._tool_browser_check_url("/local/path")
    assert result.startswith("ERROR:"), f"expected ERROR for relative URL, got: {result[:60]}"

def check_tools_list_has_required_entries():
    _ar = _import_agent()
    names = {t["name"] for t in _ar.TOOLS}
    for required in ("read_source_file", "run_bandit", "run_pip_audit",
                     "run_tests", "scan_scalability", "browser_check_url"):
        assert required in names, f"TOOLS missing entry: {required}"

def check_bandit_returns_string():
    _ar = _import_agent()
    result = _ar._tool_run_bandit("src/")
    assert isinstance(result, str) and len(result) > 0

def check_pip_audit_returns_string():
    _ar = _import_agent()
    result = _ar._tool_run_pip_audit()
    assert isinstance(result, str) and len(result) > 0

run_check("agent_review imports with expected public surface", check_ar_import)
run_check("read_source_file returns content for existing file", check_read_existing_file)
run_check("read_source_file returns ERROR for missing file", check_read_missing_file)
run_check("scan_scalability flags sync httpx.Client in reverse_search.py", check_scan_scalability_finds_httpx)
run_check("scan_scalability returns ERROR for missing file", check_scan_scalability_missing_file)
run_check("scan_scalability reports clean for marketplace_parser.py", check_scan_scalability_clean_file)
run_check("dispatch returns ERROR for unknown tool name", check_dispatch_unknown_tool)
run_check("browser_check_url rejects javascript: scheme", check_browser_rejects_bad_scheme)
run_check("browser_check_url rejects relative path", check_browser_rejects_relative_url)
run_check("TOOLS list contains all six required entries", check_tools_list_has_required_entries)
run_check("run_bandit returns a non-empty string", check_bandit_returns_string)
run_check("run_pip_audit returns a non-empty string", check_pip_audit_returns_string)

# ── section 9: price_aggregator — ranking ─────────────────────────────────────
#
# rank_by_price owns the sort that used to live in marketplace_parser.parse().
# Given unsorted ParsedListings it must return them cheapest-first without
# mutating the input. The end-to-end check confirms parse() → rank is ascending.

print("\n=== Section 9: price_aggregator — ranking ===")

from price_aggregator import rank_by_price

def _listing(price: float, title: str = "x") -> ParsedListing:
    return ParsedListing(
        title=title, url="https://x.com", source="Amazon",
        price_raw=f"${price}", price_value=price, currency="$",
    )

def check_rank_sorts_ascending():
    ranked = rank_by_price([_listing(29.99), _listing(24.50), _listing(99.00)])
    assert [l.price_value for l in ranked] == [24.50, 29.99, 99.00], \
        f"not ascending: {[l.price_value for l in ranked]}"

def check_rank_does_not_mutate_input():
    unsorted = [_listing(29.99), _listing(24.50)]
    rank_by_price(unsorted)
    assert [l.price_value for l in unsorted] == [29.99, 24.50], "input list was mutated"

def check_rank_empty_list():
    assert rank_by_price([]) == []

def check_parse_then_rank_is_ascending():
    prices = [l.price_value for l in rank_by_price(parse(_FIXTURE))]
    assert prices == sorted(prices), f"not ascending after rank: {prices}"

run_check("rank_by_price sorts ascending by price_value", check_rank_sorts_ascending)
run_check("rank_by_price does not mutate its input", check_rank_does_not_mutate_input)
run_check("rank_by_price handles empty list", check_rank_empty_list)
run_check("parse() then rank_by_price yields ascending prices", check_parse_then_rank_is_ascending)

# ── section 10: price_aggregator — aggregate() ────────────────────────────────
#
# FIXTURES:
#   active_a  — $20.00, no sold_date  (active listing)
#   active_b  — $30.00, no sold_date  (active listing)
#   sold_a    — $15.00, sold_date set (completed sale)
#   sold_b    — $25.00, sold_date set (completed sale)
#
# Expected:
#   avg_listing_price = (20 + 30) / 2 = 25.00
#   avg_sold_price    = (15 + 25) / 2 = 20.00
#   sold_count        = 2
#   listing_count     = 2
#   listings sorted   = [15.00, 20.00, 25.00, 30.00]

print("\n=== Section 10: price_aggregator — aggregate() ===")

from datetime import datetime, timezone
from price_aggregator import aggregate
from models import PriceReport

_SOLD_DATE = datetime(2025, 1, 1, tzinfo=timezone.utc)

def _active(price: float) -> ParsedListing:
    return ParsedListing(
        title="x", url="https://amazon.com", source="Amazon",
        price_raw=f"${price}", price_value=price, currency="$",
        sold_date=None,
    )

def _sold(price: float) -> ParsedListing:
    return ParsedListing(
        title="x", url="https://amazon.com", source="Amazon",
        price_raw=f"${price}", price_value=price, currency="$",
        sold_date=_SOLD_DATE,
    )

_MIX = [_active(20.00), _active(30.00), _sold(15.00), _sold(25.00)]

def check_aggregate_returns_price_report():
    report = aggregate(_MIX)
    assert isinstance(report, PriceReport), f"expected PriceReport, got {type(report)}"

def check_aggregate_avg_listing_price():
    report = aggregate(_MIX)
    assert report.avg_listing_price == 25.00, \
        f"avg_listing_price: expected 25.00, got {report.avg_listing_price}"

def check_aggregate_avg_sold_price():
    report = aggregate(_MIX)
    assert report.avg_sold_price == 20.00, \
        f"avg_sold_price: expected 20.00, got {report.avg_sold_price}"

def check_aggregate_sold_count():
    report = aggregate(_MIX)
    assert report.sold_count == 2, f"sold_count: expected 2, got {report.sold_count}"

def check_aggregate_listing_count():
    report = aggregate(_MIX)
    assert report.listing_count == 2, f"listing_count: expected 2, got {report.listing_count}"

def check_aggregate_listings_sorted():
    report = aggregate(_MIX)
    prices = [l.price_value for l in report.listings]
    assert prices == sorted(prices), f"listings not sorted ascending: {prices}"

def check_aggregate_no_sold_listings():
    report = aggregate([_active(10.00), _active(20.00)])
    assert report.avg_sold_price == 0.0, "avg_sold_price must be 0.0 when no sold listings"
    assert report.sold_count == 0

def check_aggregate_all_sold():
    report = aggregate([_sold(10.00), _sold(20.00)])
    assert report.avg_listing_price == 0.0, "avg_listing_price must be 0.0 when all are sold"
    assert report.listing_count == 0

def check_aggregate_empty():
    report = aggregate([])
    assert report.avg_listing_price == 0.0
    assert report.avg_sold_price == 0.0
    assert report.sold_count == 0
    assert report.listing_count == 0
    assert report.listings == []
    assert report.currency == "$"

def check_aggregate_currency_from_first_listing():
    listings = [_active(10.00)]
    listings[0] = ParsedListing(
        title="x", url="https://amazon.com", source="Amazon",
        price_raw="€10", price_value=10.0, currency="€",
    )
    report = aggregate(listings)
    assert report.currency == "€", f"expected €, got {report.currency}"

run_check("aggregate() returns a PriceReport instance", check_aggregate_returns_price_report)
run_check("avg_listing_price is mean of active-only listings", check_aggregate_avg_listing_price)
run_check("avg_sold_price is mean of sold-only listings", check_aggregate_avg_sold_price)
run_check("sold_count matches number of listings with sold_date", check_aggregate_sold_count)
run_check("listing_count matches number of active (unsold) listings", check_aggregate_listing_count)
run_check("PriceReport.listings is sorted ascending by price", check_aggregate_listings_sorted)
run_check("avg_sold_price is 0.0 when no sold listings exist", check_aggregate_no_sold_listings)
run_check("avg_listing_price is 0.0 when all listings are sold", check_aggregate_all_sold)
run_check("empty input returns zero-valued PriceReport", check_aggregate_empty)
run_check("currency is taken from the first listing", check_aggregate_currency_from_first_listing)

# ── section 11: pipeline — run() and format_report() ─────────────────────────
#
# run() is tested offline using a mock ReverseSearchProvider so no network call
# is made. format_report() is tested against a hand-built PriceReport.
#
# MOCK PROVIDER returns the same _FIXTURE dict used in Section 7, which after
# parse() yields 2 valid listings (Amazon $29.99, eBay $24.50 — both active).
# aggregate() produces:
#   avg_listing_price = (24.50 + 29.99) / 2 = 27.245 → 27.25
#   avg_sold_price    = 0.0   (no sold listings in fixture)
#   sold_count        = 0
#   listing_count     = 2

print("\n=== Section 11: pipeline — run() and format_report() ===")

from pipeline import run, format_report
from models import PriceReport

class _MockProvider:
    """Returns the Section 7 fixture so run() can be tested without a network."""
    def search(self, image):
        return _FIXTURE


def _mock_run(image_path: str) -> PriceReport:
    """run() called with a real temp image and the mock provider."""
    return run(image_path, _MockProvider())


def check_pipeline_returns_price_report():
    path = _make_jpeg()
    report = _mock_run(path)
    assert isinstance(report, PriceReport), f"expected PriceReport, got {type(report)}"


def check_pipeline_listing_count():
    path = _make_jpeg()
    report = _mock_run(path)
    assert report.listing_count == 2, f"expected 2 active listings, got {report.listing_count}"


def check_pipeline_avg_listing_price():
    path = _make_jpeg()
    report = _mock_run(path)
    expected = round((24.50 + 29.99) / 2, 2)
    assert report.avg_listing_price == expected, (
        f"expected avg_listing_price={expected}, got {report.avg_listing_price}"
    )


def check_pipeline_no_sold_listings():
    path = _make_jpeg()
    report = _mock_run(path)
    assert report.sold_count == 0, f"expected sold_count=0, got {report.sold_count}"
    assert report.avg_sold_price == 0.0, f"expected avg_sold_price=0.0, got {report.avg_sold_price}"


def check_pipeline_listings_sorted():
    path = _make_jpeg()
    report = _mock_run(path)
    prices = [l.price_value for l in report.listings]
    assert prices == sorted(prices), f"listings not sorted ascending: {prices}"


def check_pipeline_empty_provider():
    """Provider returns no visual_matches → empty PriceReport."""
    class _EmptyProvider:
        def search(self, image):
            return {}
    path = _make_jpeg()
    report = run(path, _EmptyProvider())
    assert isinstance(report, PriceReport)
    assert report.listing_count == 0 and report.sold_count == 0


def check_format_report_contains_key_fields():
    report = PriceReport(
        listings=[],
        avg_listing_price=27.25,
        avg_sold_price=15.00,
        sold_count=3,
        listing_count=2,
        currency="$",
    )
    output = format_report(report)
    assert "27.25" in output, "avg listing price missing from format_report output"
    assert "15.00" in output, "avg sold price missing from format_report output"
    assert "3" in output, "sold_count missing from format_report output"
    assert "2" in output, "listing_count missing from format_report output"


def check_format_report_no_listings_message():
    report = PriceReport(
        listings=[],
        avg_listing_price=0.0,
        avg_sold_price=0.0,
        sold_count=0,
        listing_count=0,
        currency="$",
    )
    output = format_report(report)
    assert "No valid listings found" in output


def check_format_report_shows_similarity():
    from datetime import datetime, timezone
    listing = ParsedListing(
        title="Widget", url="https://amazon.com/dp/1",
        source="Amazon", price_raw="$10.00", price_value=10.0,
        currency="$", similarity_score=8,
    )
    report = PriceReport(
        listings=[listing],
        avg_listing_price=10.0,
        avg_sold_price=0.0,
        sold_count=0,
        listing_count=1,
        currency="$",
    )
    output = format_report(report)
    assert "8/10" in output, "similarity score column missing from format_report output"


def check_format_report_sold_tag():
    from datetime import datetime, timezone
    sold_listing = ParsedListing(
        title="Old Widget", url="https://ebay.com/itm/1",
        source="eBay", price_raw="$10.00", price_value=10.0,
        currency="$", sold_date=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    report = PriceReport(
        listings=[sold_listing],
        avg_listing_price=0.0,
        avg_sold_price=10.0,
        sold_count=1,
        listing_count=0,
        currency="$",
    )
    output = format_report(report)
    assert "[sold]" in output, "sold tag missing for listing with sold_date"


run_check("run() returns a PriceReport", check_pipeline_returns_price_report)
run_check("run() listing_count matches valid fixture listings", check_pipeline_listing_count)
run_check("run() avg_listing_price is mean of active prices", check_pipeline_avg_listing_price)
run_check("run() reports zero sold when fixture has no sold listings", check_pipeline_no_sold_listings)
run_check("run() listings are sorted ascending by price", check_pipeline_listings_sorted)
run_check("run() with empty provider returns zero-valued PriceReport", check_pipeline_empty_provider)
run_check("format_report() includes avg prices and counts", check_format_report_contains_key_fields)
run_check("format_report() shows 'No valid listings found' when empty", check_format_report_no_listings_message)
run_check("format_report() shows similarity score column (N/10)", check_format_report_shows_similarity)
run_check("format_report() shows [sold] tag for sold listings", check_format_report_sold_tag)

# ── section 12: trending models — dataclass construction ─────────────────────

print("\n=== Section 12: trending models — dataclass construction ===")

from models import KeywordSignal, VolumeSignal, SoldSignal, TrendingItem, TrendingProvider

_NOW = datetime.now(tz=timezone.utc)

def check_keyword_signal_fields():
    k = KeywordSignal(item_id="123", keyword="widget", rank=1, fetched_at=_NOW,
                      title="Widget", url="https://ebay.com/itm/123")
    assert k.item_id == "123" and k.rank == 1 and k.fetched_at == _NOW
    assert k.title == "Widget" and k.url == "https://ebay.com/itm/123"

def check_keyword_signal_optional_title_url():
    k = KeywordSignal(item_id="123", keyword="widget", rank=1, fetched_at=_NOW)
    assert k.title == "" and k.url == "" and k.category == ""

def check_keyword_signal_category():
    k = KeywordSignal(item_id="123", keyword="flare jeans vintage", rank=1,
                      fetched_at=_NOW, category="Denim")
    assert k.category == "Denim"

def check_volume_signal_fields():
    v = VolumeSignal(item_id="456", title="Gadget", sold_quantity=200, fetched_at=_NOW)
    assert v.sold_quantity == 200

def check_sold_signal_fields():
    s = SoldSignal(
        item_id="789", title="Gizmo", sold_count=40, total_count=50,
        sold_rate=0.8, last_sold=_NOW, fetched_at=_NOW,
    )
    assert s.sold_rate == 0.8 and s.last_sold == _NOW

def check_trending_item_fields():
    it = TrendingItem(
        item_id="1", title="T", url="https://x.com", source="eBay",
        rank=1, score=4.5, keyword_rank=1, sold_quantity=100, sold_rate=0.5,
        norm_keyword=1.0, norm_volume=0.8, norm_sold=0.6, category="Denim",
    )
    assert it.rank == 1 and it.score == 4.5 and it.category == "Denim"

def check_trending_item_category_defaults_empty():
    it = TrendingItem(
        item_id="1", title="T", url="", source="eBay", rank=1, score=1.0,
        keyword_rank=1, sold_quantity=None, sold_rate=None,
        norm_keyword=1.0, norm_volume=0.0, norm_sold=0.0,
    )
    assert it.category == ""

def check_trending_item_optional_none():
    it = TrendingItem(
        item_id="2", title="T2", url="", source="eBay",
        rank=2, score=1.0,
        keyword_rank=None, sold_quantity=None, sold_rate=None,
        norm_keyword=0.0, norm_volume=0.0, norm_sold=0.0,
    )
    assert it.keyword_rank is None and it.sold_quantity is None

run_check("KeywordSignal constructs with correct fields", check_keyword_signal_fields)
run_check("KeywordSignal title/url/category default to empty string", check_keyword_signal_optional_title_url)
run_check("KeywordSignal carries a v3 category", check_keyword_signal_category)
run_check("VolumeSignal constructs with correct fields", check_volume_signal_fields)
run_check("SoldSignal constructs with correct fields (including last_sold)", check_sold_signal_fields)
run_check("TrendingItem constructs with all fields (incl. category)", check_trending_item_fields)
run_check("TrendingItem category defaults to empty string", check_trending_item_category_defaults_empty)
run_check("TrendingItem accepts None for optional signal fields", check_trending_item_optional_none)

# ── section 13: trending_scorer — normalization, filtering, and ranking ───────

print("\n=== Section 13: trending_scorer — normalization, filtering, and ranking ===")

import trending_scorer
from trending_scorer import (
    score_trending, _min_max, _passes_predicate, _passes_category_filter,
    select_enrichment_ids, CATEGORY_TAXONOMY, EXCLUDED_ITEM_TYPES,
    KEYWORD_WEIGHT, VOLUME_WEIGHT, SOLD_WEIGHT,
    TOP_N_PER_CATEGORY, GETITEM_PER_CATEGORY,
)

# v3: candidates must carry a category (from the seed) and a title that passes the
# category's inclusion filter, else they are dropped before scoring. Default to a
# Denim garment so the existing scoring/normalization checks exercise one category.
def _kw(item_id, rank, category="Denim", title="vintage denim jeans"):
    return KeywordSignal(item_id=item_id, keyword="x", rank=rank, fetched_at=_NOW,
                         title=title, category=category)

def _v(item_id, count, title=""):
    return VolumeSignal(item_id=item_id, title=title, sold_quantity=count, fetched_at=_NOW)

def _s(item_id, sold_count, total_count, last_sold=None):
    rate = sold_count / total_count if total_count > 0 else 0.0
    return SoldSignal(
        item_id=item_id, title="", sold_count=sold_count,
        total_count=total_count, sold_rate=rate,
        last_sold=last_sold or _NOW, fetched_at=_NOW,
    )

def check_min_max_basic():
    result = _min_max([0.0, 50.0, 100.0])
    assert result == [0.0, 0.5, 1.0], f"unexpected: {result}"

def check_min_max_all_equal():
    result = _min_max([5.0, 5.0, 5.0])
    assert result == [1.0, 1.0, 1.0], f"all-equal should be 1.0: {result}"

def check_min_max_with_none():
    result = _min_max([None, 0.0, 100.0])
    assert result[0] == 0.0, "None should map to 0.0"
    assert result[1] == 0.0 and result[2] == 1.0, f"unexpected: {result}"

def check_min_max_all_none():
    result = _min_max([None, None])
    assert result == [0.0, 0.0]

def check_scorer_worked_example():
    """Reproduces the worked example from Section 6 of the architecture doc."""
    kw  = [_kw("A", 1), _kw("B", 2)]
    w   = [_v("A", 500), _v("B", 100)]
    s   = [_s("A", 40, 100), _s("B", 10, 100)]
    items = score_trending(kw, w, s)
    assert len(items) == 2
    a = next(it for it in items if it.item_id == "A")
    b = next(it for it in items if it.item_id == "B")
    assert a.rank == 1 and b.rank == 2
    assert abs(a.score - 5.0) < 1e-9, f"A score: {a.score}"
    assert abs(b.score - 0.0) < 1e-9, f"B score: {b.score}"

def check_scorer_single_candidate():
    """Single candidate, all equal → norm = 1.0, score = 5.0."""
    kw = [_kw("X", 1)]
    w  = [_v("X", 100)]
    s  = [_s("X", 10, 20)]
    items = score_trending(kw, w, s)
    assert len(items) == 1
    assert items[0].score == 5.0

def check_scorer_missing_volume_within_category():
    """A categorized candidate missing the volume signal gets norm_volume=0.0."""
    kw = [_kw("A", 1), _kw("B", 2)]                 # both Denim, pass inclusion
    s  = [_s("A", 10, 20), _s("B", 5, 20)]
    items = score_trending(kw, [], s)               # no volume signals supplied
    assert items, "categorized candidates should survive on keyword + sold alone"
    for it in items:
        assert it.norm_volume == 0.0, f"expected 0.0, got {it.norm_volume}"
        assert it.category == "Denim"

def check_scorer_drops_off_category_titles():
    """Inclusion/exclusion filter drops a non-garment title even if categorized."""
    kw = [
        _kw("GOOD", 1, title="vintage Levi's denim jeans"),  # passes inclusion
        _kw("BELT", 2, title="vintage leather jacket belt"), # belt → excluded
        _kw("NONE", 3, title="vintage ceramic mug"),         # no Denim keyword
    ]
    items = score_trending(kw, [], [])
    ids = [it.item_id for it in items]
    assert "GOOD" in ids
    assert "BELT" not in ids, f"belt accessory leaked: {ids}"
    assert "NONE" not in ids, f"off-category title leaked: {ids}"

def check_scorer_top_n_per_category():
    """More than TOP_N_PER_CATEGORY in one category → only that many returned."""
    kw = [_kw(str(i), i) for i in range(1, 16)]     # 15 Denim candidates
    v  = [_v(str(i), i * 10) for i in range(1, 16)]
    items = score_trending(kw, v, [])
    assert len(items) == TOP_N_PER_CATEGORY, f"expected {TOP_N_PER_CATEGORY}, got {len(items)}"

def check_scorer_groups_multiple_categories():
    """Items span categories; each category contributes its own ranked block."""
    kw = [
        _kw("D1", 1, category="Denim", title="vintage flare jeans"),
        _kw("D2", 2, category="Denim", title="vintage baggy jeans"),
        _kw("T1", 1, category="Tops",  title="vintage band tee"),
    ]
    items = score_trending(kw, [], [])
    cats = {it.category for it in items}
    assert cats == {"Denim", "Tops"}, f"unexpected categories: {cats}"
    # rank is within-category: Denim has a rank-1 and a rank-2; Tops has a rank-1
    denim_ranks = sorted(it.rank for it in items if it.category == "Denim")
    tops_ranks  = sorted(it.rank for it in items if it.category == "Tops")
    assert denim_ranks == [1, 2] and tops_ranks == [1]

def check_scorer_rank_within_category():
    """rank counts from 1 within each category."""
    kw = [_kw("A", 1), _kw("B", 2)]
    v  = [_v("A", 100), _v("B", 50)]
    items = score_trending(kw, v, [])
    ranks = sorted(it.rank for it in items)
    assert ranks == [1, 2]

def check_scorer_tiebreak_by_sold_quantity():
    """Equal scores within a category broken by sold_quantity descending."""
    # A and B both end at score 2.0 (A: keyword-max/volume-min; B: keyword-min/
    # volume-max); C sits below. The A/B tie must resolve to B (higher sold qty).
    kw = [_kw("A", 1), _kw("B", 2), _kw("C", 2)]
    v  = [_v("A", 100), _v("B", 300), _v("C", 200)]
    items = score_trending(kw, v, [])
    a = next(it for it in items if it.item_id == "A")
    b = next(it for it in items if it.item_id == "B")
    assert abs(a.score - b.score) < 1e-9, f"expected a tie: {a.score} vs {b.score}"
    assert b.rank < a.rank, "higher sold quantity should rank first on a tie"

def check_passes_predicate_noise_gate():
    # no units sold, zero sell-through, not surfaced by search → dropped
    assert not _passes_predicate(0, 0.0, None, None, _NOW, _NOW)

def check_passes_predicate_good():
    assert _passes_predicate(100, 0.5, 1, _NOW, _NOW, _NOW)

def check_passes_predicate_keyword_only_survives():
    # zero engagement but surfaced by Best Match search → kept
    assert _passes_predicate(0, 0.0, 3, None, _NOW, _NOW)

def check_passes_predicate_recency_too_old():
    old = _NOW - timedelta(days=90)
    assert not _passes_predicate(100, 0.5, 1, old, old, _NOW)

def check_passes_predicate_data_presence():
    assert not _passes_predicate(None, None, None, _NOW, _NOW, _NOW)

run_check("_min_max basic normalization [0, 50, 100]", check_min_max_basic)
run_check("_min_max all-equal values → all 1.0", check_min_max_all_equal)
run_check("_min_max None values → 0.0", check_min_max_with_none)
run_check("_min_max all-None → all 0.0", check_min_max_all_none)
run_check("score_trending worked example from §6 (A=5.0, B=0.0)", check_scorer_worked_example)
run_check("score_trending single candidate gets score 5.0", check_scorer_single_candidate)
run_check("score_trending missing volume within category → norm_volume=0.0", check_scorer_missing_volume_within_category)
run_check("score_trending drops off-category / excluded-accessory titles", check_scorer_drops_off_category_titles)
run_check("score_trending returns at most TOP_N_PER_CATEGORY per category", check_scorer_top_n_per_category)
run_check("score_trending groups output across categories", check_scorer_groups_multiple_categories)
run_check("score_trending assigns rank 1..N within category", check_scorer_rank_within_category)
run_check("score_trending tiebreaks by sold_quantity descending", check_scorer_tiebreak_by_sold_quantity)
run_check("_passes_predicate noise gate (sold=0, rate=0.0, no keyword)", check_passes_predicate_noise_gate)
run_check("_passes_predicate passes valid candidate", check_passes_predicate_good)
run_check("_passes_predicate keeps keyword-only candidate (search-surfaced)", check_passes_predicate_keyword_only_survives)
run_check("_passes_predicate recency gate drops activity > 60 days ago", check_passes_predicate_recency_too_old)
run_check("_passes_predicate data-presence gate (no signals at all)", check_passes_predicate_data_presence)

# ── section 14: trending_cache — Redis round-trip with fakeredis ──────────────

print("\n=== Section 14: trending_cache — Redis round-trip (fakeredis, no network) ===")

import fakeredis
import trending_cache
from trending_cache import load, save, acquire_lock, release_lock, SCHEMA_VER

def _fake_client() -> fakeredis.FakeRedis:
    return fakeredis.FakeRedis()

def _sample_items() -> list[TrendingItem]:
    return [TrendingItem(
        item_id="1", title="Widget", url="https://ebay.com/1", source="eBay",
        rank=1, score=4.5, keyword_rank=1, sold_quantity=100, sold_rate=0.5,
        norm_keyword=1.0, norm_volume=1.0, norm_sold=1.0, category="Denim",
    )]

def _sample_signals():
    kw     = [KeywordSignal("1", "widget", 1, _NOW, "Widget", "https://ebay.com/1", "Denim")]
    volume = [VolumeSignal("1", "Widget", 100, _NOW)]
    sold   = [SoldSignal("1", "Widget", 50, 100, 0.5, None, _NOW)]
    return kw, volume, sold

def check_cache_save_and_load():
    r = _fake_client()
    items = _sample_items()
    kw, volume, sold = _sample_signals()
    save(r, items, kw, volume, sold)
    loaded, ttl = load(r)
    assert loaded is not None, "load returned None after save"
    assert len(loaded) == 1
    assert loaded[0].item_id == "1"
    assert ttl > 0

def check_cache_miss_returns_none():
    r = _fake_client()
    items, ttl = load(r)
    assert items is None
    assert ttl == -2

def check_cache_round_trip_fields():
    r = _fake_client()
    original = _sample_items()[0]
    kw, volume, sold = _sample_signals()
    save(r, [original], kw, volume, sold)
    loaded, _ = load(r)
    it = loaded[0]
    assert it.title == original.title
    assert it.score == original.score
    assert it.norm_keyword == original.norm_keyword
    assert it.category == original.category == "Denim"  # v3 category survives round-trip

def check_cache_lock_single_flight():
    r = _fake_client()
    first  = acquire_lock(r)
    second = acquire_lock(r)
    assert first is True,  "first acquire should win the lock"
    assert second is False, "second acquire should fail while lock is held"

def check_cache_lock_release():
    r = _fake_client()
    acquire_lock(r)
    release_lock(r)
    again = acquire_lock(r)
    assert again is True, "should be able to re-acquire after release"

def check_cache_schema_version_in_key():
    r = _fake_client()
    kw, volume, sold = _sample_signals()
    save(r, _sample_items(), kw, volume, sold)
    keys = [k.decode() for k in r.keys("trending:*")]
    assert any(SCHEMA_VER in k for k in keys), f"schema version {SCHEMA_VER} not in keys: {keys}"

def check_cache_old_schema_key_not_read():
    """Saving with v1 then changing SCHEMA_VER to v2 means load() returns None."""
    r = _fake_client()
    kw, volume, sold = _sample_signals()
    save(r, _sample_items(), kw, volume, sold)
    original_ver = trending_cache.SCHEMA_VER
    try:
        trending_cache.SCHEMA_VER = "v999"
        items, ttl = load(r)
        assert items is None, "old-schema key should not be read under new SCHEMA_VER"
    finally:
        trending_cache.SCHEMA_VER = original_ver

run_check("save() then load() round-trips item list", check_cache_save_and_load)
run_check("load() on empty Redis returns (None, -2)", check_cache_miss_returns_none)
run_check("round-trip preserves title, score, norm_keyword, category", check_cache_round_trip_fields)
run_check("acquire_lock twice: first True, second False", check_cache_lock_single_flight)
run_check("acquire_lock after release_lock: can re-acquire", check_cache_lock_release)
run_check("save() writes versioned key containing SCHEMA_VER", check_cache_schema_version_in_key)
run_check("bumping SCHEMA_VER means old key is not read", check_cache_old_schema_key_not_read)

# ── section 15: trending_fetcher — offline mock transport ────────────────────

print("\n=== Section 15: trending_fetcher — offline mock transport (no network) ===")

import httpx
from trending_fetcher import EbayTrendingProvider

# Modern Browse API fixtures. The mock transport answers three endpoints:
#   - OAuth token  (identity/v1/oauth2/token)  → application access token
#   - item search  (item_summary/search)        → itemSummaries with itemWebUrl
#   - getItem      (/item/{id})                  → estimatedAvailabilities

_OAUTH_FIXTURE = {"access_token": "test-token", "token_type": "Application", "expires_in": 7200}

_SEARCH_FIXTURE = {
    "itemSummaries": [
        {"itemId": "v1|100|0", "title": "Vintage Camera",
         "itemWebUrl": "https://www.ebay.com/itm/100"},
        {"itemId": "v1|200|0", "title": "Retro Headphones",
         "itemWebUrl": "https://www.ebay.com/itm/200"},
    ]
}

# Per-item getItem responses keyed by the numeric id embedded in the path.
_ITEM_FIXTURES = {
    "100": {"itemId": "v1|100|0", "title": "Vintage Camera",
            "estimatedAvailabilities": [
                {"estimatedSoldQuantity": 450, "estimatedAvailableQuantity": 50}]},
    "200": {"itemId": "v1|200|0", "title": "Retro Headphones",
            "estimatedAvailabilities": [
                {"estimatedSoldQuantity": 200, "estimatedAvailableQuantity": 800}]},
}

def _mock_transport(search_resp=None, item_fixtures=None):
    search = search_resp   if search_resp   is not None else _SEARCH_FIXTURE
    items  = item_fixtures if item_fixtures is not None else _ITEM_FIXTURES
    def handler(request):
        url = str(request.url)
        if "identity/v1/oauth2/token" in url:
            return httpx.Response(200, json=_OAUTH_FIXTURE)
        if "item_summary/search" in url:
            return httpx.Response(200, json=search)
        # getItem: /buy/browse/v1/item/<url-encoded id>
        for num, payload in items.items():
            if num in url:
                return httpx.Response(200, json=payload)
        return httpx.Response(404, text="not found")
    return httpx.MockTransport(handler)

def _provider(client, **kw):
    return EbayTrendingProvider(
        client_id="id", client_secret="secret", client=client,
        seed_queries=["camera"], **kw,
    )

def check_fetcher_validate_key_empty():
    p = EbayTrendingProvider(client_id="", client_secret="")
    try:
        p._validate_key()
        raise AssertionError("expected EnvironmentError, got none")
    except EnvironmentError:
        pass

def check_fetcher_validate_key_missing_secret():
    p = EbayTrendingProvider(client_id="id", client_secret="")
    try:
        p._validate_key()
        raise AssertionError("expected EnvironmentError when secret missing")
    except EnvironmentError:
        pass

def check_fetcher_validate_key_present():
    EbayTrendingProvider(client_id="id", client_secret="secret")._validate_key()  # no raise

def check_fetcher_oauth_token_minted_and_cached():
    calls = [0]
    def handler(request):
        url = str(request.url)
        if "identity/v1/oauth2/token" in url:
            calls[0] += 1
            return httpx.Response(200, json=_OAUTH_FIXTURE)
        if "item_summary/search" in url:
            return httpx.Response(200, json=_SEARCH_FIXTURE)
        return httpx.Response(200, json=_ITEM_FIXTURES["100"])
    client = httpx.Client(transport=httpx.MockTransport(handler))
    p = _provider(client)
    assert p._get_token() == "test-token"
    p.fetch_keyword_signals(60)          # reuses cached token
    assert calls[0] == 1, f"token should be minted once, got {calls[0]}"

def check_fetcher_oauth_no_token_raises():
    def handler(request):
        return httpx.Response(200, json={"error": "invalid_client"})  # no access_token
    client = httpx.Client(transport=httpx.MockTransport(handler))
    p = _provider(client)
    try:
        p._get_token()
        raise AssertionError("expected RuntimeError when access_token missing")
    except RuntimeError:
        pass

def check_fetcher_keyword_signals_offline():
    client = httpx.Client(transport=_mock_transport())
    p = _provider(client)
    signals = p.fetch_keyword_signals(60)
    assert len(signals) == 2
    by_id = {s.item_id: s for s in signals}
    assert by_id["v1|100|0"].rank == 1
    assert by_id["v1|200|0"].rank == 2
    assert by_id["v1|100|0"].title == "Vintage Camera"
    assert by_id["v1|100|0"].url == "https://www.ebay.com/itm/100"

def check_fetcher_volume_signals_offline():
    client = httpx.Client(transport=_mock_transport())
    p = _provider(client)
    signals = p.fetch_volume_signals(["v1|100|0", "v1|200|0"], 60)
    assert len(signals) == 2
    sq = {s.item_id: s.sold_quantity for s in signals}
    assert sq["v1|100|0"] == 450
    assert sq["v1|200|0"] == 200

def check_fetcher_sold_signals_offline():
    client = httpx.Client(transport=_mock_transport())
    p = _provider(client)
    signals = p.fetch_sold_signals(["v1|200|0"], 60)
    assert len(signals) == 1
    s = signals[0]
    assert s.sold_count == 200
    assert s.total_count == 1000           # 200 sold + 800 available
    assert abs(s.sold_rate - 0.2) < 1e-9   # sell-through 200/1000
    assert s.last_sold is None

def check_fetcher_getitem_memoized():
    """volume + sold over the same id call getItem once (per-instance cache)."""
    item_calls = [0]
    def handler(request):
        url = str(request.url)
        if "identity/v1/oauth2/token" in url:
            return httpx.Response(200, json=_OAUTH_FIXTURE)
        if "item_summary/search" in url:
            return httpx.Response(200, json=_SEARCH_FIXTURE)
        item_calls[0] += 1
        return httpx.Response(200, json=_ITEM_FIXTURES["100"])
    client = httpx.Client(transport=httpx.MockTransport(handler))
    p = _provider(client)
    p.fetch_volume_signals(["v1|100|0"], 60)
    p.fetch_sold_signals(["v1|100|0"], 60)
    assert item_calls[0] == 1, f"getItem should be cached, called {item_calls[0]}x"

def check_fetcher_search_non_200_raises():
    def handler(request):
        if "identity/v1/oauth2/token" in str(request.url):
            return httpx.Response(200, json=_OAUTH_FIXTURE)
        return httpx.Response(503, text="service unavailable")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    p = _provider(client)
    try:
        p.fetch_keyword_signals(60)
        raise AssertionError("expected RuntimeError, got none")
    except RuntimeError:
        pass

def check_fetcher_getitem_404_skipped():
    """A candidate whose getItem 404s is silently skipped, not fatal."""
    client = httpx.Client(transport=_mock_transport())
    p = _provider(client)
    signals = p.fetch_volume_signals(["v1|999|0"], 60)  # 999 not in fixtures → 404
    assert signals == []

def check_fetcher_keyword_dedup_keeps_best_rank():
    """An item under two seeds keeps its lowest (best) rank."""
    # seed 'a' → item at pos 2; seed 'b' → same item at pos 1
    search_a = {"itemSummaries": [
        {"itemId": "v1|X|0", "title": "X", "itemWebUrl": "u"},
        {"itemId": "v1|Y|0", "title": "Y", "itemWebUrl": "u"}]}
    search_b = {"itemSummaries": [
        {"itemId": "v1|Y|0", "title": "Y", "itemWebUrl": "u"}]}
    seq = {"a": search_a, "b": search_b}
    def handler(request):
        url = str(request.url)
        if "identity/v1/oauth2/token" in url:
            return httpx.Response(200, json=_OAUTH_FIXTURE)
        # pick fixture by the q param
        q = httpx.URL(url).params.get("q")
        return httpx.Response(200, json=seq[q])
    client = httpx.Client(transport=httpx.MockTransport(handler))
    p = EbayTrendingProvider(client_id="id", client_secret="secret",
                             client=client, seed_queries=["a", "b"])
    signals = p.fetch_keyword_signals(60)
    by_id = {s.item_id: s.rank for s in signals}
    assert by_id["v1|Y|0"] == 1, f"Y should keep best rank 1, got {by_id['v1|Y|0']}"

def check_fetcher_tags_category_from_seed():
    """v3: each KeywordSignal inherits its seed's category (CATEGORY_SEED_MAP)."""
    client = httpx.Client(transport=_mock_transport())
    p = EbayTrendingProvider(client_id="id", client_secret="secret",
                             client=client, seed_queries=["flare jeans vintage"])
    signals = p.fetch_keyword_signals(60)
    assert signals and all(s.category == "Denim" for s in signals), \
        f"expected all Denim, got {[s.category for s in signals]}"

def check_fetcher_category_follows_best_rank():
    """v3: on cross-seed dedup, category follows the best-ranked (winning) seed."""
    # Y appears under a Tops seed at pos 1 and a Denim seed at pos 2 → category Tops.
    denim = {"itemSummaries": [
        {"itemId": "v1|X|0", "title": "X", "itemWebUrl": "u"},
        {"itemId": "v1|Y|0", "title": "Y", "itemWebUrl": "u"}]}      # Y at pos 2
    tops  = {"itemSummaries": [
        {"itemId": "v1|Y|0", "title": "Y", "itemWebUrl": "u"}]}      # Y at pos 1
    by_seed = {"vintage straight leg jeans": denim, "vintage band tee": tops}
    def handler(request):
        url = str(request.url)
        if "identity/v1/oauth2/token" in url:
            return httpx.Response(200, json=_OAUTH_FIXTURE)
        q = httpx.URL(url).params.get("q")
        return httpx.Response(200, json=by_seed[q])
    client = httpx.Client(transport=httpx.MockTransport(handler))
    p = EbayTrendingProvider(client_id="id", client_secret="secret", client=client,
                             seed_queries=["vintage straight leg jeans", "vintage band tee"])
    signals = p.fetch_keyword_signals(60)
    by_id = {s.item_id: s for s in signals}
    assert by_id["v1|Y|0"].category == "Tops", \
        f"Y should take winning seed's category, got {by_id['v1|Y|0'].category}"

run_check("_validate_key raises when client_id+secret empty", check_fetcher_validate_key_empty)
run_check("_validate_key raises when only secret missing", check_fetcher_validate_key_missing_secret)
run_check("_validate_key does not raise when both set", check_fetcher_validate_key_present)
run_check("OAuth token minted once and cached across calls", check_fetcher_oauth_token_minted_and_cached)
run_check("OAuth response without access_token raises RuntimeError", check_fetcher_oauth_no_token_raises)
run_check("fetch_keyword_signals maps search → KeywordSignal (title/url/rank)", check_fetcher_keyword_signals_offline)
run_check("fetch_volume_signals maps getItem → VolumeSignal (sold_quantity)", check_fetcher_volume_signals_offline)
run_check("fetch_sold_signals computes sell-through from getItem", check_fetcher_sold_signals_offline)
run_check("getItem is memoized across volume+sold for the same id", check_fetcher_getitem_memoized)
run_check("search non-200 raises RuntimeError", check_fetcher_search_non_200_raises)
run_check("getItem 404 candidate is skipped, not fatal", check_fetcher_getitem_404_skipped)
run_check("keyword dedup keeps best (lowest) rank across seeds", check_fetcher_keyword_dedup_keeps_best_rank)
run_check("fetch_keyword_signals tags category from seed (CATEGORY_SEED_MAP)", check_fetcher_tags_category_from_seed)
run_check("category follows best-ranked seed on cross-seed dedup", check_fetcher_category_follows_best_rank)

# ── section 16: orchestration — get_trending() end-to-end ────────────────────

print("\n=== Section 16: orchestration — get_trending() end-to-end (fakeredis + stub provider) ===")

from trending_scorer import get_trending

class _StubProvider:
    """Minimal in-process provider for orchestration tests — no network."""
    def fetch_keyword_signals(self, lookback_days):
        return [_kw("A", 1), _kw("B", 2)]

    def fetch_volume_signals(self, item_ids, lookback_days):
        return [_v("A", 500), _v("B", 100)]

    def fetch_sold_signals(self, item_ids, lookback_days):
        return [_s("A", 40, 100), _s("B", 10, 100)]

def check_get_trending_returns_items():
    r = _fake_client()
    items = get_trending(_StubProvider(), r)
    assert len(items) > 0

def check_get_trending_top_ranked():
    r = _fake_client()
    items = get_trending(_StubProvider(), r)
    assert items[0].rank == 1

def check_get_trending_cache_hit_skips_provider():
    """Second call serves from cache — provider calls would be 0 on second call."""
    call_count = [0]
    class _CountingProvider(_StubProvider):
        def fetch_keyword_signals(self, lookback_days):
            call_count[0] += 1
            return super().fetch_keyword_signals(lookback_days)

    r = _fake_client()
    get_trending(_CountingProvider(), r)  # populates cache
    before = call_count[0]
    get_trending(_CountingProvider(), r)  # should hit cache
    assert call_count[0] == before, "cache hit should not call provider again"

def check_get_trending_returns_list_of_trending_items():
    r = _fake_client()
    items = get_trending(_StubProvider(), r)
    for it in items:
        assert isinstance(it, TrendingItem), f"expected TrendingItem, got {type(it)}"

def check_get_trending_warm_refresh_below_floor():
    """TTL below REFRESH_FLOOR triggers a warm refresh that re-invokes the provider."""
    call_count = [0]
    class _CountingProvider(_StubProvider):
        def fetch_keyword_signals(self, lookback_days):
            call_count[0] += 1
            return super().fetch_keyword_signals(lookback_days)

    r = _fake_client()
    p = _CountingProvider()
    get_trending(p, r)                       # cold → populate cache (count=1)
    before = call_count[0]
    # Force the ranked key's TTL just under the warm-refresh floor.
    r.expire(trending_cache._key("ranked"), trending_cache.REFRESH_FLOOR_SECONDS - 10)
    items = get_trending(p, r)               # hit + low TTL → inline warm refresh
    assert call_count[0] > before, "warm refresh should re-invoke the provider"
    assert items and items[0].rank == 1, "should still serve a ranked list"

run_check("get_trending() returns non-empty list", check_get_trending_returns_items)
run_check("get_trending() first item has rank=1", check_get_trending_top_ranked)
run_check("get_trending() second call hits cache (provider not called again)", check_get_trending_cache_hit_skips_provider)
run_check("get_trending() returns list[TrendingItem]", check_get_trending_returns_list_of_trending_items)
run_check("get_trending() warm-refreshes when TTL < REFRESH_FLOOR", check_get_trending_warm_refresh_below_floor)

# ── summary ───────────────────────────────────────────────────────────────────

print(f"\n{'='*40}")
print(f"Results: {_passed} passed, {_failed} failed")
if _failed:
    sys.exit(1)
