#!/usr/bin/env python3
"""Static guard for the recurring "package split broke a relative import" bug.

Twice now a god-file split into a subpackage left a single-dot relative import
pointing at a ROOT module that is no longer a sibling:
  - base.py  -> base/  (fix(base): add missing imports lost in the base.py split)
  - parser_sweden.py -> parser_sweden/  (Bytbil dead in prod:
    "No module named 'parsers.parser_sweden.rate_limiter'")

`import module` does NOT catch these, because the bad imports are DEFERRED
(inside functions) and only run on a hot code path. This linter parses every
file under parsers/ and flags any `from .X import ...` (level == 1) whose target
`X` is not an actual sibling module/package in the same directory. Such an import
almost always needs more dots (`from ..X`) to reach a parent-level module.

Run:  python tools/check_relative_imports.py   (exit 1 on any violation)
A good pre-deploy / CI gate and a /check-sources companion.
"""
from __future__ import annotations
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "parsers"
SKIP = {"venv", "__pycache__", ".git"}


def sibling_exists(file: Path, name: str) -> bool:
    """Is `name` a module/package sibling of `file` (same directory)?"""
    d = file.parent
    top = name.split(".")[0]
    return (d / f"{top}.py").exists() or (d / top / "__init__.py").exists()


def violations() -> list[str]:
    out: list[str] = []
    for py in PKG.rglob("*.py"):
        if any(part in SKIP for part in py.parts):
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        except SyntaxError as e:
            out.append(f"{py}: SYNTAX ERROR: {e}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
                if not sibling_exists(py, node.module):
                    rel = py.relative_to(ROOT)
                    out.append(
                        f"{rel}:{node.lineno}: `from .{node.module}` has no sibling "
                        f"`{node.module}` here — did you mean `from ..{node.module}`?"
                    )
    return out


def main() -> int:
    bad = violations()
    if bad:
        print("Relative-import check FAILED:\n")
        for v in bad:
            print(f"  {v}")
        print(f"\n{len(bad)} violation(s).")
        return 1
    print("Relative-import check passed — all single-dot imports resolve to siblings.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
