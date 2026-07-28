"""Operator-adjustable scrape options, kept out of the code.

Every portal makes you choose something before it hands over data — a date range,
which properties, an accounting basis. The demonstration captures ONE set of those
choices and the agent bakes them into the scraper, after which changing "last 30
days" to "last 90" costs a code change, a test run and an approval.

The property that keeps the design honest is the last group here: the harness
renders whatever a source declares and knows none of the field names itself.
"""

import pytest

from core import settings
from tests.test_codegen_gate import _Result, _wrote


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Never touch the operator's real core/policies/source_settings.yaml."""
    monkeypatch.setattr(settings, "SETTINGS_PATH", tmp_path / "source_settings.yaml")
    return tmp_path


SCHEMA = [
    {"key": "lookback_days", "label": "How far back", "type": "number",
     "default": 30, "min": 1, "max": 365},
    {"key": "basis", "label": "Accounting basis", "type": "choice", "default": "accrual",
     "options": [{"value": "accrual", "label": "Accrual"}, {"value": "cash", "label": "Cash"}]},
    {"key": "include_zero", "label": "Include zero-value rows", "type": "boolean", "default": False},
    {"key": "note", "label": "Note", "type": "text", "default": ""},
]


@pytest.fixture
def declared(monkeypatch):
    monkeypatch.setattr(settings, "schema_for", lambda key: [dict(f) for f in SCHEMA])


# --- reading -----------------------------------------------------------------

def test_defaults_apply_when_nothing_is_stored(isolated, declared):
    assert settings.values_for("epic") == {
        "lookback_days": 30, "basis": "accrual", "include_zero": False, "note": "",
    }


def test_overrides_sit_on_top_of_defaults(isolated, declared):
    settings.save_for("epic", {"lookback_days": 90})
    values = settings.values_for("epic")

    assert values["lookback_days"] == 90
    assert values["basis"] == "accrual", "untouched options keep their declared default"


def test_stored_shows_only_what_the_operator_changed(isolated, declared):
    settings.save_for("epic", {"lookback_days": 90})
    assert settings.stored_for("epic") == {"lookback_days": 90}


def test_a_source_with_no_declared_options_is_not_an_error(isolated, monkeypatch):
    """The normal case for a plain document parser: no options screen."""
    monkeypatch.setattr(settings, "schema_for", lambda key: [])
    assert settings.values_for("dfcu") == {}


# --- validation --------------------------------------------------------------

def test_numbers_are_coerced_from_form_strings(isolated, declared):
    """Everything arrives from a web form as text."""
    settings.save_for("epic", {"lookback_days": "90"})
    assert settings.values_for("epic")["lookback_days"] == 90


def test_a_number_outside_its_range_is_refused(isolated, declared):
    with pytest.raises(settings.SettingsError, match="at most 365"):
        settings.save_for("epic", {"lookback_days": 5000})
    with pytest.raises(settings.SettingsError, match="at least 1"):
        settings.save_for("epic", {"lookback_days": 0})


def test_a_choice_outside_its_options_is_refused(isolated, declared):
    with pytest.raises(settings.SettingsError, match="must be one of"):
        settings.save_for("epic", {"basis": "vibes"})


def test_booleans_accept_what_a_checkbox_actually_sends(isolated, declared):
    for raw, expected in [("on", True), ("true", True), (True, True),
                          ("false", False), ("", False), (False, False)]:
        settings.save_for("epic", {"include_zero": raw})
        assert settings.values_for("epic")["include_zero"] is expected, raw


def test_an_unknown_key_is_refused_rather_than_quietly_stored(isolated, declared):
    """A typo sitting in config doing nothing is worse than an error, because the
    operator believes they changed something."""
    with pytest.raises(settings.SettingsError, match="Not an option"):
        settings.save_for("epic", {"lookbackdays": 90})


def test_nonsense_for_a_number_says_which_field(isolated, declared):
    with pytest.raises(settings.SettingsError, match="How far back"):
        settings.save_for("epic", {"lookback_days": "soon"})


def test_saving_against_a_source_with_no_options_is_refused(isolated, monkeypatch):
    monkeypatch.setattr(settings, "schema_for", lambda key: [])
    with pytest.raises(settings.SettingsError, match="no adjustable options"):
        settings.save_for("dfcu", {"anything": 1})


# --- persistence -------------------------------------------------------------

