# ai_agent_security_compat.md — Autonomous Review Agent: Technical Breakdown

## Role in the Project

`src/agent_review.py` is a standalone CLI tool that runs outside the main
pipeline. It drives a Claude-powered agent loop to review the codebase itself —
checking security, verifying module contracts, flagging scalability bottlenecks,
running the test suite, and optionally auditing a live HTTP endpoint in a
headless browser.

```
developer invokes agent_review.py
    │
    ▼
Claude agent loop (claude-sonnet-4-6)
    ├── Pass 1: SECURITY      → bandit + pip-audit
    ├── Pass 2: COMPATIBILITY → read + diff module contracts
    ├── Pass 3: SCALABILITY   → AST scan each .py
    ├── Pass 4: TESTS         → run test_setup.py
    └── Pass 5: BROWSER       → Playwright headless Chromium (optional)
    │
    ▼
structured JSON report  →  stdout or --output file
```

---

## Why an Agent Loop Instead of a Script

A fixed script runs a predetermined sequence. An agent can:

- **Decide what to read** based on findings from earlier tool calls
  (e.g., bandit flags a file → agent reads that file for context)
- **Iterate** — if a tool call returns an error, Claude reasons about why
  and retries with a corrected input
- **Skip passes** when they are not applicable (browser pass skipped unless a
  URL is provided)
- **Synthesise** across passes — e.g., a scalability finding in `reverse_search.py`
  combined with a compatibility issue in `marketplace_parser.py` produces a
  unified root-cause finding

---

## Tool Inventory

| Tool | Implementation | Purpose |
|---|---|---|
| `read_source_file` | `Path.read_text` | Inspect module code for contract mismatches |
| `run_bandit` | subprocess → `bandit -r` | Static security analysis (OWASP, injection, secrets) |
| `run_pip_audit` | subprocess → `pip-audit` | CVE database check on installed packages |
| `run_tests` | subprocess → `python test_setup.py` | Confirms test suite is green end-to-end |
| `scan_scalability` | `ast.NodeVisitor` | Flags sync HTTP clients and unbounded loops |
| `browser_check_url` | Playwright `sync_playwright` | Audits live HTTP security headers in Chromium |

All tool implementations are in `_TOOL_MAP` — a dict of lambdas dispatched by
name. Claude calls tools by name; `dispatch(name, input)` routes to the right
lambda and wraps exceptions as error strings so the agent loop never crashes on
a bad tool result.

---

## Pass 1 — Security

### bandit

`run_bandit("src/")` shells out:

```
bandit -r src/ -f text --quiet
```

bandit walks the AST and flags issues by:
- **Test ID** (`B105`, `B106`, `B107` — hardcoded passwords)
- **Severity**: HIGH / MEDIUM / LOW
- **Confidence**: HIGH / MEDIUM / LOW

**Relevant checks for this project:**

| bandit test | What it catches |
|---|---|
| B105 | Hardcoded password / API key string literals |
| B110 | `try/except/pass` — swallowed exceptions |
| B311 | `random` used for security purposes |
| B501–B509 | SSL/TLS misconfigurations |
| B601 | Shell injection via `subprocess` |

Because `SERPAPI_KEY` is loaded from `.env` via `os.getenv`, bandit B105 will
not fire. If the key were ever hardcoded, bandit would catch it immediately.

### pip-audit

`run_pip_audit()` shells out:

```
pip-audit --format columns
```

pip-audit queries the PyPI Advisory Database (OSV) for each installed package
version. It returns a table of vulnerable packages, affected versions, and CVE
IDs. For this project the main risk is the deep dependency tree: torch,
transformers, easyocr, and PIL each have had historical CVEs.

**Graceful degradation:** if `bandit` or `pip-audit` are not installed, the
tool returns a human-readable `ERROR: not installed` string. Claude sees this
in the tool result and notes it in the report rather than crashing.

---

## Pass 2 — Compatibility

