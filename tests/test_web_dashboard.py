"""Cheap guards on the web dashboard's JS.

frontend/src/legacy/app.js still carries ~3000 lines of hand-written,
template-string-rendering JS (the pre-modularization dashboard, moved
mechanically into a Vite project — see frontend/README or the promotion-log
note on the frontend/desktop split). Vite's own build catches a hard syntax
error, but not a silently-wrong one (a duplicated `let paths` left behind by
an edit passed `vite build` fine and still bricked every handler on the page —
that's the incident these guards exist for), so this file keeps checking the
source directly rather than trusting the bundler alone.

As views get extracted out of app.js into frontend/src/views/*.js (see
docs/frontend-migration notes), each extracted file should get the same
`node --check` + dangling-handler guards, not lose them.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

DASHBOARD_HTML = Path("frontend/index.html")
SRC_DIR = Path("frontend/src")


def _source_files() -> list[Path]:
    return sorted(SRC_DIR.rglob("*.js"))


def _script() -> str:
    """All of the dashboard's JS, concatenated — for definition-scanning and
    for pulling one self-contained function's source out via
    _function_source(). NOT meant to be run as a single program: it's several
    ES modules now (legacy/app.js + views/*.js), each syntax-checked
    separately by test_the_dashboard_scripts_parse."""
    return "\n".join(p.read_text() for p in _source_files())


def _all_markup() -> str:
    """Every place an onclick/onchange attribute can appear: the static shell
    in frontend/index.html, and the dynamic template strings the scripts build
    panels from — the wizard/source panels are innerHTML'd from JS, not
    present in the shell markup at all."""
    return DASHBOARD_HTML.read_text() + "\n" + _script()


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_the_dashboard_scripts_parse():
    """The guard that matters: a parse error bricks every button on the page.

    Each file is checked separately (as an ES module — they use import/export
    now) rather than concatenated into one program: a concatenation would
    parse import/export statements from multiple files as one module and
    prove nothing about whether any individual file is valid on its own.
    """
    for path in _source_files():
        proc = subprocess.run(["node", "--check", "--input-type=module", "-"],
                              input=path.read_text(), capture_output=True, text=True, timeout=30)
        assert proc.returncode == 0, f"{path} does not parse:\n{proc.stderr}"


def test_every_inline_handler_names_a_function_that_exists():
    """A typo'd handler is invisible until someone clicks (or picks a file)."""
    markup = _all_markup()
    script = _script()
    called = {m.group(1)
              for m in re.finditer(r"on(?:click|change)=[\"'](?:event\.\w+\(\);)?(\w+)\(", markup)}
    defined = set()
    for pattern in (
        r"(?:async\s+)?function\s+(\w+)\s*\(",     # function foo(...)
        r"(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\(",   # const foo = (...) =>
        r"(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?function",
    ):
        defined |= {m.group(1) for m in re.finditer(pattern, script)}
    missing = called - defined
    assert not missing, f"onclick handlers with no definition: {sorted(missing)}"


def test_every_inline_handler_is_reachable_from_window():
    """Existing (in some file) is not the same as callable (from an inline
    handler). ES modules don't expose their top-level declarations globally
    the way the old single classic <script> did, so each file that generates
    onclick/onchange markup explicitly re-attaches what that markup calls onto
    `window` (see the Object.assign(window, {...}) block at the bottom of
    legacy/app.js and views/wizard.js). A function can be defined, tested, and
    still be invisible to a click if that line is missing — this is the gap
    that let `runRoute`, `renderIngest`, and the bare `wiz.carrier=...`/
    `wiz.fork=...`/`wiz.label=...` mutations slip through during the module
    migration despite every function existing and every test up to this one
    passing. `event` is the one implicit global inline handlers get for free.

    Handles two shapes: a bare call (`onclick="foo('x')"`, optionally after an
    `event.stopPropagation();`-style prefix) and a bare property mutation
    (`onchange="wiz.carrier=this.value"`) — both need their LEADING identifier
    on window, whether it turns out to be a function or a plain object.
    """
    markup = _all_markup()
    script = _script()

    called = set()
    for m in re.finditer(r'on(?:click|change|input)="((?:[^"\\]|\\.)*)"', markup):
        for stmt in m.group(1).replace("\\'", "'").split(";"):
            ref = re.match(r"^([a-zA-Z_]\w*)\s*(?:\(|\.|=)", stmt.strip())
            if ref:
                called.add(ref.group(1))
    called.discard("event")

    exported = set()
    for block in re.finditer(r"Object\.assign\(window,\s*\{(.*?)\}\s*\)", script, re.S):
        exported |= {name for name in re.findall(r"(\w+)\s*(?:,|$)", block.group(1)) if name}

    missing = called - exported
    assert not missing, (
        f"inline handlers reference {sorted(missing)}, which no "
        "Object.assign(window, {...}) block exports — defined somewhere is not "
        "the same as reachable from a click."
    )


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


# --- opening a source lands on the route that last ran -----------------------


def _last_run_route(transports: list[dict]) -> str | None:
    """What the page would select when the operator expands this source."""
    script = _script()
    program = "\n".join([
        _function_source(script, "lastRunRoute"),
        f"const s = {json.dumps({'transports': transports})};",
        "const r = lastRunRoute(s);",
        "console.log(JSON.stringify(r ? r.id : null));",
    ])
    proc = subprocess.run(["node", "-e", program], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _ran(route_id: str, at: str | None) -> dict:
    return {"id": route_id, "last_run": {"parsed_at": at} if at else None}


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_the_selected_node_is_the_one_that_ran_most_recently():
    """Not the first route listed, and not the first that has ever run — the
    LATEST run, because that is the one holding the rows drawn underneath."""
    assert _last_run_route([
        _ran("upload", "2026-07-02T09:00:00+00:00"),
        _ran("scrape", "2026-07-29T18:00:00+00:00"),
        _ran("email", "2026-06-11T07:00:00+00:00"),
    ]) == "scrape"


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_routes_that_have_never_run_are_not_selectable_defaults():
    """A route with no run of its own would open the source onto "Not run",
    hiding rows the source actually has."""
    assert _last_run_route([
        _ran("upload", None),
        _ran("scrape", "2026-07-29T18:00:00+00:00"),
    ]) == "scrape"


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_a_source_that_has_never_run_selects_nothing():
    """Nothing selected is what leaves a brand-new source showing its build
    panel, which is the only thing worth doing to it."""
    assert _last_run_route([_ran("upload", None), _ran("scrape", None)]) is None
    assert _last_run_route([]) is None


def test_no_way_to_pin_a_favourite_route_is_left_on_the_page():
    """The star set a default the operator then had to keep in sync with what
    actually ran. Opening onto the last run answers the same question from
    history, so the control — and its handler — are gone rather than dormant."""
    markup = _all_markup()

    assert "pinDefault" not in markup
    assert "set_default_transport" not in markup
    assert "★" not in markup and "☆" not in markup


def test_the_transport_tool_surface_the_page_calls_actually_exists():
    """The page is the only caller of these, so a rename would break it silently.

    Both spellings: callTool() straight, and act(), which is callTool plus the run
    timeline. ⏵ on a chip goes through act(), so a route wired to a tool that
    isn't registered fails only when someone presses it.
    """
    from interfaces import mcp_tools

    script = _script()
    names = {fn.__name__ for fn in mcp_tools.ALL_TOOLS}
    called = set(re.findall(r"callTool\(\s*'(\w+)'", script))
    called |= set(re.findall(r"\bact\(\s*'(\w+)'", script))
    missing = called - names
    assert not missing, f"page calls tools that are not registered: {sorted(missing)}"