def test_values_survive_a_reload(isolated, declared):
    settings.save_for("epic", {"lookback_days": 90, "basis": "cash"})
    assert settings.values_for("epic")["lookback_days"] == 90
    assert settings.values_for("epic")["basis"] == "cash"


def test_saving_one_source_leaves_another_alone(isolated, declared):
    settings.save_for("epic", {"lookback_days": 90})
    settings.save_for("dfcu", {"lookback_days": 7})
    assert settings.values_for("epic")["lookback_days"] == 90
    assert settings.values_for("dfcu")["lookback_days"] == 7


def test_a_partial_save_merges_rather_than_wipes(isolated, declared):
    settings.save_for("epic", {"lookback_days": 90, "basis": "cash"})
    settings.save_for("epic", {"lookback_days": 60})
    assert settings.values_for("epic") == {
        "lookback_days": 60, "basis": "cash", "include_zero": False, "note": "",
    }


def test_reset_returns_to_the_declared_defaults(isolated, declared):
    settings.save_for("epic", {"lookback_days": 90})
    assert settings.reset_for("epic")["lookback_days"] == 30
    assert settings.stored_for("epic") == {}


def test_the_written_file_explains_itself(isolated, declared):
    settings.save_for("epic", {"lookback_days": 90})
    text = settings.SETTINGS_PATH.read_text()
    assert text.startswith("#"), "a config file an operator may open deserves a header"
    assert "declared by each source" in text


# --- the harness stays domain-blind ------------------------------------------

def test_the_schema_comes_from_the_SOURCE_not_the_harness(isolated, monkeypatch):
    """The whole point: nothing in core/ or the UI names a portal's fields. A
    source declares them and the harness renders whatever it finds."""
    import types
    fake = types.SimpleNamespace(SETTINGS=[
        {"key": "whatever_this_portal_needs", "label": "Whatever", "type": "text", "default": "x"},
    ])
    monkeypatch.setattr(settings, "_import", lambda name: fake if "scrapers" in name else None)

    schema = settings.schema_for("some_source")
    assert [f["key"] for f in schema] == ["whatever_this_portal_needs"]
    assert settings.values_for("some_source") == {"whatever_this_portal_needs": "x"}


def test_no_declaration_anywhere_means_no_options(isolated, monkeypatch):
    monkeypatch.setattr(settings, "_import", lambda name: None)
    assert settings.schema_for("mystery") == []


# --- the tool surface --------------------------------------------------------

def test_tools_report_schema_values_and_what_was_overridden(isolated, declared):
    from interfaces import mcp_tools

    mcp_tools.save_source_settings("epic", {"lookback_days": 90})
    out = mcp_tools.source_settings("epic")

    assert [f["key"] for f in out["schema"]] == [f["key"] for f in SCHEMA]
    assert out["values"]["lookback_days"] == 90
    assert out["overridden"] == ["lookback_days"], "shows what differs from the defaults"


def test_a_bad_value_reaches_the_operator_as_a_tool_error(isolated, declared):
    from interfaces import mcp_tools

    with pytest.raises(mcp_tools.ToolError, match="at most 365"):
        mcp_tools.save_source_settings("epic", {"lookback_days": 99999})


# --- enforcement: no hardcoding, as infrastructure ---------------------------

