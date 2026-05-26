# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup

```powershell
# Activate virtual environment (Windows)
.venv\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.txt

# Verify environment
python src/test_setup.py
```

Requires a `.env` file in the project root with:
```
SERPAPI_KEY=<your_key>
```

## Architecture

The goal is a pipeline that takes a product image, reverse-searches it via Google (SerpAPI), parses marketplace listings, and aggregates prices.

**Intended data flow:**

```
[image file]
    → image_processor.py   # load, validate (JPEG/PNG/WEBP only), resize to ≤1024×1024, base64-encode → ProcessedImage
    → reverse_search.py    # sends encoded image to SerpAPI Google Lens endpoint → raw search results
    → marketplace_parser.py # extracts/filters product listings from SerpAPI results
    → price_aggregator.py  # ranks listings by price (rank_by_price)
    → pipeline.py          # orchestrates the above (stub)
```

**Key design decisions:**
- Shared dataclasses live in `models.py`: `ProcessedImage` (the `image_processor` → `reverse_search` contract) and `ParsedListing` (the `marketplace_parser` → `price_aggregator` contract). Modules import types from `models`, never from each other, so no module depends on a sibling's implementation just to name a type.
- `reverse_search.py` exposes `SerpApiSearcher`, which implements the `ReverseSearchProvider` protocol (`models.py`). `pipeline` depends on that protocol, not the concrete class, so a local embedding/FAISS backend can be swapped in later. API key and HTTP client are injected via the constructor (no import-time globals).
- `data/embeddings/` is reserved for FAISS vector index files; `data/uploads/` holds input images.
- `faiss-cpu`, `transformers`, and `torch` are installed for local embedding/similarity search (not yet wired into the pipeline — `reverse_search.py` via SerpAPI is the current approach).
- `easyocr` is available for extracting text from product label images.

**Current state:** `image_processor.py`, `reverse_search.py`, `marketplace_parser.py`, and `price_aggregator.py` (`rank_by_price`) are implemented. `pipeline.py` is the only remaining stub.

## Git Commit Policy

After every feature addition or meaningful change, you **must** create a git commit with a single-sentence message that describes the new feature or change in plain language.

**Format:** `<verb> <what was added/changed>` — e.g., `add marketplace parser to extract listings from SerpAPI results`

- One sentence, lowercase, no period.
- Focus on *what* was added or changed, not implementation details.
- Commit only after tests pass.

## Testing Policy

Every time a new feature or module is added or modified, you **must**:

1. Add corresponding checks to `src/test_setup.py` covering the new public surface (happy path, edge cases, and error conditions).
2. Run `python src/test_setup.py` and confirm all checks pass before considering the work done.

**Conventions for `src/test_setup.py`:**
- Group checks under a clearly labelled `=== Section N: <module> — <topic> ===` header.
- Each check is a plain function passed to `run_check(label, fn)` — no test framework needed.
- Checks must be self-contained: use in-process fixture data or temp files; never hit the network.
- After adding checks, run the script and paste the summary line (`N passed, 0 failed`) in your response to confirm the suite is green.
