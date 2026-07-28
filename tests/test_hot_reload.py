"""Picking up code the harness just wrote, without restarting the server.

The harness rewrites its own domain code while agent-web is running. Python
caches modules, so without an explicit reload the server keeps executing the
version it imported at startup: the agent reports a successful fix and the OLD
code runs. That is a silent wrong-answer bug, not an inconvenience.

Built against a throwaway package that mirrors the real
`core/scrapers/__init__.py` shape (REGISTRY binding a function imported from a
per-source module), so the reload is exercised for real rather than mocked.
"""

import importlib
import sys
import textwrap

import pytest

from core import hot_reload


@pytest.fixture
def fake_package(tmp_path, monkeypatch):
    """A package shaped like core/scrapers: REGISTRY binds a per-source callable."""
    pkg = tmp_path / "demopkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(textwrap.dedent("""
        from demopkg.src_one import retrieve as _one
        REGISTRY = {"src_one": _one}

        def get(key):
            return REGISTRY[key]
    """))
    (pkg / "src_one.py").write_text('def retrieve():\n    return "OLD"\n')

    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setitem(hot_reload._PACKAGES, "scraper", "demopkg")
    for name in [n for n in sys.modules if n.startswith("demopkg")]:
        del sys.modules[name]

    yield pkg

    for name in [n for n in sys.modules if n.startswith("demopkg")]:
        del sys.modules[name]


def _rewrite(pkg, body):
    (pkg / "src_one.py").write_text(f'def retrieve():\n    return "{body}"\n')


def test_without_a_reload_the_process_keeps_running_the_old_code(fake_package):
    """The bug being fixed. Documented so nobody 'simplifies' the reload away."""
    import demopkg
    assert demopkg.get("src_one")() == "OLD"

    _rewrite(fake_package, "NEW")
    assert demopkg.get("src_one")() == "OLD", "stale until reloaded — this is the problem"


def test_reload_picks_up_the_rewritten_code(fake_package):
    import demopkg
    assert demopkg.get("src_one")() == "OLD"

    _rewrite(fake_package, "NEW")
    result = hot_reload.reload_source_code("scraper", "src_one")

    assert result["reloaded"] is True
    assert sys.modules["demopkg"].get("src_one")() == "NEW"


def test_a_same_length_edit_in_the_same_second_is_still_picked_up(fake_package):
    """The trap: the bytecode cache validates on (mtime, size), so an edit of
    identical length landing in the same second looks 'unchanged' and the stale
    .pyc is reused. Measured before invalidate_caches() was added: the reload
    returned the previous code."""
    import demopkg
    assert demopkg.get("src_one")() == "OLD"

    _rewrite(fake_package, "NEW")          # exactly the same byte length as "OLD"? no —
    _rewrite(fake_package, "OLE")          # same length as "OLD", written immediately
    hot_reload.reload_source_code("scraper", "src_one")

    assert sys.modules["demopkg"].get("src_one")() == "OLE"


def test_the_registry_is_rebound_not_just_the_module(fake_package):
    """REGISTRY binds the FUNCTION OBJECT at import time, so reloading only the
    submodule leaves the registry pointing at the old function."""
    import demopkg
    _rewrite(fake_package, "NEW")

    # submodule only — deliberately incomplete
    importlib.invalidate_caches()
    importlib.reload(sys.modules["demopkg.src_one"])
    assert demopkg.REGISTRY["src_one"]() == "OLD", "registry still holds the old function"

    hot_reload.reload_source_code("scraper", "src_one")
    assert sys.modules["demopkg"].REGISTRY["src_one"]() == "NEW"


def test_a_previously_imported_helper_sees_the_new_registry(fake_package):
    """mcp_tools does `from core.scrapers import get_scraper` at startup. reload()
    re-executes the package in its EXISTING namespace, so that already-imported
    helper reads the new REGISTRY through its unchanged __globals__. If this ever
    stops holding, run_scraper would keep calling the old code."""
    from demopkg import get as get_before_reload

    _rewrite(fake_package, "NEW")
    hot_reload.reload_source_code("scraper", "src_one")

    assert get_before_reload("src_one")() == "NEW"


def test_a_module_never_imported_yet_is_simply_imported(fake_package):
    (fake_package / "src_two.py").write_text('def retrieve():\n    return "TWO"\n')
    result = hot_reload.reload_source_code("scraper", "src_two")

    assert result["reloaded"] is True
    assert sys.modules["demopkg.src_two"].retrieve() == "TWO"


def test_a_broken_rewrite_is_reported_not_raised(fake_package):
    """A syntax error from the agent must not take down the request."""
    import demopkg  # noqa: F401
    (fake_package / "src_one.py").write_text("def retrieve(  syntax error\n")

    result = hot_reload.reload_source_code("scraper", "src_one")

    assert result["reloaded"] is False
    assert "restart" in result["error"].lower(), "tell the operator the fallback"


def test_an_unknown_kind_is_rejected():
    result = hot_reload.reload_source_code("fetcher", "x")
    assert result["reloaded"] is False and "Unknown kind" in result["error"]
