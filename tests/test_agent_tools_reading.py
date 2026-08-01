"""How the agent reads the repo — the second-biggest source of context bloat.

Measured on a build that died: eight whole-file reads pulled in 90,000 characters
(~22,500 tokens), including all 21,000 of browser_session.py to use one function
(`launch`), and all 20,000 of a different source's scraper. These are the tools
that make the cheap version of that possible.
"""

import pytest

from orchestration import agent_tools as tools


# --- reading a slice --------------------------------------------------------

def test_a_slice_returns_only_those_lines():
    out = tools.read_file("core/progress.py", 1, 3)
    assert "lines 1-3 of" in out
    assert out.count("\n") <= 4


def test_lines_are_numbered_so_the_next_slice_can_be_asked_for():
    out = tools.read_file("core/progress.py", 10, 12)
    assert "   10 " in out and "   12 " in out


def test_the_slice_is_inclusive_at_both_ends():
    out = tools.read_file("core/progress.py", 5, 7)
    body = out.split(":\n", 1)[1]
    assert len([ln for ln in body.splitlines() if ln.strip()]) <= 3


def test_an_end_past_the_file_is_clamped_not_an_error():
    assert "lines 1-" in tools.read_file("core/progress.py", 1, 99999)


def test_a_start_past_the_end_says_so():
    with pytest.raises(tools.ToolError, match="past the end"):
        tools.read_file("core/progress.py", 99999)


def test_a_whole_file_read_still_works():
    assert "def step(" in tools.read_file("core/progress.py")


def test_a_large_whole_file_read_points_at_the_cheaper_way():
    """The nudge is the only thing that reaches an agent already mid-run."""
    out = tools.read_file("core/tools/browser_session.py")
    assert "search_files" in out and "start_line" in out
    assert len(out) < 21000


# --- the outline ------------------------------------------------------------

def test_outline_gives_the_callable_surface_not_the_bodies():
    out = tools.outline("core/tools/browser_session.py")
    assert "def launch(" in out
    # The signature and the one-line summary, never a statement from inside it.
    assert "launch_persistent_context" not in out
    assert "return" not in out


def test_outline_is_a_fraction_of_the_file():
    path = "core/tools/browser_session.py"
    whole = len(open(path).read())
    assert len(tools.outline(path)) < whole / 10


def test_outline_keeps_module_constants_because_they_are_interface():
    """SETTINGS / SERVICE_KEY / METHOD are exactly what a scraper is asked about."""
    out = tools.outline("core/scrapers/epic_property_management.py")
    assert "SETTINGS = ..." in out and "SERVICE_KEY = ..." in out


def test_outline_hides_private_names():
    out = tools.outline("core/tools/browser_session.py")
    assert "_SESSION_COOKIES_FILE" not in out
    assert "def _session_cookies_path" not in out


def test_outline_refuses_a_non_module():
    with pytest.raises(tools.ToolError, match="not a Python module"):
        tools.outline("README.md")


# --- searching --------------------------------------------------------------

def test_search_reports_path_line_and_text():
    out = tools.search_files(r"def api_headers")
    assert "core/tools/q2_online_banking.py:" in out
    assert "def api_headers" in out


def test_search_can_be_narrowed_to_a_directory():
    out = tools.search_files(r"def launch", "core/tools")
    assert "core/scrapers" not in out


def test_search_says_so_when_there_is_nothing():
    assert "No match" in tools.search_files(r"zzz_not_a_real_symbol_zzz")


def test_search_caps_its_own_output():
    out = tools.search_files(r".", "core", max_results=5)
    assert len(out.splitlines()) <= 6


def test_search_rejects_a_bad_pattern_instead_of_raising_re_error():
    with pytest.raises(tools.ToolError, match="Bad search pattern"):
        tools.search_files(r"(unclosed")


def test_search_stays_inside_the_repo():
    with pytest.raises(tools.ToolError, match="outside the repository"):
        tools.search_files(r"x", "../..")


def test_search_skips_the_directories_that_would_swamp_it():
    """.venv and .git dwarf the source; a hit in either is never the answer."""
    out = tools.search_files(r"def launch", ".", max_results=40)
    assert ".venv" not in out and "node_modules" not in out
