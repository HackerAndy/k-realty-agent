"""Patch-shaped editing: the matching rules, and the gates that depend on it.

The editing rules are strict on purpose — an edit that lands in the wrong place
is worse than one that doesn't happen, because nothing downstream can tell it
was unintended. So every refusal here is also checked for saying what to do
next: a model that gets "edit failed" retries by flailing, and on a small local
model that is most of a run.

The last section is the one that would hurt silently. Every gate decides what
the agent changed by reading its tool calls back, and it used to look for
`write_file` by name. A new write tool missing from `WRITE_TOOLS` is invisible
to all of them at once — no test required, no coverage checked, no lint — and
the run scores as a no-op that touched nothing.
"""

from __future__ import annotations

import pytest

from orchestration import agent_tools, codegen
from orchestration.file_editor import EditError, insert_at, replace_once, snippet

SAMPLE = """\
def alpha():
    value = 1
    return value


def beta():
    value = 2
    return value
"""


# --- matching ---------------------------------------------------------------

def test_a_unique_block_is_replaced_in_place():
    edit = replace_once(SAMPLE, "def alpha():\n    value = 1", "def alpha():\n    value = 99")

    assert "value = 99" in edit.content
    assert "def beta():\n    value = 2" in edit.content, "the rest of the file must survive"
    assert edit.line == 1


def test_a_substring_is_enough_when_it_is_unique():
    """Matching is on text, not whole lines — the indent need not be included."""
    edit = replace_once(SAMPLE, "value = 1", "value = 99")
    assert "    value = 99" in edit.content, "the surrounding indent is left alone"


def test_whitespace_inside_the_match_must_be_exact():
    """Only the OUTER whitespace gets a second chance; this is a real mismatch."""
    with pytest.raises(EditError, match="did not appear"):
        replace_once(SAMPLE, "value  = 1", "value = 99")


def test_text_that_is_not_there_is_refused_with_advice():
    with pytest.raises(EditError) as excinfo:
        replace_once(SAMPLE, "def gamma():", "    pass")

    message = str(excinfo.value)
    assert "did not appear" in message
    assert "read_file" in message, "tell the model where to get an exact copy"


def test_an_ambiguous_block_is_refused_and_names_the_lines():
    with pytest.raises(EditError) as excinfo:
        replace_once(SAMPLE, "    return value", "    return None")

    message = str(excinfo.value)
    assert "2 times" in message
    assert "3, 8" in message, "the line numbers are how the model disambiguates"
    assert "surrounding lines" in message


def test_ambiguity_changes_nothing():
    """The dangerous case: a partial edit is worse than a refused one."""
    with pytest.raises(EditError):
        replace_once(SAMPLE, "    return value", "    return None")
    assert SAMPLE.count("return value") == 2


def test_stray_surrounding_whitespace_still_matches():
    """A block copied out of numbered output often brings a newline with it."""
    edit = replace_once(SAMPLE, "\n    value = 1\n", "    value = 99")
    assert "value = 99" in edit.content


def test_the_replacement_text_is_never_stripped():
    """Stripping new_str would silently eat indentation the caller asked for."""
    edit = replace_once(SAMPLE, "    value = 1", "        value = 1  # deeper")
    assert "        value = 1  # deeper" in edit.content


def test_replacing_text_with_itself_is_refused():
    with pytest.raises(EditError, match="identical"):
        replace_once(SAMPLE, "    value = 1", "    value = 1")


def test_an_empty_old_str_is_refused():
    with pytest.raises(EditError, match="empty"):
        replace_once(SAMPLE, "", "anything")


def test_deleting_a_block_is_an_empty_replacement():
    edit = replace_once(SAMPLE, "def alpha():\n    value = 1\n    return value\n\n\n", "")
    assert "alpha" not in edit.content
    assert "def beta():" in edit.content


# --- insertion --------------------------------------------------------------

def test_insert_after_a_line():
    edit = insert_at(SAMPLE, 1, "    # first")
    assert edit.content.splitlines()[1] == "    # first"
    assert edit.line == 2


def test_insert_at_zero_goes_to_the_top():
    edit = insert_at(SAMPLE, 0, "import os")
    assert edit.content.startswith("import os\n")


def test_insert_past_the_end_is_refused():
    with pytest.raises(EditError, match="outside the file"):
        insert_at(SAMPLE, 999, "x = 1")


def test_insert_at_the_end_of_a_file_with_no_trailing_newline():
    """Otherwise the insertion is welded onto the last line."""
    edit = insert_at("a = 1\nb = 2", 2, "c = 3")
    assert edit.content == "a = 1\nb = 2\nc = 3\n"


