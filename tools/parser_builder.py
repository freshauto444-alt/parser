#!/usr/bin/env python3
"""
Parser Builder — Claude agent with tool use for analyzing websites and writing scrapers.

Claude is given 4 tools: fetch_page, extract_structure, run_python, save_parser.
It iterates until it produces a working parser and saves it.

Usage:
    python tools/parser_builder.py --url https://www.autoscout24.com/lst/
    python tools/parser_builder.py --url https://www.bytbil.com/bil --task "extract body_type field"
    python tools/parser_builder.py --url https://www.autoscout24.com/lst/ --fix "body_type is always None in parse_listing_from_nextdata"

Requirements:
    pip install anthropic httpx
"""

import argparse
import json
import re
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import httpx

try:
    import anthropic
except ImportError:
    print("ERROR: anthropic SDK not installed. Run: pip install anthropic", file=sys.stderr)
    sys.exit(1)

# ── Constants ─────────────────────────────────────────────────────────────────

TOOLS_DIR = Path(__file__).parent
MODEL = "claude-sonnet-4-6"
MAX_ITERATIONS = 12
MAX_TOOL_OUTPUT = 40_000  # characters per tool result

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/136.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9,de;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

SYSTEM_PROMPT = """You are an expert Python web scraping engineer specializing in car listing sites.
You write clean, async, production-ready parsers that integrate with the Fresh Auto project.

Project context:
- Parsers live in parsers/ directory
- All parsers return list[dict] with fields: make, model, price, year, mileage, fuel, transmission, body_type, features, images, url
- Use httpx for HTTP (never requests), async/await, loguru for logging
- Parse __NEXT_DATA__ from Next.js sites — it contains full listing data
- Key field names in the project DB: price (EUR int), mileage (km int), body_type (string)
- Features go through translate_and_categorize_features() from base.py
- Swedish prices in SEK must be converted to EUR

Workflow:
1. Fetch the target URL to understand the site structure
2. Check if it's Next.js (look for __NEXT_DATA__ in HTML)
3. If Next.js: extract __NEXT_DATA__ JSON, analyze structure to find listings array and all fields
4. If classic HTML: inspect listing card structure, find CSS selectors for each field
5. Write test code to extract one listing and verify all fields
6. Fix issues, re-test until all fields extract correctly
7. Write the complete parser and save it

IMPORTANT: Always extract body_type. In Next.js __NEXT_DATA__, look for fields named:
carBodyType, bodyType, category, vehicleCategory, type in the vehicle or listing object.
Also check vehicleDetails[] array items for body type labels."""

TOOL_DEFINITIONS = [
    {
        "name": "fetch_page",
        "description": (
            "Fetch HTML from a URL. "
            "Use extract_next_data=true for Next.js sites to get the __NEXT_DATA__ JSON. "
            "Use show_bytes to control how much HTML to show (default 8000)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Full URL to fetch"},
                "extract_next_data": {
                    "type": "boolean",
                    "description": "Extract and return __NEXT_DATA__ JSON from HTML (for Next.js sites)",
                },
                "show_bytes": {
                    "type": "integer",
                    "description": "How many bytes of raw HTML to return when not extracting JSON (default 8000)",
                },
                "headers": {
                    "type": "object",
                    "description": "Additional HTTP headers to send",
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "extract_structure",
        "description": (
            "Show the nested structure of a JSON object — keys, types, sample values — "
            "up to 4 levels deep. "
            "Use path to navigate to a specific sub-object (dot-notation, e.g. 'props.pageProps.listings.0')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "json_text": {
                    "type": "string",
                    "description": "JSON string to analyze",
                },
                "path": {
                    "type": "string",
                    "description": "Dot-notation path to navigate into (e.g. 'props.pageProps.listings.0')",
                },
                "max_keys": {
                    "type": "integer",
                    "description": "Max keys to show per dict level (default 30)",
                },
            },
            "required": ["json_text"],
        },
    },
    {
        "name": "run_python",
        "description": (
            "Execute Python code and return stdout/stderr. "
            "Use to test parsing logic against real HTML/JSON. "
            "You can import httpx, json, re, bs4. "
            "Code runs in a temporary file with 30s timeout."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python code to execute"},
            },
            "required": ["code"],
        },
    },
    {
        "name": "save_parser",
        "description": (
            "Save the final parser code to a .py file in the tools/ directory. "
            "Call this only when the parser is complete and tested."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "Output filename (e.g. 'parser_as24_new.py')",
                },
                "code": {
                    "type": "string",
                    "description": "Complete, working Python parser code",
                },
                "summary": {
                    "type": "string",
                    "description": "One-paragraph summary of what was found and implemented",
                },
            },
            "required": ["filename", "code"],
        },
    },
]