Claude reads each source file with `read_source_file` and checks:

### Contract: ProcessedImage

```
image_processor.process_image() → ProcessedImage(encoded, format, size)
                                          ↓
reverse_search.search(image: ProcessedImage) → dict
  uses: image.encoded (base64 str), image.format (str)
  ignores: image.size
```

Claude verifies:
- `reverse_search._post()` calls `base64.b64decode(image.encoded)` — matches
  `image_processor._encoded()` which produces a UTF-8 base64 string
- `mime = f"image/{image.format.lower()}"` — format is `"JPEG"|"PNG"|"WEBP"`,
  maps correctly to `"image/jpeg"` etc.

### Contract: SerpAPI dict → marketplace_parser

```
reverse_search.search() → raw SerpAPI dict (untouched)
                                  ↓
marketplace_parser.parse(serpapi_response: dict)
  reads: serpapi_response["visual_matches"]
  each match: ["title"], ["link"], ["source"], ["price"]["extracted_value"]
```

Claude verifies that `reverse_search` returns `response.json()` unmodified —
no field stripping, no reshaping — so `marketplace_parser` always has the full
payload available.

### What breaks compatibility

- Renaming `ProcessedImage.encoded` to `ProcessedImage.b64` in image_processor
  without updating reverse_search
- `reverse_search` stripping `visual_matches` before returning
- Adding a required positional argument to `marketplace_parser.parse()`

---

## Pass 3 — Scalability (AST Analysis)

`scan_scalability` uses Python's `ast` module to walk the parse tree of each
`.py` file without executing it. Two checks are implemented:

### Check 1: Synchronous `httpx.Client`

```python
class _Visitor(ast.NodeVisitor):
    def visit_Call(self, node):
        if node.func.attr == "Client" and node.func.value.id == "httpx":
            findings.append(f"line {node.lineno}: sync httpx.Client")
```

**Why this matters:** `reverse_search._post()` uses `httpx.Client` (sync).
This blocks the OS thread for the full SerpAPI round-trip (~2–10 s). For a
single-image CLI tool this is fine; for a web server handling concurrent
requests, this would starve other requests. The finding is informational now
and actionable when `pipeline.py` is wired into a web framework.

**Fix path:**
```python
# current (sync)
with httpx.Client(timeout=_TIMEOUT) as client:
    return client.post(...)

# future (async)
async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
    return await client.post(...)
```

### Check 2: Unbounded `while True`

```python
def visit_While(self, node):
    if node.test is True and no Break in subtree:
        findings.append(f"line {node.lineno}: unbounded while True")
```

An infinite loop with no break is a liveness hazard — any bug that prevents
the exit condition from being reached hangs the process permanently. This check
does not fire on `while True: ... break` patterns.

---

## Pass 4 — Tests

`run_tests()` executes `src/test_setup.py` as a subprocess, capturing both
stdout and stderr. Claude reads the summary line:

```
Results: N passed, 0 failed
```

If `failed > 0`, Claude includes the failed check labels in the compatibility
and security findings of the report, since a test failure is direct evidence of
a broken contract or regression.

---

## Pass 5 — Browser Verification (MCP-style via Playwright)

This pass runs only when `--browser <URL>` is passed.

### Why a browser, not httpx?

httpx can fetch HTTP headers from a URL directly. But:
- A browser executes JavaScript, which can set headers dynamically
- Playwright captures the *effective* headers after any redirects
- The agent can observe the rendered page title and status code together

### Security headers checked

| Header | What it prevents |
|---|---|
| `content-security-policy` | XSS, data injection |
| `x-content-type-options: nosniff` | MIME-type sniffing attacks |
| `x-frame-options` | Clickjacking |
| `strict-transport-security` | Protocol downgrade, MITM |
| `referrer-policy` | Referrer leakage to third parties |

### How Playwright integrates

