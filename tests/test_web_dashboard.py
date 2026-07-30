"""Cheap guards on the single-file web dashboard.

interfaces/web/index.html carries ~1000 lines of hand-written JS and no build
step, so nothing catches a syntax error before the operator opens the page — and
a broken parse takes out the WHOLE app silently: every handler becomes undefined,
the panel renders empty, and no console error necessarily surfaces. That happened
during development (a duplicated `let paths` left behind by an edit), and cost
more to diagnose than this file costs to keep.
"""

import json
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


def test_every_inline_handler_names_a_function_that_exists():
    """A typo'd handler is invisible until someone clicks (or picks a file)."""
    html = DASHBOARD.read_text()
    script = _script()
    called = {m.group(1)
              for m in re.finditer(r"on(?:click|change)=[\"'](?:event\.\w+\(\);)?(\w+)\(", html)}
    defined = set()
    for pattern in (
        r"(?:async\s+)?function\s+(\w+)\s*\(",     # function foo(...)
        r"(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\(",   # const foo = (...) =>
        r"(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?function",
    ):
        defined |= {m.group(1) for m in re.finditer(pattern, script)}
    missing = called - defined
    assert not missing, f"onclick handlers with no definition: {sorted(missing)}"


# --- the graph's two columns line up ----------------------------------------
#
# The funnel draws ways in on the left and readers in the middle, and connects
# them with curves. Routes arrive in a fixed order (upload, website, mailbox)
# but readers are laid out in the order they are first needed — so Epic, whose
# mailbox and upload share one parser, drew Mailbox's arrow ACROSS Website's to
# reach it. Crossed lines read as a relationship between the things they cross,
# which is precisely what these two routes do not have.
#
# These run the real functions rather than checking for their text, because the
# property that matters (no crossings) is arithmetic on the drawn positions.


def _function_source(script: str, name: str) -> str:
    """One function, brace-matched out of the page's inline script."""
    start = script.index(f"function {name}(")
    depth, i = 0, script.index("{", start)
    for i in range(i, len(script)):
        if script[i] == "{":
            depth += 1
        elif script[i] == "}":
            depth -= 1
            if depth == 0:
                return script[start:i + 1]
    raise AssertionError(f"{name}() never closes")


def _order(routes: list[dict]) -> dict:
    """What the page would draw: the route order, and each one's reader row."""
    script = _script()
    program = "\n".join([
        _function_source(script, "readerGroups"),
        _function_source(script, "routesByReader"),
        f"const routes = {json.dumps(routes)};",
        "const groups = readerGroups(routes);",
        "const rank = {};",
        "groups.forEach((g, i) => g.routes.forEach(id => { rank[id] = i; }));",
        "const ordered = routesByReader(routes, groups);",
        "console.log(JSON.stringify({",
        "  order: ordered.map(r => r.id),",
        "  rows: ordered.map(r => rank[r.id]),",
        "  readers: groups.map(g => g.reader.label),",
        "}));",
    ])
    proc = subprocess.run(["node", "-e", program], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _parser(name):
    return {"kind": "parser", "label": f"Parser · {name}", "name": name, "built": True}


API = {"kind": "api", "label": "API call", "built": True}

# Epic: the upload and the mailbox hand the same PDF to the same parser; the
# website calls the portal's API. This is the shape that crossed.
EPIC = [
    {"id": "upload", "reader": _parser("buildium_owner_statement")},
    {"id": "scrape", "reader": API},
    {"id": "email", "reader": _parser("buildium_owner_statement")},
]


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_routes_sharing_a_reader_are_drawn_together():
    drawn = _order(EPIC)

    assert drawn["order"] == ["upload", "email", "scrape"]


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_no_two_arrows_can_cross():
    """The general statement, of which the ordering above is one instance: a
    route's reader row never steps back up the column, so the curves between the
    two columns are monotonic and cannot intersect."""
    rows = _order(EPIC)["rows"]

    assert rows == sorted(rows), f"arrows cross: rows {rows}"


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_an_order_that_already_works_is_left_alone():
    """DFCU: a parser it has, a scraper it doesn't. Nothing to fix, and the
    routes must not be shuffled for the sake of it."""
    dfcu = [
        {"id": "upload", "reader": _parser("dfcu_financial_bank")},
        {"id": "scrape", "reader": {"kind": "none", "label": "No scraper yet", "built": False}},
    ]

    assert _order(dfcu)["order"] == ["upload", "scrape"]


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_two_readers_that_do_not_exist_yet_stay_two_readers():
    """"No parser yet" and "No scraper yet" are different missing things, built
    differently. Merging them into one node would draw a brand-new source as if
    its routes already converged somewhere."""
    fresh = [
        {"id": "upload", "reader": {"kind": "none", "label": "No parser yet", "built": False}},
        {"id": "scrape", "reader": {"kind": "none", "label": "No scraper yet", "built": False}},
        {"id": "email", "reader": {"kind": "none", "label": "No parser yet", "built": False}},
    ]

    drawn = _order(fresh)

    assert drawn["readers"] == ["No parser yet", "No scraper yet"]
    assert drawn["order"] == ["upload", "email", "scrape"]
    assert drawn["rows"] == sorted(drawn["rows"])


def test_the_transport_tool_surface_the_page_calls_actually_exists():
    """The page is the only caller of these, so a rename would break it silently."""
    from interfaces import mcp_tools

    names = {fn.__name__ for fn in mcp_tools.ALL_TOOLS}
    called = set(re.findall(r"callTool\(\s*'(\w+)'", DASHBOARD.read_text()))
    missing = called - names
    assert not missing, f"page calls tools that are not registered: {sorted(missing)}"
