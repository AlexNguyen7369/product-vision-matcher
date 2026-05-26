"""
Autonomous code review agent using Claude API.

Usage:
    python src/agent_review.py [--target src/] [--browser URL]

Runs four passes — security, compatibility, scalability, tests — then
optionally verifies a running web endpoint in a headless browser via
Playwright. Outputs a structured JSON report to stdout (or --output file).
"""
from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).parent.parent

# ── tool schema definitions sent to Claude ─────────────────────────────────────

TOOLS: list[dict] = [
    {
        "name": "read_source_file",
        "description": (
            "Read a Python source file from the project. "
            "Use to inspect code for compatibility and contract issues."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path relative to project root, e.g. 'src/image_processor.py'",
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "run_bandit",
        "description": (
            "Run the bandit security linter on a Python file or directory. "
            "Returns findings with severity and confidence levels."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Path relative to project root, e.g. 'src/' or 'src/reverse_search.py'",
                }
            },
            "required": ["target"],
        },
    },
    {
        "name": "run_pip_audit",
        "description": "Audit installed packages for known CVEs. Returns vulnerable package list.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "run_tests",
        "description": "Execute src/test_setup.py and return full output including pass/fail counts.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "scan_scalability",
        "description": (
            "AST-analyse a Python file for scalability concerns: "
            "blocking synchronous HTTP calls, unbounded infinite loops."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path relative to project root, e.g. 'src/reverse_search.py'",
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "browser_check_url",
        "description": (
            "Navigate a headless Chromium browser to a URL, check HTTP security headers "
            "(CSP, HSTS, X-Content-Type-Options, X-Frame-Options, Referrer-Policy), "
            "and return a header audit report. Use only for live HTTP/HTTPS endpoints."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "URL to audit — must start with http:// or https://",
                }
            },
            "required": ["url"],
        },
    },
]

# ── tool implementations ───────────────────────────────────────────────────────

def _tool_read_source_file(path: str) -> str:
    full = PROJECT_ROOT / path
    if not full.exists():
        return f"ERROR: '{path}' not found under project root"
    return full.read_text(encoding="utf-8")


def _tool_run_bandit(target: str) -> str:
    full = str(PROJECT_ROOT / target)
    try:
        result = subprocess.run(
            ["bandit", "-r", full, "-f", "text", "--quiet"],
            capture_output=True, text=True, timeout=60,
        )
        output = (result.stdout + result.stderr).strip()
        return output or "bandit: no issues found"
    except FileNotFoundError:
        return "ERROR: bandit not installed — run: pip install bandit"
    except subprocess.TimeoutExpired:
        return "ERROR: bandit timed out after 60 s"


def _tool_run_pip_audit() -> str:
    try:
        result = subprocess.run(
            ["pip-audit", "--format", "columns"],
            capture_output=True, text=True, timeout=120,
        )
        output = (result.stdout + result.stderr).strip()
        return output or "pip-audit: no known vulnerabilities found"
    except FileNotFoundError:
        return "ERROR: pip-audit not installed — run: pip install pip-audit"
    except subprocess.TimeoutExpired:
        return "ERROR: pip-audit timed out after 120 s"


def _tool_run_tests() -> str:
    src_dir = str(PROJECT_ROOT / "src")
    try:
        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "src" / "test_setup.py")],
            capture_output=True, text=True, timeout=120, cwd=src_dir,
        )
        return (result.stdout + result.stderr).strip()
    except subprocess.TimeoutExpired:
        return "ERROR: test suite timed out after 120 s"


def _tool_scan_scalability(path: str) -> str:
    full = PROJECT_ROOT / path
    if not full.exists():
        return f"ERROR: '{path}' not found"

    source = full.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as e:
        return f"Syntax error — cannot analyse: {e}"

    findings: list[str] = []

    class _Visitor(ast.NodeVisitor):
        def visit_While(self, node: ast.While) -> None:
            if isinstance(node.test, ast.Constant) and node.test.value is True:
                has_break = any(isinstance(n, ast.Break) for n in ast.walk(node))
                if not has_break:
                    findings.append(
                        f"line {node.lineno}: unbounded `while True` with no break"
                    )
            self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            # Flag sync httpx.Client — blocks thread under async load
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "Client"
                and isinstance(func.value, ast.Name)
                and func.value.id == "httpx"
            ):
                findings.append(
                    f"line {node.lineno}: sync `httpx.Client` — use `httpx.AsyncClient` "
                    f"for concurrent pipeline throughput"
                )
            self.generic_visit(node)

    _Visitor().visit(tree)

    if not findings:
        return f"No scalability concerns detected in '{path}'."
    return "\n".join(f"  ! {f}" for f in findings)


