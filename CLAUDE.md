# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup

The codebase is OS-agnostic (uses `pathlib`, `os.path.join`, `tempfile`, and
list-form `subprocess` — no shell strings or hardcoded separators), so it runs
unchanged on Windows and macOS/Linux. Only the virtual-environment activation
command differs:

```powershell
# Windows (PowerShell)
python -m venv .venv
.venv\Scripts\Activate.ps1
```

```bash
# macOS / Linux
python3 -m venv .venv
source .venv/bin/activate
```

Then, on either OS:

```
# Install dependencies (pip resolves the right per-OS wheels for torch/faiss/opencv)
pip install -r requirements.txt

# Verify environment
python src/test_setup.py
```

`.venv/` is git-ignored and platform-specific — never commit it. When moving
between a Windows and a Mac machine, recreate it locally with the matching
command above rather than copying the directory across.

Requires a `.env` file in the project root with:
```
SERPAPI_KEY=<your_key>

# Trending feature (eBay Browse API, OAuth client-credentials):
EBAY_CLIENT_ID=<your_ebay_client_id>
EBAY_CLIENT_SECRET=<your_ebay_client_secret>
REDIS_URL=redis://localhost:6379/0
```
`EBAY_RUNAME` is **not** required — the Browse API is reached with an application
token (client-credentials grant), which needs only the Client ID + Secret.

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

- `agent_review.py` lives at the **project root** (not inside `src/`). Run it with `python agent_review.py` from the project root. It reviews the entire codebase — security, compatibility, scalability, and tests.

**Current state:** `image_processor.py`, `reverse_search.py`, `marketplace_parser.py`, and `price_aggregator.py` (`rank_by_price`) are implemented. `pipeline.py` is the only remaining stub.

## Git Commit Policy

After every feature addition or meaningful change, you **must** create a git commit with a single-sentence message that describes the new feature or change in plain language.

**Format:** `<verb> <what was added/changed>` — e.g., `add marketplace parser to extract listings from SerpAPI results`

- One sentence, lowercase, no period.
- Focus on *what* was added or changed, not implementation details.
- Commit only after tests pass.

## Change Log Policy

Every time a feature is implemented or a meaningful change lands in the codebase,
you **must** append an entry to `logging.md` at the project root, in the same commit
as the change. This file is the human-readable, incremental history of what shipped.

- Add a new `## N — <file(s)>: <short title>` section (newest last); never rewrite
  prior entries.
- Each entry records *what changed and why*, any non-obvious design decisions or
  trade-offs, and the resulting test count (`N passed, 0 failed`).
- Keep it in lockstep with commits: one logical change → one `logging.md` entry →
  one commit. Update `logging.md` **before** committing so it ships with the change.

## Testing Policy

Every time a new feature or module is added or modified, you **must**:

1. Add corresponding checks to `src/test_setup.py` covering the new public surface (happy path, edge cases, and error conditions).
2. Run `python src/test_setup.py` and confirm all checks pass before considering the work done.

**Conventions for `src/test_setup.py`:**
- Group checks under a clearly labelled `=== Section N: <module> — <topic> ===` header.
- Each check is a plain function passed to `run_check(label, fn)` — no test framework needed.
- Checks must be self-contained: use in-process fixture data or temp files; never hit the network.
- After adding checks, run the script and paste the summary line (`N passed, 0 failed`) in your response to confirm the suite is green.