```python
with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    page    = browser.new_page()
    response = page.goto(url, timeout=15_000)
    headers  = response.headers          # dict of response headers
    status   = response.status           # HTTP status code
    title    = page.title()              # rendered <title>
    browser.close()
```

`sync_playwright` is used (not async) because `agent_review.py` runs
synchronously. If the project later exposes a web UI (e.g., a FastAPI server
wrapping the pipeline), `--browser http://localhost:8000` would audit it as
part of the review cycle.

### Graceful degradation

If Playwright is not installed, `_tool_browser_check_url` returns:
```
ERROR: playwright not installed — run: pip install playwright && playwright install chromium
```
Claude records this in the report and marks the browser pass as skipped.

---

## Agent Loop Design

```
messages = [{"role": "user", "content": "Review src/..."}]

while True:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        tools=TOOLS,
        messages=messages,
    )

    if response.stop_reason == "tool_use":
        results = [dispatch(block.name, block.input) for block in response.content
                   if block.type == "tool_use"]
        messages += [assistant turn, tool_result turn]
        continue

    # stop_reason == "end_turn" → extract JSON from final text block
    return parse_json(response)
```

**Why a while loop instead of recursion:** the number of tool call rounds is
not known in advance. Claude may call `read_source_file` multiple times if it
finds a contract mismatch it wants to trace through the call graph. The while
loop accumulates the full conversation in `messages` so each round has complete
context of prior tool results.

**Token budget:** `max_tokens=8096` is set on each API call. The agent's system
prompt is ~400 tokens; each tool result adds ~200–2000 tokens depending on
bandit output size. For a typical review the total is under 20k tokens across
3–5 rounds, well within the 200k context window.

---

## Output Contract

The agent always outputs a JSON object. Claude is instructed in the system
prompt to produce *only* JSON as its final message. `run_agent()` extracts it
with `text.find("{")` / `text.rfind("}")` as a fallback if Claude wraps it in
markdown fences.

```json
{
  "security":      [{"severity": "HIGH", "file": "src/x.py", "line": 12, "finding": "..."}],
  "compatibility": [{"modules": ["a.py", "b.py"], "issue": "..."}],
  "scalability":   [{"file": "src/y.py", "line": 37, "finding": "..."}],
  "test_suite":    {"passed": 28, "failed": 0, "status": "pass"},
  "browser":       [{"url": "http://...", "finding": "CSP header missing"}],
  "summary":       "All 28 tests pass. One LOW-severity bandit finding ..."
}
```

Empty lists mean no findings in that category — a clean report for a category
is `[]`, not absent.

---

## CLI Usage

```powershell
# activate venv first
.venv\Scripts\Activate.ps1

# basic review (static analysis only)
python src/agent_review.py

# review a specific file
python src/agent_review.py --target src/reverse_search.py

# include browser verification of a live endpoint
python src/agent_review.py --browser http://localhost:8000

# write report to file
python src/agent_review.py --output report.json
```

---

## Dependencies

| Package | Purpose | Install |
|---|---|---|
| `anthropic` | Claude API client | `pip install anthropic` |
| `bandit` | Security static analysis | `pip install bandit` |
| `pip-audit` | CVE dependency scan | `pip install pip-audit` |
| `playwright` | Headless browser (optional) | `pip install playwright && playwright install chromium` |

`anthropic` and `bandit` are required for the agent to run. `pip-audit` and
`playwright` are optional — the agent degrades gracefully if they are absent.

---

## Extension Points

| When | What to add |
|---|---|
| `pipeline.py` is implemented | Add `read_source_file("src/pipeline.py")` to compatibility pass |
| Web UI is added (FastAPI) | Wire `--browser http://localhost:8000` into CI |
| `price_aggregator.py` is implemented | Add `scan_scalability("src/price_aggregator.py")` |
| Semgrep is preferred over bandit | Swap `run_bandit` for `run_semgrep` — same subprocess pattern |
| Async pipeline | Update scalability check to flag missing `await` on `AsyncClient` calls |