class TestNoHardcodedOptions:
    """The operator's rule: this is an infrastructure requirement, not advice.

    The scraper-builder prompt already asked for settings to be declared. Epic's
    scraper was written under that instruction and still froze a 30-day window,
    an accounting basis, a property selection and two more — which is the
    difference between asking and enforcing.
    """

    def _scan(self, tmp_path, monkeypatch, source: str):
        from orchestration import verify
        monkeypatch.setattr(verify, "REPO_ROOT", tmp_path)
        (tmp_path / "core" / "scrapers").mkdir(parents=True, exist_ok=True)
        path = "core/scrapers/probe.py"
        (tmp_path / path).write_text(source)
        return verify.hardcoded_options(path)

    def test_a_frozen_time_window_is_caught(self, tmp_path, monkeypatch):
        found = self._scan(tmp_path, monkeypatch,
                           "from datetime import timedelta\nw = timedelta(days=30)\n")
        assert [f["kind"] for f in found] == ["time_window"]
        assert "days=30" in found[0]["detail"]

    def test_frozen_filter_selections_are_caught(self, tmp_path, monkeypatch):
        found = self._scan(tmp_path, monkeypatch, '''
body = {
    "PropertySelectionType": "AllProperties",
    "AccountingBasis": 1,
    "IncludeFundType": True,
    "GlAccountIds": account_ids,
}
''')
        details = {f["detail"] for f in found}
        assert "PropertySelectionType='AllProperties'" in details
        assert "AccountingBasis=1" in details
        assert not any("GlAccountIds" in d for d in details), "a computed value is not hardcoded"

    def test_an_extract_mapping_is_not_flagged(self, tmp_path, monkeypatch):
        """The false positive that would make the gate unusable: every scraper
        builds a fields dict, and its values are expressions, not literals."""
        found = self._scan(tmp_path, monkeypatch, '''
def _extract(rows):
    return [{
        "Id": str(r.get("Id", "")),
        "Date": str(r.get("Date", "")),
        "Amount": str(r.get("Amount", "")),
        "Name": str(r.get("Name", "")),
    } for r in rows]
''')
        assert found == []

    def test_a_fixed_line_can_be_exempted_with_a_stated_reason(self, tmp_path, monkeypatch):
        """Some values really are protocol. The escape hatch is per line and has
        to say something, so the justification is visible in review."""
        found = self._scan(tmp_path, monkeypatch, '''
body = {
    "PropertySelectionType": "AllProperties",  # fixed: the API rejects any other value
    "AccountingBasis": 1,
    "IncludeFundType": True,
}
''')
        assert all("PropertySelectionType" not in f["detail"] for f in found)
        assert any("AccountingBasis" in f["detail"] for f in found), "only the marked line is exempt"

    def test_declaring_settings_without_reading_them_does_not_count(self, tmp_path, monkeypatch):
        from orchestration import verify
        monkeypatch.setattr(verify, "REPO_ROOT", tmp_path)
        (tmp_path / "core" / "scrapers").mkdir(parents=True, exist_ok=True)

        (tmp_path / "core/scrapers/a.py").write_text(
            'SETTINGS = [{"key": "lookback_days", "default": 30}]\n')
        assert verify.declares_settings("core/scrapers/a.py") is False, "declared but never read"

        (tmp_path / "core/scrapers/b.py").write_text(
            'from core import settings\n'
            'SETTINGS = [{"key": "lookback_days", "default": 30}]\n'
            'def retrieve():\n    return settings.values_for("x")\n')
        assert verify.declares_settings("core/scrapers/b.py") is True

    def test_the_build_gate_fails_and_names_the_lines(self, monkeypatch):
        """It has to be actionable: which file, which line, which value."""
        from orchestration import codegen

        monkeypatch.setattr(codegen, "hardcoded_options",
                            lambda p: [{"line": 206, "kind": "time_window",
                                        "detail": "timedelta(days=30)"}])
        v = codegen.fold_hardcoded({"ok": True},
                                   [("write_file", {"path": "core/scrapers/epic.py"})])

        assert v["ok"] is False
        assert v["hardcoded_options"]["core/scrapers/epic.py"][0]["line"] == 206

    def test_the_agent_is_told_how_to_fix_it_and_retried(self, monkeypatch):
        from orchestration import codegen

        seen = {"tasks": []}
        results = iter([
            _Result(_wrote("core/scrapers/epic.py", "tests/test_epic.py")),
            _Result(_wrote("core/scrapers/epic.py", "tests/test_epic.py")),
        ])

        def fake_run_agent(task, system, on_event=print, **kw):
            seen["tasks"].append(task)
            return next(results)

        scans = iter([[{"line": 206, "kind": "time_window", "detail": "timedelta(days=30)"}], []])
        monkeypatch.setattr(codegen, "run_agent", fake_run_agent)
        monkeypatch.setattr(codegen, "hardcoded_options", lambda p: list(next(scans)))

        _, v = codegen.run_codegen_gated(
            "build it", "SYSTEM", lambda: {"ok": True}, on_event=lambda m: None)

        assert len(seen["tasks"]) == 2, "hardcoded options must earn a retry"
        assert "SETTINGS" in seen["tasks"][1] and "values_for" in seen["tasks"][1]
        assert "# fixed:" in seen["tasks"][1], "the escape hatch must be offered too"
        assert v["ok"] is True
