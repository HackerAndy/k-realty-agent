"""The gate that catches code which reads as if it works.

The field case, and the reason this exists: Epic's scraper computed a property
filter from the operator's chosen property and then never used it, so the request
always asked for every property. Choosing a property in the app did nothing, and
said nothing. Every other gate was green — the settings were declared, they were
read at run time, the test passed — because the tests checked the SHAPE of the
declaration and nothing checked that a chosen value reached the request.

A value computed and thrown away is that bug, and it costs nothing to detect.
"""

from orchestration import codegen, verify


def _write(tmp_path, monkeypatch, body: str, name: str = "core/scrapers/thing.py") -> str:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    monkeypatch.setattr(verify, "REPO_ROOT", tmp_path)
    return name


def test_a_setting_read_and_then_discarded_is_caught(tmp_path, monkeypatch):
    """The exact shape of the Epic bug, reduced."""
    path = _write(tmp_path, monkeypatch, '''
def retrieve(opts, properties):
    chosen = opts["property_id"]
    matching = [p for p in properties if p["id"] == chosen]
    property_filter = matching[0]["name"] if matching else None
    return {"PropertySelectionType": "AllProperties"}
''')

    findings = verify.lint([path])

    assert any(f["code"] == "F841" and "property_filter" in f["detail"] for f in findings)


def test_a_setting_that_actually_reaches_the_request_passes(tmp_path, monkeypatch):
    path = _write(tmp_path, monkeypatch, '''
def retrieve(opts, properties):
    chosen = opts["property_id"]
    matching = [p for p in properties if p["id"] == chosen]
    property_filter = matching[0]["name"] if matching else None
    return {"PropertySelectionEntityId": property_filter}
''')

    assert verify.lint([path]) == []


def test_an_unused_import_does_not_fail_a_build(tmp_path, monkeypatch):
    """It used to, and it was the single most common reason a build was refused.

    Measured on the codegen bench: F401 blocked all but one build in a run, and
    on the case the bench scored CORRECT it was the only thing wrong — an unused
    `pytest` import in the generated test file. This gate's claim is that a value
    computed and discarded means a wire was left unconnected, which is F841. An
    unused import is tidiness, and refusing correct work over it costs a full
    rebuild and teaches nothing.
    """
    path = _write(tmp_path, monkeypatch, "from datetime import UTC, date\n\nx = date.today()\n")

    assert verify.lint([path]) == []


def test_a_value_computed_and_discarded_is_still_caught(tmp_path, monkeypatch):
    """The rule the gate actually exists for — dropping F401 must not drop this.

    A setting read and then not used is worse than one hardcoded, because the
    screen offers a choice that silently does nothing.
    """
    path = _write(tmp_path, monkeypatch,
                  'def retrieve(opts):\n'
                  '    lookback = opts["lookback_days"]\n'
                  '    return {"range": "fixed"}\n')

    assert any(f["code"] == "F841" for f in verify.lint([path]))


def test_a_name_that_does_not_exist_is_caught(tmp_path, monkeypatch):
    """The one that would crash at run time rather than merely do nothing."""
    path = _write(tmp_path, monkeypatch, "def go():\n    return undefined_helper()\n")

    assert any(f["code"] == "F821" for f in verify.lint([path]))


def test_style_is_not_the_gate_s_business(tmp_path, monkeypatch):
    """A build blocked over line length teaches the agent to fight the formatter
    instead of fixing its logic. Only rules about code that doesn't work."""
    path = _write(tmp_path, monkeypatch,
                  "x = 1\n" + "y = '" + "a" * 300 + "'\n" + "def  f( ):\n\treturn x\n")

    assert verify.lint([path]) == []


def test_non_python_files_are_skipped(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, "not python", name="core/policies/notes.yaml")

    assert verify.lint(["core/policies/notes.yaml"]) == []


def test_nothing_written_means_nothing_to_check():
    assert verify.lint([]) == []


def test_a_missing_linter_does_not_fail_every_build(tmp_path, monkeypatch):
    """A gate that can't run is a reason to say so, not to refuse all work."""
    path = _write(tmp_path, monkeypatch, "def f():\n    dead = 1\n")

    def boom(*a, **k):
        raise OSError("ruff is not installed")
    monkeypatch.setattr(verify.subprocess, "run", boom)

    assert verify.lint([path]) == []


# --- how it reaches the build and the operator ------------------------------

def test_the_build_is_refused_and_the_agent_told_what_to_reconnect(tmp_path, monkeypatch):
    path = _write(tmp_path, monkeypatch, "def f(opts):\n    basis = opts['basis']\n    return {}\n")
    calls = [("write_file", {"path": path})]

    v = codegen.fold_lint({"ok": True}, calls)

    assert v["ok"] is False
    assert any("basis" in f["detail"] for f in v["lint"])


def test_the_operator_is_told_a_wire_was_left_unconnected():
    reasons = verify.blockers({
        "ok": False,
        "test": {"ok": True},
        "lint": [{"path": "core/scrapers/epic_property_management.py", "line": 470,
                  "code": "F841",
                  "detail": "Local variable `property_filter` is assigned to but never used"}],
    })

    assert any("property_filter" in r for r in reasons)
    assert not any("test failed" in r for r in reasons), "the test passed; name what did stop it"


def test_clean_code_leaves_the_build_alone(tmp_path, monkeypatch):
    path = _write(tmp_path, monkeypatch, "def f(opts):\n    return {'Basis': opts['basis']}\n")

    v = codegen.fold_lint({"ok": True}, [("write_file", {"path": path})])

    assert v["ok"] is True and "lint" not in v


def test_a_file_the_agent_never_actually_wrote_is_left_to_the_other_gates(tmp_path, monkeypatch):
    """Ruff answers "no such file" for a path that isn't there, which explains
    nothing. Whether the agent wrote what it claimed is the no-changes and
    untested-code gates' question, and they say it in words."""
    monkeypatch.setattr(verify, "REPO_ROOT", tmp_path)

    assert verify.lint(["core/parsers/never_written.py"]) == []