# ── Tool implementations ───────────────────────────────────────────────────────

def _fetch_page(
    url: str,
    extract_next_data: bool = False,
    show_bytes: int = 8000,
    headers: dict | None = None,
) -> str:
    combined_headers = {**HEADERS, **(headers or {})}
    try:
        r = httpx.get(url, headers=combined_headers, timeout=30, follow_redirects=True)
        html = r.text
        status_line = f"HTTP {r.status_code}  {len(html):,} bytes  {r.url}\n\n"

        if extract_next_data:
            m = re.search(
                r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
                html,
                re.DOTALL,
            )
            if not m:
                return status_line + "No __NEXT_DATA__ script found on this page."
            raw_json = m.group(1).strip()
            try:
                parsed = json.loads(raw_json)
                pretty = json.dumps(parsed, ensure_ascii=False, indent=2)
                return status_line + f"__NEXT_DATA__ ({len(pretty):,} chars):\n\n" + pretty[:MAX_TOOL_OUTPUT]
            except json.JSONDecodeError as e:
                return status_line + f"Found __NEXT_DATA__ but JSON parse failed: {e}\nRaw (first 2000):\n{raw_json[:2000]}"

        return status_line + html[:show_bytes]

    except Exception as e:
        return f"fetch_page error: {type(e).__name__}: {e}"


def _show_structure(obj: object, depth: int, max_depth: int, max_keys: int) -> str:
    indent = "  " * depth
    if depth >= max_depth:
        if isinstance(obj, dict):
            return f"{{...{len(obj)} keys}}"
        if isinstance(obj, list):
            return f"[...{len(obj)} items]"
        return repr(obj)[:80]

    if isinstance(obj, dict):
        if not obj:
            return "{}"
        lines = ["{"]
        items = list(obj.items())[:max_keys]
        for k, v in items:
            val = _show_structure(v, depth + 1, max_depth, max_keys)
            lines.append(f'{indent}  "{k}": {val}')
        if len(obj) > max_keys:
            lines.append(f'{indent}  ... ({len(obj) - max_keys} more keys)')
        lines.append(f"{indent}}}")
        return "\n".join(lines)

    if isinstance(obj, list):
        if not obj:
            return "[]"
        first = _show_structure(obj[0], depth + 1, max_depth, max_keys)
        return f"[{len(obj)} items, first:\n{indent}  {first}\n{indent}]"

    if isinstance(obj, str):
        preview = obj[:120].replace("\n", "\\n")
        suffix = "..." if len(obj) > 120 else ""
        return f'"{preview}{suffix}"'

    return repr(obj)[:120]


def _extract_structure(
    json_text: str,
    path: str = "",
    max_keys: int = 30,
) -> str:
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError as e:
        return f"JSON parse error: {e}\nFirst 500 chars: {json_text[:500]}"

    if path:
        try:
            for key in path.split("."):
                if isinstance(data, list) and key.isdigit():
                    data = data[int(key)]
                elif isinstance(data, dict):
                    data = data[key]
                else:
                    return f"Cannot navigate to '{key}' in {type(data).__name__}"
        except (KeyError, IndexError) as e:
            return f"Path navigation error at '{key}': {e}"

    return _show_structure(data, depth=0, max_depth=4, max_keys=max_keys)


def _run_python(code: str) -> str:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(code)
        fname = f.name
    try:
        result = subprocess.run(
            [sys.executable, fname],
            capture_output=True,
            text=True,
            timeout=30,
        )
        stdout = result.stdout[-6000:] if result.stdout else ""
        stderr = result.stderr[-3000:] if result.stderr else ""
        parts = []
        if stdout:
            parts.append(f"STDOUT:\n{stdout}")
        if stderr:
            parts.append(f"STDERR:\n{stderr}")
        if result.returncode != 0 and not stderr:
            parts.append(f"Exit code: {result.returncode}")
        return "\n\n".join(parts) if parts else "(no output)"
    except subprocess.TimeoutExpired:
        return "TIMEOUT: code took longer than 30 seconds"
    except Exception as e:
        return f"run_python error: {type(e).__name__}: {e}"
    finally:
        Path(fname).unlink(missing_ok=True)