# --- the echo ---------------------------------------------------------------

def test_the_snippet_is_numbered_and_local():
    text = snippet(SAMPLE, line=6, span=1, context=1)

    assert "6\tdef beta():" in text
    assert "def alpha():" not in text, "a snippet that shows the file defeats the point"


# --- the tools --------------------------------------------------------------

@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_tools, "REPO_ROOT", tmp_path)
    agent_tools.forget_originals()
    (tmp_path / "core").mkdir(parents=True, exist_ok=True)
    (tmp_path / "core" / "thing.py").write_text(SAMPLE)
    return tmp_path


def test_write_file_creates_a_new_file(repo):
    agent_tools.write_file("core/new.py", "x = 1\n")
    assert (repo / "core" / "new.py").read_text() == "x = 1\n"


def test_write_file_refuses_to_overwrite_and_points_at_str_replace(repo):
    with pytest.raises(agent_tools.ToolError) as excinfo:
        agent_tools.write_file("core/thing.py", "gutted")

    assert "str_replace" in str(excinfo.value)
    assert (repo / "core" / "thing.py").read_text() == SAMPLE, "the file must be untouched"


def test_str_replace_edits_and_shows_the_result(repo):
    out = agent_tools.str_replace("core/thing.py", "    value = 1", "    value = 99")

    assert (repo / "core" / "thing.py").read_text().count("value = 99") == 1
    assert "line 2" in out
    assert "value = 99" in out, "the echo is what saves a re-read"


def test_a_refused_edit_leaves_the_file_alone(repo):
    with pytest.raises(agent_tools.ToolError):
        agent_tools.str_replace("core/thing.py", "    return value", "    return None")
    assert (repo / "core" / "thing.py").read_text() == SAMPLE


def test_editing_a_file_that_does_not_exist_says_to_create_it(repo):
    with pytest.raises(agent_tools.ToolError, match="write_file"):
        agent_tools.str_replace("core/absent.py", "a", "b")


def test_the_baseline_is_what_the_run_started_from(repo):
    """Later edits are the agent iterating; the 'before' is the run's start."""
    agent_tools.str_replace("core/thing.py", "    value = 1", "    value = 50")
    agent_tools.str_replace("core/thing.py", "    value = 50", "    value = 99")

    assert agent_tools.originals()["core/thing.py"] == SAMPLE


def test_insert_also_records_the_baseline(repo):
    agent_tools.insert("core/thing.py", 0, "import os")
    assert agent_tools.originals()["core/thing.py"] == SAMPLE


def test_a_created_file_has_no_baseline(repo):
    agent_tools.write_file("core/brand_new.py", "x = 1\n")
    assert agent_tools.originals() == {}


def test_edits_outside_the_repo_are_still_refused(repo):
    with pytest.raises(agent_tools.ToolError, match="outside the repository"):
        agent_tools.str_replace("../escape.py", "a", "b")


# --- the gates still see the change -----------------------------------------

def test_every_write_tool_is_dispatchable_and_declared():
    schema_names = {schema["name"] for schema in agent_tools.TOOL_SCHEMAS}

    assert agent_tools.WRITE_TOOLS <= schema_names
    assert agent_tools.WRITE_TOOLS <= set(agent_tools._DISPATCH)


def test_the_schemas_and_the_dispatch_table_agree():
    """A tool advertised but not wired up fails at the moment the agent calls it."""
    schema_names = {schema["name"] for schema in agent_tools.TOOL_SCHEMAS}
    assert schema_names == set(agent_tools._DISPATCH)


@pytest.mark.parametrize("tool", sorted(agent_tools.WRITE_TOOLS))
def test_a_change_made_by_any_write_tool_counts_as_a_change(tool):
    calls = [(tool, {"path": "core/parsers/x.py"})]
    assert codegen.files_written(calls) == ["core/parsers/x.py"]
    assert codegen.fold_noop({"ok": True}, calls).get("no_changes") is None


@pytest.mark.parametrize("tool", sorted(agent_tools.WRITE_TOOLS))
def test_code_changed_by_any_write_tool_still_needs_a_test(tool):
    calls = [(tool, {"path": "core/parsers/x.py"})]
    assert codegen.untested_code_files(calls) == ["core/parsers/x.py"]


def test_str_replace_is_what_the_prompts_tell_the_agent_to_use():
    """The reviser prompts name it; a rename here would leave them lying."""
    from pathlib import Path

    prompts = Path(__file__).resolve().parent.parent / "core" / "prompts"
    for name in ("parser_reviser.v1.md", "scraper_reviser.v1.md"):
        assert "str_replace" in (prompts / name).read_text()
