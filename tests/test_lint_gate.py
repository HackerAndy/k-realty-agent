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


def test_an_unused_import_is_reported_but_does_not_fail_a_build(tmp_path, monkeypatch):
    """It used to fail one, and was the most common refusal on the codegen bench —
    including the sole complaint against a parser the bench scored CORRECT.

    Note it is still REPORTED. The first attempt at this deleted the rule, which
    also deleted the finding: nobody was told again, ever. Advisory means "not
    worth a rebuild", not "not worth knowing".
    """
    path = _write(tmp_path, monkeypatch, "from datetime import UTC, date\n\nx = date.today()\n")

    findings = verify.lint([path])

    assert any(f["code"] == "F401" for f in findings), "still seen"
    assert verify.blocking(findings) == [], "just not fatal"
    assert "tidiness" in next(f for f in findings if f["code"] == "F401")["advice"]


def test_a_value_computed_and_discarded_is_still_caught(tmp_path, monkeypatch):
    """The rule the gate actually exists for — dropping F401 must not drop this.

    A setting read and then not used is worse than one hardcoded, because the
    screen offers a choice that silently does nothing.
    """
    path = _write(tmp_path, monkeypatch,
                  'def retrieve(opts):\n'
                  '    lookback = opts["lookback_days"]\n'
                  '    return {"range": "fixed"}\n')

    assert any(f["code"] == "F841" for f in verify.blocking(verify.lint([path])))


def test_a_name_that_does_not_exist_is_caught(tmp_path, monkeypatch):
    """The one that would crash at run time rather than merely do nothing."""
    path = _write(tmp_path, monkeypatch, "def go():\n    return undefined_helper()\n")

    assert any(f["code"] == "F821" for f in verify.blocking(verify.lint([path])))


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


# --- classification, not filtering ------------------------------------------
#
# The gate used to have one knob: a rule was selected or it did not exist. So
# the only way to stop it refusing correct work over a triviality was to delete
# the rule — and that loses the finding for everyone, forever. Twice in one
# session that was nearly the answer. Findings are classified instead.

def test_an_unused_exception_binding_does_not_fail_a_build(tmp_path, monkeypatch):
    """F841 covers two different things; only one of them is this gate's business."""
    path = _write(tmp_path, monkeypatch,
                  "def go():\n"
                  "    try:\n"
                  "        pass\n"
                  "    except ValueError as e:\n"
                  "        raise RuntimeError('boom')\n")

    findings = verify.lint([path])

    assert any(f["code"] == "F841" for f in findings), "still reported"
    assert verify.blocking(findings) == []
    assert "naming choice" in findings[0]["advice"]


def test_a_discarded_value_in_the_same_file_still_blocks(tmp_path, monkeypatch):
    """The two F841s must not be confused: one is the bug the gate exists for."""
    path = _write(tmp_path, monkeypatch,
                  "def go(opts):\n"
                  "    try:\n"
                  "        pass\n"
                  "    except ValueError as e:\n"
                  "        raise RuntimeError('boom')\n"
                  "    lookback = opts['lookback_days']\n"
                  "    return {'range': 'fixed'}\n")

    blocking = verify.blocking(verify.lint([path]))

    assert len(blocking) == 1
    assert "lookback" in blocking[0]["detail"]


def test_an_unclassified_rule_blocks_by_default(tmp_path, monkeypatch):
    """The list fails CLOSED. A rule nobody has ruled on stops a build rather
    than slipping through because it was never considered."""
    path = _write(tmp_path, monkeypatch, "x = 1 is 1\n")  # F632

    blocking = verify.blocking(verify.lint([path]))

    assert blocking and blocking[0]["code"] not in verify.ADVISORY_RULES


def test_advisory_findings_are_recorded_on_the_verification(tmp_path, monkeypatch):
    """Kept where the operator can reach them, rather than vanishing."""
    path = _write(tmp_path, monkeypatch, "import os\n\nx = 1\n")

    v = codegen.fold_lint({"ok": True}, [("write_file", {"path": path})])

    assert v["ok"] is True, "tidiness does not fail a build"
    assert "lint" not in v, "and is not offered as a reason it failed"
    assert any(f["code"] == "F401" for f in v["lint_advisory"])


def test_the_operator_is_shown_what_was_noticed_but_not_fatal():
    """An advisory finding nobody renders is the same as one nobody recorded."""
    v = {"ok": True, "lint_advisory": [
        {"path": "core/parsers/x.py", "line": 3, "code": "F401",
         "detail": "`os` imported but unused", "advice": "tidiness, not a disconnected wire"}]}

    said = verify.notes(v)

    assert said and "core/parsers/x.py line 3" in said[0]
    assert "tidiness" in said[0]
    assert verify.blockers(v) == [], "a note is not a refusal"