def _save_parser(filename: str, code: str, summary: str = "") -> str:
    if not filename.endswith(".py"):
        filename += ".py"
    out_path = TOOLS_DIR / filename
    out_path.write_text(code, encoding="utf-8")
    msg = f"Saved to {out_path}"
    if summary:
        msg += f"\n\nSummary:\n{summary}"
    return msg


def execute_tool(name: str, inputs: dict) -> str:
    try:
        if name == "fetch_page":
            return _fetch_page(**inputs)
        if name == "extract_structure":
            return _extract_structure(**inputs)
        if name == "run_python":
            return _run_python(**inputs)
        if name == "save_parser":
            return _save_parser(**inputs)
        return f"Unknown tool: {name}"
    except TypeError as e:
        return f"Tool call error (bad arguments): {e}"
    except Exception as e:
        return f"Tool execution error: {type(e).__name__}: {e}"


# ── Agentic loop ──────────────────────────────────────────────────────────────

def run_agent(url: str, task: str, output: str) -> None:
    client = anthropic.Anthropic()

    user_message = textwrap.dedent(f"""
        Analyze this car listing site and write a production-ready Python parser for it.

        URL: {url}
        Task: {task}
        Output file: {output}

        Start by fetching the page. Check for __NEXT_DATA__ (Next.js sites).
        Then systematically extract all available fields, paying special attention to body_type.
        Test your extraction code with run_python before saving the final parser.
    """).strip()

    messages: list[dict] = [{"role": "user", "content": user_message}]

    print(f"\n[parser_builder] Starting agent for: {url}")
    print(f"[parser_builder] Task: {task}")
    print(f"[parser_builder] Output: {output}\n")

    saved = False

    for iteration in range(MAX_ITERATIONS):
        print(f"[iter {iteration + 1}/{MAX_ITERATIONS}] Calling Claude...")

        response = client.messages.create(
            model=MODEL,
            max_tokens=8000,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=TOOL_DEFINITIONS,
            messages=messages,
        )

        messages.append({"role": "assistant", "content": response.content})

        # Print any text blocks Claude produced
        for block in response.content:
            if hasattr(block, "text") and block.text:
                print(f"\n[Claude]\n{block.text[:800]}\n")

        if response.stop_reason == "end_turn":
            print("\n[parser_builder] Agent finished (end_turn).")
            break

        if response.stop_reason != "tool_use":
            print(f"[parser_builder] Unexpected stop_reason: {response.stop_reason}")
            break

        # Execute all tool calls
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            print(f"[tool] {block.name}({json.dumps(block.input, ensure_ascii=False)[:150]})")
            result = execute_tool(block.name, block.input)
            # Truncate very large results
            if len(result) > MAX_TOOL_OUTPUT:
                result = result[:MAX_TOOL_OUTPUT] + f"\n... (truncated, {len(result):,} chars total)"
            print(f"[result] {result[:200]}{'...' if len(result) > 200 else ''}\n")

            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                }
            )

            if block.name == "save_parser":
                saved = True

        messages.append({"role": "user", "content": tool_results})

        if saved:
            print(f"\n[parser_builder] Parser saved. Done after {iteration + 1} iterations.")
            break
    else:
        print(f"\n[parser_builder] Reached max iterations ({MAX_ITERATIONS}) without saving.")


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Claude-powered parser builder for car listing sites",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
            Examples:
              python tools/parser_builder.py --url https://www.autoscout24.com/lst/
              python tools/parser_builder.py --url https://www.autoscout24.com/lst/ \\
                  --fix "body_type is always None in parse_listing_from_nextdata" \\
                  --output as24_body_type_fix.py
        """),
    )
    parser.add_argument("--url", required=True, help="Car listing page URL to analyze")
    parser.add_argument(
        "--task",
        default="Write a complete parser that extracts all available fields including body_type and features.",
        help="Specific task description for Claude",
    )
    parser.add_argument(
        "--fix",
        default="",
        help="Describe a specific bug to fix (appended to task)",
    )
    parser.add_argument(
        "--output",
        default="parser_generated.py",
        help="Output filename (saved to tools/ directory)",
    )
    args = parser.parse_args()

    task = args.task
    if args.fix:
        task = f"{task}\n\nSpecific bug to fix: {args.fix}"

    run_agent(url=args.url, task=task, output=args.output)


if __name__ == "__main__":
    main()
