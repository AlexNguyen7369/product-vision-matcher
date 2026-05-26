import sys
import tempfile
import base64

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

# ── section 5: stub status ────────────────────────────────────────────────────

print("\n=== Section 5: Stub module status ===")
for module in ("pipeline",):
    print(f"  [STUB] {module} - not yet implemented")

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

def check_rs_search_returns_json_offline():
    def handler(request):
        return httpx.Response(200, json={"visual_matches": []})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    searcher = SerpApiSearcher(api_key="dummy", client=client)
    result = searcher.search(_tiny_image())
    assert result == {"visual_matches": []}, f"unexpected payload: {result}"

def check_rs_non_200_raises_offline():
    def handler(request):
        return httpx.Response(500, text="upstream error")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    searcher = SerpApiSearcher(api_key="dummy", client=client)
    try:
        searcher.search(_tiny_image())
        raise AssertionError("expected RuntimeError on non-200, got none")
    except RuntimeError:
        pass

run_check("reverse_search exposes SerpApiSearcher", check_rs_import)
run_check("_validate_key raises EnvironmentError when key is empty", check_rs_missing_key_raises)
run_check("_validate_key passes when key is present", check_rs_key_present_does_not_raise)
run_check("search() returns parsed JSON via MockTransport", check_rs_search_returns_json_offline)
run_check("search() raises RuntimeError on non-200 via MockTransport", check_rs_non_200_raises_offline)

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

from marketplace_parser import parse

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

# ── section 8: agent_review — tool unit tests ─────────────────────────────────
#
# Tests the tool-implementation layer of agent_review without calling the
# Claude API or hitting the network. Each function under test is imported
# directly so the agent loop itself is never invoked.

print("\n=== Section 8: agent_review — tool implementations ===")

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

# ── summary ───────────────────────────────────────────────────────────────────

print(f"\n{'='*40}")
print(f"Results: {_passed} passed, {_failed} failed")
if _failed:
    sys.exit(1)