def _tool_browser_check_url(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        return "ERROR: URL must start with http:// or https://"
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return (
            "ERROR: playwright not installed — run: "
            "pip install playwright && playwright install chromium"
        )

    _SECURITY_HEADERS = [
        "content-security-policy",
        "x-content-type-options",
        "x-frame-options",
        "strict-transport-security",
        "referrer-policy",
    ]

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            response = page.goto(url, timeout=15_000)
            headers = response.headers if response else {}
            status  = response.status if response else "N/A"
            title   = page.title()
            browser.close()

        lines = [f"URL: {url}", f"HTTP status: {status}", f"Page title: {title}", "", "Security headers:"]
        for h in _SECURITY_HEADERS:
            value = headers.get(h, "MISSING")
            flag  = "PASS" if value != "MISSING" else "FAIL"
            lines.append(f"  [{flag}] {h}: {value}")
        return "\n".join(lines)
    except Exception as exc:
        return f"Browser check failed: {exc}"


# ── tool dispatch ──────────────────────────────────────────────────────────────

_TOOL_MAP: dict = {
    "read_source_file":  lambda inp: _tool_read_source_file(inp["path"]),
    "run_bandit":        lambda inp: _tool_run_bandit(inp["target"]),
    "run_pip_audit":     lambda _:   _tool_run_pip_audit(),
    "run_tests":         lambda _:   _tool_run_tests(),
    "scan_scalability":  lambda inp: _tool_scan_scalability(inp["path"]),
    "browser_check_url": lambda inp: _tool_browser_check_url(inp["url"]),
}


def dispatch(name: str, tool_input: dict) -> str:
    handler = _TOOL_MAP.get(name)
    if not handler:
        return f"ERROR: unknown tool '{name}'"
    try:
        return handler(tool_input)
    except Exception as exc:
        return f"ERROR in {name}: {exc}"


# ── agent loop ─────────────────────────────────────────────────────────────────

_SYSTEM = """\
You are an autonomous code review agent for the product-vision-matcher project —
a Python pipeline: image_processor → reverse_search → marketplace_parser.

Run these passes in order, calling tools as needed:

1. SECURITY     — run_bandit on 'src/', then run_pip_audit
2. COMPATIBILITY — read_source_file each module and verify data contracts:
                   ProcessedImage must flow unchanged from image_processor
                   into reverse_search, and the raw dict from reverse_search
                   must match what marketplace_parser.parse() expects
3. SCALABILITY  — scan_scalability on every .py in src/
4. TESTS        — run_tests; confirm the suite is green
5. BROWSER      — browser_check_url only if the user message includes a URL;
                   skip otherwise

After all passes, output ONLY a JSON object (no prose before or after):
{
  "security":      [{"severity": "HIGH|MEDIUM|LOW|INFO", "file": "...", "line": 0, "finding": "..."}],
  "compatibility": [{"modules": ["a.py", "b.py"], "issue": "..."}],
  "scalability":   [{"file": "...", "line": 0, "finding": "..."}],
  "test_suite":    {"passed": 0, "failed": 0, "status": "pass|fail"},
  "browser":       [{"url": "...", "finding": "..."}],
  "summary":       "One paragraph plain-English summary."
}
"""


def run_agent(target_dir: str = "src/", browser_url: str | None = None) -> dict:
    client = anthropic.Anthropic()

    user_msg = f"Review the code under '{target_dir}'."
    if browser_url:
        user_msg += f" Also run browser_check_url on: {browser_url}"

    messages: list[dict] = [{"role": "user", "content": user_msg}]

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8096,
            system=_SYSTEM,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = dispatch(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})
            continue

        # end_turn — extract the JSON report from the final text block
        for block in response.content:
            if hasattr(block, "text"):
                text = block.text.strip()
                start = text.find("{")
                end   = text.rfind("}") + 1
                if start != -1 and end > start:
                    try:
                        return json.loads(text[start:end])
                    except json.JSONDecodeError:
                        pass
                return {"raw": text}

        return {"error": "Agent produced no parseable output"}


# ── CLI entry point ────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Autonomous code review agent (security · compatibility · scalability)"
    )
    parser.add_argument("--target", default="src/", help="Directory or file to review (default: src/)")
    parser.add_argument(
        "--browser", metavar="URL", default=None,
        help="Live HTTP/HTTPS URL to audit in headless Chromium after static checks",
    )
    parser.add_argument("--output", metavar="FILE", default=None, help="Write JSON report to file")
    args = parser.parse_args()

    print(f"[agent_review] starting review of '{args.target}' …", file=sys.stderr)
    report = run_agent(target_dir=args.target, browser_url=args.browser)
    output = json.dumps(report, indent=2)

    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        print(f"[agent_review] report written to {args.output}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
