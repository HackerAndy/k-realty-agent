#!/usr/bin/env python3
"""Portability lint: fails if core/ or evals/ import langgraph or langchain.

These two directories are the "portability contract" — if a harness swap is
ever needed (Claude Agent SDK, OpenAI SDK, managed platform), only
orchestration/ should need to be rewritten. Run standalone or via CI:

    python scripts/check_portability.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GUARDED_DIRS = ["core", "evals"]
BANNED_PREFIXES = ("langgraph", "langchain")


def is_banned(module_name: str | None) -> bool:
    if not module_name:
        return False
    return module_name.split(".")[0] in BANNED_PREFIXES


def find_violations(py_file: Path) -> list[tuple[int, str]]:
    tree = ast.parse(py_file.read_text(), filename=str(py_file))
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if is_banned(alias.name):
                    violations.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if is_banned(node.module):
                violations.append((node.lineno, node.module or ""))
    return violations


def main() -> int:
    all_violations: list[str] = []
    for dir_name in GUARDED_DIRS:
        guarded_dir = REPO_ROOT / dir_name
        if not guarded_dir.is_dir():
            continue
        for py_file in sorted(guarded_dir.rglob("*.py")):
            for lineno, module_name in find_violations(py_file):
                rel_path = py_file.relative_to(REPO_ROOT)
                all_violations.append(f"{rel_path}:{lineno}: imports '{module_name}'")

    if all_violations:
        print("Portability check FAILED — langgraph/langchain imports found outside orchestration/:")
        for violation in all_violations:
            print(f"  {violation}")
        print(f"\n{GUARDED_DIRS} must stay framework-free. Move this code into orchestration/.")
        return 1

    print(f"Portability check passed — no langgraph/langchain imports in {GUARDED_DIRS}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
