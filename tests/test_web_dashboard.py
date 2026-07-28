"""Cheap guards on the single-file web dashboard.

interfaces/web/index.html carries ~1000 lines of hand-written JS and no build
step, so nothing catches a syntax error before the operator opens the page — and
a broken parse takes out the WHOLE app silently: every handler becomes undefined,
the panel renders empty, and no console error necessarily surfaces. That happened
during development (a duplicated `let paths` left behind by an edit), and cost
more to diagnose than this file costs to keep.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

DASHBOARD = Path("interfaces/web/index.html")


def _script() -> str:
    html = DASHBOARD.read_text()
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert scripts, "the dashboard has no inline script"
    return "\n".join(scripts)


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_the_dashboard_script_parses():
    """The guard that matters: a parse error bricks every button on the page."""
    proc = subprocess.run(["node", "--check", "-"], input=_script(),
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"index.html script does not parse:\n{proc.stderr}"


def test_every_onclick_names_a_function_that_exists():
    """A typo'd handler is invisible until someone clicks it."""
    html = DASHBOARD.read_text()
    script = _script()
    called = {m.group(1) for m in re.finditer(r"onclick=[\"'](?:event\.\w+\(\);)?(\w+)\(", html)}
    defined = set()
    for pattern in (
        r"(?:async\s+)?function\s+(\w+)\s*\(",     # function foo(...)
        r"(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\(",   # const foo = (...) =>
        r"(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?function",
    ):
        defined |= {m.group(1) for m in re.finditer(pattern, script)}
    missing = called - defined
    assert not missing, f"onclick handlers with no definition: {sorted(missing)}"


def test_the_transport_tool_surface_the_page_calls_actually_exists():
    """The page is the only caller of these, so a rename would break it silently."""
    from interfaces import mcp_tools

    names = {fn.__name__ for fn in mcp_tools.ALL_TOOLS}
    called = set(re.findall(r"callTool\(\s*'(\w+)'", DASHBOARD.read_text()))
    missing = called - names
    assert not missing, f"page calls tools that are not registered: {sorted(missing)}"
