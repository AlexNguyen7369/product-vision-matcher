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
    → marketplace_parser.py # extracts product listings from SerpAPI results (stub)
    → price_aggregator.py  # aggregates/ranks prices across marketplaces (stub)
    → pipeline.py          # orchestrates the above (stub)
```

**Key design decisions:**
- `ProcessedImage` dataclass is the contract between `image_processor` and `reverse_search` — all downstream code receives this, never raw PIL or bytes.
- `data/embeddings/` is reserved for FAISS vector index files; `data/uploads/` holds input images.
- `faiss-cpu`, `transformers`, and `torch` are installed for local embedding/similarity search (not yet wired into the pipeline — `reverse_search.py` via SerpAPI is the current approach).
- `easyocr` is available for extracting text from product label images.

**Current state:** `image_processor.py` and the SerpAPI key loading in `reverse_search.py` are the only implemented pieces. `marketplace_parser.py`, `price_aggregator.py`, and `pipeline.py` are stubs.

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
