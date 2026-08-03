"""The code-gen gate: what counts as success, and who has to notice a failure.

Both behaviours here come from the same field session:

  1. Asked to add missing test coverage, the agent wrote NO files, re-ran the
     existing suite, and the harness reported ok — because the untested-code net
     only fires when code IS written. A no-op scored a perfect green, which is
     worse than a failure because it ends the conversation.
  2. The agent skipped writing a test at all, and the operator had to notice and
     ask "why didn't you test that?". That is the harness's job, not theirs.
"""

import pytest

from orchestration import codegen


class _Result:
    def __init__(self, tool_calls, text="done"):
        self.tool_calls = tool_calls
        self.final_text = text
        self.turns = 1


def _wrote(*paths):
    return [("write_file", {"path": p}) for p in paths]


# --- what counts as a change ------------------------------------------------

def test_fold_noop_rejects_a_run_that_wrote_nothing():
    v = codegen.fold_noop({"ok": True, "test": {"ok": True}}, [])
    assert v["ok"] is False and v["no_changes"] is True


def test_fold_noop_accepts_a_run_that_wrote_something():
    v = codegen.fold_noop({"ok": True}, _wrote("core/scrapers/x.py"))
    assert v["ok"] is True and "no_changes" not in v


def test_reading_files_is_not_changing_them():
    v = codegen.fold_noop({"ok": True}, [("read_file", {"path": "core/scrapers/x.py"}),
                                         ("run_command", {"command": "pytest"})])
    assert v["ok"] is False


# --- the escape hatch: already correct --------------------------------------
#
# Without it the gate deadlocks. A bug fixed on run 1 means run 2 has nothing to
# write, so run 2 is refused as a no-op — and so is every run after it. The
# operator's screen said "nothing changed" with no move left that could change it.

def _declared(reason="the status= kwarg was already removed at line 197; 16 tests cover it"):
    return [("read_file", {"path": "core/scrapers/x.py"}),
            ("no_change_needed", {"reason": reason})]


def test_a_declared_no_change_is_allowed_through():
    v = codegen.fold_noop({"ok": True, "test": {"ok": True}}, _declared())
    assert v["ok"] is True and "no_changes" not in v


def test_the_reason_is_kept_for_the_operator_to_read():
    v = codegen.fold_noop({"ok": True}, _declared("already fixed at line 197 — see the test"))
    assert v["no_change_reason"] == "already fixed at line 197 — see the test"


def test_declaring_it_cannot_rescue_a_failing_test():
    """The hatch excuses writing nothing. It does not excuse broken code."""
    v = codegen.fold_noop({"ok": False, "test": {"ok": False}}, _declared())
    assert v["ok"] is False


def test_an_empty_reason_is_not_a_declaration():
    v = codegen.fold_noop({"ok": True}, [("no_change_needed", {"reason": "   "})])
    assert v["ok"] is False and v["no_changes"] is True


def test_a_declaration_alongside_real_edits_still_counts_the_edits():
    v = codegen.fold_noop({"ok": True}, _declared() + _wrote("core/scrapers/x.py"))
    assert v["ok"] is True and "no_change_reason" not in v


def test_a_declared_no_change_is_not_retried(monkeypatch):
    seen = _runner(monkeypatch, [_Result(_declared())])
    _, v = codegen.run_codegen_gated(
        "task", "sys", lambda: {"ok": True, "test": {"ok": True}}, on_event=lambda _: None,
        require_changes=True)
    assert len(seen["tasks"]) == 1 and v["ok"] is True


# --- the auto-retry ---------------------------------------------------------

def _runner(monkeypatch, sequence):
    """Feed run_agent a scripted sequence of results; record the tasks it got."""
    seen = {"tasks": []}
    calls = iter(sequence)

    def fake_run_agent(task, system, on_event=print, **kw):
        seen["tasks"].append(task)
        return next(calls)

    monkeypatch.setattr(codegen, "run_agent", fake_run_agent)
    return seen


def test_missing_test_triggers_one_automatic_retry(monkeypatch):
    """The operator should never be the one to say 'you didn't test that'."""
    seen = _runner(monkeypatch, [
        _Result(_wrote("core/scrapers/epic.py")),                      # no test
        _Result(_wrote("core/scrapers/epic.py", "tests/test_epic.py")),  # fixed
    ])
    verifications = iter([{"ok": True}, {"ok": True}])

    result, v = codegen.run_codegen_gated(
        "fix the scraper", "SYSTEM", lambda: dict(next(verifications)), on_event=lambda m: None
    )

    assert len(seen["tasks"]) == 2, "should have retried exactly once"
    assert "wrote no test" in seen["tasks"][1]
    assert "Original task" in seen["tasks"][1], "retry must keep the original context"
    assert v["ok"] is True and "untested_code" not in v


def test_a_noop_revise_triggers_a_retry(monkeypatch):
    seen = _runner(monkeypatch, [
        _Result([]),                               # today's failure: changed nothing
        _Result(_wrote("core/scrapers/epic.py", "tests/test_epic.py")),
    ])
    verifications = iter([{"ok": True}, {"ok": True}])

    _, v = codegen.run_codegen_gated(
        "fix it", "SYSTEM", lambda: dict(next(verifications)),
        on_event=lambda m: None, require_changes=True,
    )

    assert len(seen["tasks"]) == 2
    assert "without writing any file" in seen["tasks"][1]
    assert v["ok"] is True


def test_a_failing_test_is_NOT_retried(monkeypatch):
    """A red test is a real engineering problem for the operator to see and
    direct — not a rule the agent forgot. Retrying would just burn tokens and
    hide it."""
    seen = _runner(monkeypatch, [_Result(_wrote("core/parsers/x.py", "tests/test_x.py"))])

    _, v = codegen.run_codegen_gated(
        "build it", "SYSTEM", lambda: {"ok": False, "test": {"ok": False, "output": "1 failed"}},
        on_event=lambda m: None,
    )

    assert len(seen["tasks"]) == 1, "must not retry a genuine test failure"
    assert v["ok"] is False


def test_the_retry_happens_at_most_once(monkeypatch):
    """A stubborn agent must not loop forever against the operator's LLM."""
    seen = _runner(monkeypatch, [_Result([]), _Result([]), _Result([])])
    verifications = iter([{"ok": True}] * 3)

    _, v = codegen.run_codegen_gated(
        "fix it", "SYSTEM", lambda: dict(next(verifications)),
        on_event=lambda m: None, require_changes=True,
    )

    assert len(seen["tasks"]) == 2, "one attempt + one retry, then report honestly"
    assert v["ok"] is False and v["no_changes"] is True


def test_a_clean_first_run_is_not_retried(monkeypatch):
    seen = _runner(monkeypatch, [_Result(_wrote("core/parsers/x.py", "tests/test_x.py"))])

    _, v = codegen.run_codegen_gated(
        "build it", "SYSTEM", lambda: {"ok": True}, on_event=lambda m: None
    )

    assert len(seen["tasks"]) == 1 and v["ok"] is True


def test_the_retry_is_announced_to_the_operator(monkeypatch):
    """It spends their LLM, so it must be visible in the event stream."""
    _runner(monkeypatch, [_Result([]), _Result(_wrote("core/x.py", "tests/test_x.py"))])
    verifications = iter([{"ok": True}, {"ok": True}])
    events = []

    codegen.run_codegen_gated(
        "fix it", "SYSTEM", lambda: dict(next(verifications)),
        on_event=events.append, require_changes=True,
    )

    assert any("harness rejected that run" in e for e in events)
    assert any("Retrying once" in e for e in events)


def test_build_mode_does_not_require_changes(monkeypatch):
    """Only a *fix* is meaningless without a change; a build is judged on its
    verification alone."""
    seen = _runner(monkeypatch, [_Result([])])

    _, v = codegen.run_codegen_gated(
        "build it", "SYSTEM", lambda: {"ok": True}, on_event=lambda m: None
    )

    assert len(seen["tasks"]) == 1
    assert "no_changes" not in v


@pytest.mark.parametrize("calls, expected", [
    ([], []),
    (_wrote("core/a.py"), ["core/a.py"]),
    (_wrote("core/a.py", "tests/test_a.py"), ["core/a.py", "tests/test_a.py"]),
])
def test_files_written(calls, expected):
    assert codegen.files_written(calls) == expected


# --- coverage, not existence -------------------------------------------------

def test_uncovered_changes_fail_the_gate_and_trigger_a_retry(monkeypatch):
    """The hole that started all this: the Epic scraper's two tests passed for the
    entire time it was 403-broken, because they only covered a pure helper the fix
    never touched. Measured on that real commit: 22 changed executable lines, 4
    covered, 18 not."""
    seen = _runner(monkeypatch, [
        _Result(_wrote("core/scrapers/epic.py", "tests/test_epic.py")),  # stale test
        _Result(_wrote("core/scrapers/epic.py", "tests/test_epic.py")),  # extended
    ])
    coverage_results = iter([
        {"ok": False, "checked": True, "uncovered": {"core/scrapers/epic.py": [39, 40, 41]}},
        {"ok": True, "checked": True, "uncovered": {}},
    ])
    monkeypatch.setattr(codegen, "covers_changes", lambda t, c: dict(next(coverage_results)))

    _, v = codegen.run_codegen_gated(
        "fix it", "SYSTEM", lambda: {"ok": True},
        on_event=lambda m: None, test_path="tests/test_epic.py",
    )

    assert len(seen["tasks"]) == 2, "uncovered changes must earn a retry"
    assert "never RUNS the code you changed" in seen["tasks"][1]
    assert "lines [39, 40, 41]" in seen["tasks"][1], "name the lines, not just the file"
    assert v["ok"] is True


def test_covered_changes_pass_cleanly(monkeypatch):
    seen = _runner(monkeypatch, [_Result(_wrote("core/parsers/x.py", "tests/test_x.py"))])
    monkeypatch.setattr(codegen, "covers_changes",
                        lambda t, c: {"ok": True, "checked": True, "uncovered": {}})

    _, v = codegen.run_codegen_gated(
        "build", "SYSTEM", lambda: {"ok": True},
        on_event=lambda m: None, test_path="tests/test_x.py",
    )

    assert len(seen["tasks"]) == 1 and v["ok"] is True


def test_coverage_is_not_second_guessed_when_no_test_was_written_at_all(monkeypatch):
    """Missing test is the clearer complaint; don't stack a confusing second one."""
    _runner(monkeypatch, [_Result(_wrote("core/parsers/x.py"))] * 2)
    called = []
    monkeypatch.setattr(codegen, "covers_changes",
                        lambda t, c: called.append(1) or {"ok": False, "checked": True, "uncovered": {}})

    _, v = codegen.run_codegen_gated(
        "build", "SYSTEM", lambda: {"ok": True},
        on_event=lambda m: None, test_path="tests/test_x.py",
    )

    assert not called, "no point measuring coverage of a test that doesn't exist"
    assert v["untested_code"] == ["core/parsers/x.py"]


def test_an_unmeasurable_coverage_run_does_not_block(monkeypatch):
    """If coverage can't run, fail open — the existence + pass checks still hold.
    A tooling problem must not strand the operator."""
    _runner(monkeypatch, [_Result(_wrote("core/parsers/x.py", "tests/test_x.py"))])
    monkeypatch.setattr(codegen, "covers_changes",
                        lambda t, c: {"ok": True, "checked": False, "detail": "Coverage unavailable"})

    _, v = codegen.run_codegen_gated(
        "build", "SYSTEM", lambda: {"ok": True},
        on_event=lambda m: None, test_path="tests/test_x.py",
    )

    assert v["ok"] is True and "uncovered_changes" not in v


# --- reconciling against the source's own numbers ---------------------------
#
# This was the ONE rule in the scraper prompt with nothing enforcing it, and the
# gap showed up in a single build cycle: the Epic scraper reconciles, the DFCU
# scraper — same instructions, same model — does not, though the bank returns a
# runningBalance on every row. An instruction the harness doesn't check is a
# suggestion, and a model under context pressure drops suggestions first.
#
# It matters more than the other gates, not less. A passing test says the parsing
# is unchanged; a successful run says the portal answered. Neither notices that a
# date window clipped rows or pagination stopped early — the run stays green while
# the numbers are quietly wrong.

from orchestration import verify as _verify


def _scraper(tmp_path, monkeypatch, body):
    monkeypatch.setattr(_verify, "REPO_ROOT", tmp_path)
    path = tmp_path / "core" / "scrapers" / "x.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return "core/scrapers/x.py"


RECONCILING = '''
from core import reconcile

def retrieve():
    rows = fetch()
    reconcile.record("Checking", expected=payload["endingBalance"], actual=sum(r["amount"] for r in rows))
    return rows
'''

SILENT = '''
def retrieve():
    return fetch()
'''

DECLARED = '''
NO_CONTROL_TOTALS = "The endpoint returns bare rows: no total, no balance, no count."

def retrieve():
    return fetch()
'''


def test_a_scraper_that_reconciles_passes(tmp_path, monkeypatch):
    path = _scraper(tmp_path, monkeypatch, RECONCILING)
    assert _verify.reconciles(path)["ok"] is True


def test_a_silent_scraper_fails(tmp_path, monkeypatch):
    path = _scraper(tmp_path, monkeypatch, SILENT)
    result = _verify.reconciles(path)
    assert result["ok"] is False
    assert "NO_CONTROL_TOTALS" in result["detail"]


def test_a_source_with_no_totals_can_say_so(tmp_path, monkeypatch):
    """The escape hatch is a declaration, not a silence — greppable and reviewable."""
    path = _scraper(tmp_path, monkeypatch, DECLARED)
    assert _verify.reconciles(path)["ok"] is True


def test_an_empty_excuse_is_not_accepted(tmp_path, monkeypatch):
    path = _scraper(tmp_path, monkeypatch, 'NO_CONTROL_TOTALS = "none"\n')
    assert _verify.reconciles(path)["ok"] is False


def test_an_unparseable_module_is_not_blamed_for_this(tmp_path, monkeypatch):
    """Some other gate names a syntax error properly; this one must not bury it."""
    path = _scraper(tmp_path, monkeypatch, "def retrieve(:\n")
    assert _verify.reconciles(path)["ok"] is True


def test_the_gate_fails_the_verification(tmp_path, monkeypatch):
    path = _scraper(tmp_path, monkeypatch, SILENT)
    v = codegen.fold_reconciliation({"ok": True, "test": {"ok": True}}, path)
    assert v["ok"] is False and v["unreconciled"]


def test_the_gate_records_what_satisfied_it(tmp_path, monkeypatch):
    path = _scraper(tmp_path, monkeypatch, RECONCILING)
    v = codegen.fold_reconciliation({"ok": True}, path)
    assert v["ok"] is True and "reconcile.record()" in v["reconciliation"]


def test_an_unreconciled_scraper_gets_one_retry(monkeypatch, tmp_path):
    path = _scraper(tmp_path, monkeypatch, SILENT)
    monkeypatch.setattr(codegen, "REPO_ROOT", tmp_path)
    seen = _runner(monkeypatch, [_Result(_wrote(path)), _Result(_wrote(path))])
    codegen.run_codegen_gated(
        "task", "sys", lambda: {"ok": True, "test": {"ok": True}},
        on_event=lambda _: None, reconcile_path=path)
    assert len(seen["tasks"]) == 2
    assert "reconcile.record" in seen["tasks"][1]


def test_the_operator_is_told_in_their_own_words():
    out = _verify.blockers({"ok": False, "unreconciled": "nothing checks the extraction"})
    assert any("complete pull from a partial one" in b for b in out)


# --- a fix must edit, not replace -------------------------------------------
#
# The opposite failure to fold_noop, and it cost more. Asked three times running
# to correct a single undefined name, the agent rewrote a 272-line test file from
# scratch: twice introducing new bugs, and the third time hitting the turn cap
# mid-write and leaving a file that would not parse.
#
# Every other gate looks only at what the NEW file contains, so none of them can
# notice what the old one contained and the new one doesn't. That rewrite silently
# dropped the reconciliation tests — the scraper kept its control-total check while
# nothing was left to prove it.
#
# The threshold is calibrated on the recorded builds: every genuine iterative edit
# the agent has made to an existing file scored 0.84-1.00 line-similarity; the
# rewrite scored 0.08.

TARGETED_BEFORE = "\n".join(f"line {i}" for i in range(60))
TARGETED_AFTER = TARGETED_BEFORE.replace("line 30", "line 30  # fixed")
REPLACED = "\n".join(f"completely different content {i}" for i in range(60))


def _file(tmp_path, monkeypatch, body):
    monkeypatch.setattr(_verify, "REPO_ROOT", tmp_path)
    path = tmp_path / "tests" / "t.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return "tests/t.py"


def test_a_targeted_edit_is_allowed(tmp_path, monkeypatch):
    rel = _file(tmp_path, monkeypatch, TARGETED_BEFORE)
    before = _verify.snapshot_files([rel])
    (tmp_path / rel).write_text(TARGETED_AFTER)
    assert _verify.wholesale_rewrites(before) == {}


def test_a_wholesale_replacement_is_flagged(tmp_path, monkeypatch):
    rel = _file(tmp_path, monkeypatch, TARGETED_BEFORE)
    before = _verify.snapshot_files([rel])
    (tmp_path / rel).write_text(REPLACED)
    assert rel in _verify.wholesale_rewrites(before)


def test_growing_a_file_is_not_a_rewrite(tmp_path, monkeypatch):
    """Adding a test class keeps every existing line — that must stay allowed."""
    rel = _file(tmp_path, monkeypatch, TARGETED_BEFORE)
    before = _verify.snapshot_files([rel])
    (tmp_path / rel).write_text(TARGETED_BEFORE + "\n" + "\n".join(
        f"new test line {i}" for i in range(25)))
    assert _verify.wholesale_rewrites(before) == {}


def test_an_untouched_file_is_not_flagged(tmp_path, monkeypatch):
    rel = _file(tmp_path, monkeypatch, TARGETED_BEFORE)
    before = _verify.snapshot_files([rel])
    assert _verify.wholesale_rewrites(before) == {}


def test_a_file_that_did_not_exist_before_is_never_flagged(tmp_path, monkeypatch):
    """A build writes the first version; there is nothing to be similar to."""
    monkeypatch.setattr(_verify, "REPO_ROOT", tmp_path)
    before = _verify.snapshot_files(["tests/brand_new.py"])
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests" / "brand_new.py").write_text(REPLACED)
    assert _verify.wholesale_rewrites(before) == {}


def test_the_rewrite_gate_fails_the_verification(tmp_path, monkeypatch):
    rel = _file(tmp_path, monkeypatch, TARGETED_BEFORE)
    before = _verify.snapshot_files([rel])
    (tmp_path / rel).write_text(REPLACED)
    v = codegen.fold_rewrite({"ok": True, "test": {"ok": True}}, before)
    assert v["ok"] is False and rel in v["wholesale_rewrite"]


def test_a_rewrite_gets_one_retry_telling_it_to_start_from_disk(monkeypatch, tmp_path):
    rel = _file(tmp_path, monkeypatch, TARGETED_BEFORE)
    monkeypatch.setattr(codegen, "REPO_ROOT", tmp_path)

    def rewrite_it(task, system, on_event=print, **kw):
        (tmp_path / rel).write_text(REPLACED)
        return _Result(_wrote(rel))

    seen = {"tasks": []}
    def fake(task, system, on_event=print, **kw):
        seen["tasks"].append(task)
        return rewrite_it(task, system, on_event, **kw)
    monkeypatch.setattr(codegen, "run_agent", fake)

    codegen.run_codegen_gated(
        "fix one name", "sys", lambda: {"ok": True, "test": {"ok": True}},
        on_event=lambda _: None, require_changes=True, test_path=rel)

    assert len(seen["tasks"]) == 2
    assert "REPLACED a file instead of editing it" in seen["tasks"][1]


def test_a_build_is_never_checked_for_rewrites(monkeypatch, tmp_path):
    """Successive drafts within one build are how the agent works."""
    rel = _file(tmp_path, monkeypatch, TARGETED_BEFORE)
    monkeypatch.setattr(codegen, "REPO_ROOT", tmp_path)

    def fake(task, system, on_event=print, **kw):
        (tmp_path / rel).write_text(REPLACED)
        return _Result(_wrote(rel))
    monkeypatch.setattr(codegen, "run_agent", fake)

    _, v = codegen.run_codegen_gated(
        "build it", "sys", lambda: {"ok": True, "test": {"ok": True}},
        on_event=lambda _: None, test_path=rel)      # no require_changes
    assert "wholesale_rewrite" not in v


def test_the_operator_is_warned_that_work_may_be_missing():
    out = _verify.blockers({"ok": False, "wholesale_rewrite": {"tests/t.py": 0.08}})
    assert any("replaced a file rather than editing it" in b for b in out)
    assert any("may have been dropped" in b for b in out)


# --- deleting tests is the loss the ratio can't see -------------------------
#
# The blind spot in the similarity check above. A revise took a 778-line test
# file down to 455 — it kept enough lines to score well above the 0.4 threshold,
# sailed through, and six tests went with it. Deleting a third of a file is the
# same damage as replacing it, in a shape a ratio cannot notice.
#
# Tests specifically, because their loss is the silent kind: delete a function
# the code needs and something fails at once; delete the test that proves the
# code works and everything stays green. That is exactly how the reconciliation
# coverage vanished while the scraper kept reconciling.

def _suite(n_cases, extra_class=True):
    body = ["import pytest", "", "class TestBasics:"]
    for i in range(n_cases):
        body += [f"    def test_case_{i}(self):", f"        assert {i} == {i}", ""]
    if extra_class:
        body += ["class TestDateFiltering:", "    def test_filter(self):",
                 "        assert True", ""]
    return "\n".join(body)


def _suite_file(tmp_path, monkeypatch, body):
    monkeypatch.setattr(_verify, "REPO_ROOT", tmp_path)
    path = tmp_path / "tests" / "test_thing.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return "tests/test_thing.py"


def _replace_contents(rel, body):
    """Swap a whole file's contents through the agent's own editing tool.

    `write_file` cannot overwrite any more, so an agent that replaces a file
    does it as one enormous str_replace — which is exactly what these gates have
    to keep catching. Going through the tool rather than writing the path
    directly is the point: it is what exercises the `_ORIGINALS` capture.
    """
    current = (codegen.agent_tools.REPO_ROOT / rel).read_text()
    return codegen.agent_tools.str_replace(rel, current, body)


def test_a_deleted_test_is_caught(tmp_path, monkeypatch):
    rel = _suite_file(tmp_path, monkeypatch, _suite(27))
    before = _verify.snapshot_files([rel])
    (tmp_path / rel).write_text(_suite(21, extra_class=False))
    assert "test_case_21" in _verify.removed_tests(before)[rel]


def test_a_deleted_test_CLASS_is_caught(tmp_path, monkeypatch):
    """The class AND the methods it took with it — both are gone, both are named,
    because "TestDateFiltering" alone doesn't tell you what stopped being checked."""
    rel = _suite_file(tmp_path, monkeypatch, _suite(27))
    before = _verify.snapshot_files([rel])
    (tmp_path / rel).write_text(_suite(27, extra_class=False))
    assert _verify.removed_tests(before)[rel] == ["TestDateFiltering", "test_filter"]


def test_the_similarity_gate_alone_would_have_missed_it(tmp_path, monkeypatch):
    """Pins WHY this check exists — a shrink that stays similar enough to pass."""
    rel = _suite_file(tmp_path, monkeypatch, _suite(27))
    before = _verify.snapshot_files([rel])
    (tmp_path / rel).write_text(_suite(21, extra_class=False))
    assert _verify.wholesale_rewrites(before) == {}       # blind
    assert _verify.removed_tests(before)                   # not blind


def test_adding_tests_is_allowed(tmp_path, monkeypatch):
    rel = _suite_file(tmp_path, monkeypatch, _suite(27))
    before = _verify.snapshot_files([rel])
    (tmp_path / rel).write_text(_suite(28))
    assert _verify.removed_tests(before) == {}


def test_editing_a_test_body_is_allowed(tmp_path, monkeypatch):
    """Fixing a wrong assertion must not read as deleting the test."""
    rel = _suite_file(tmp_path, monkeypatch, _suite(27))
    before = _verify.snapshot_files([rel])
    (tmp_path / rel).write_text(_suite(27).replace("assert 3 == 3", "assert 3 == 3  # fixed"))
    assert _verify.removed_tests(before) == {}


def test_a_file_that_stops_parsing_is_left_to_the_lint_gate(tmp_path, monkeypatch):
    """Otherwise every test reads as deleted and buries the real syntax error."""
    rel = _suite_file(tmp_path, monkeypatch, _suite(27))
    before = _verify.snapshot_files([rel])
    (tmp_path / rel).write_text(_suite(27) + "\n}")
    assert _verify.removed_tests(before) == {}


def test_a_rename_is_reported_as_a_removal(tmp_path, monkeypatch):
    """Intended: the agent should SAY it renamed something, not have it vanish."""
    rel = _suite_file(tmp_path, monkeypatch, _suite(5))
    before = _verify.snapshot_files([rel])
    (tmp_path / rel).write_text(_suite(5).replace("test_case_0", "test_case_zero"))
    assert _verify.removed_tests(before)[rel] == ["test_case_0"]


def test_the_gate_fails_the_verification_and_names_the_tests(tmp_path, monkeypatch):
    rel = _suite_file(tmp_path, monkeypatch, _suite(27))
    before = _verify.snapshot_files([rel])
    (tmp_path / rel).write_text(_suite(21, extra_class=False))
    v = codegen.fold_rewrite({"ok": True, "test": {"ok": True}}, before)
    assert v["ok"] is False
    assert "TestDateFiltering" in v["removed_tests"][rel]


def test_deleting_tests_gets_one_retry_telling_it_to_put_them_back(monkeypatch, tmp_path):
    rel = _suite_file(tmp_path, monkeypatch, _suite(27))
    monkeypatch.setattr(codegen, "REPO_ROOT", tmp_path)

    seen = {"tasks": []}
    def fake(task, system, on_event=print, **kw):
        seen["tasks"].append(task)
        (tmp_path / rel).write_text(_suite(21, extra_class=False))
        return _Result(_wrote(rel))
    monkeypatch.setattr(codegen, "run_agent", fake)

    codegen.run_codegen_gated(
        "fix one assertion", "sys", lambda: {"ok": True, "test": {"ok": True}},
        on_event=lambda _: None, require_changes=True, test_path=rel)

    assert len(seen["tasks"]) == 2
    assert "DELETED tests that were passing" in seen["tasks"][1]


def test_the_operator_is_told_which_tests_went():
    out = _verify.blockers({"ok": False,
                            "removed_tests": {"tests/t.py": ["test_a", "TestB"]}})
    assert any("test_a" in b and "TestB" in b for b in out)
    assert any("no longer being checked" in b for b in out)


# --- files nobody predicted the agent would touch ---------------------------
#
# The snapshot covers the scraper and its test — the two files a revise is
# SUPPOSED to touch. It cannot cover a third file, because nothing knows in
# advance which one that would be. agent_tools records the pre-write content of
# anything it actually overwrites, which closes that gap.

def test_a_third_file_the_agent_damages_is_still_caught(monkeypatch, tmp_path):
    monkeypatch.setattr(_verify, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(codegen.agent_tools, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(codegen, "REPO_ROOT", tmp_path)

    other = tmp_path / "tests" / "test_unrelated.py"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text(_suite(10))
    watched = _suite_file(tmp_path, monkeypatch, _suite(5))

    def fake(task, system, on_event=print, **kw):
        # Touches the watched file legitimately, and quietly guts another.
        _replace_contents(watched, _suite(6))
        _replace_contents("tests/test_unrelated.py", _suite(2, extra_class=False))
        return _Result(_wrote(watched, "tests/test_unrelated.py"))
    monkeypatch.setattr(codegen, "run_agent", fake)

    _, v = codegen.run_codegen_gated(
        "fix one thing", "sys", lambda: {"ok": True, "test": {"ok": True}},
        on_event=lambda _: None, require_changes=True, test_path=watched, max_retries=0)

    assert "tests/test_unrelated.py" in v["removed_tests"]
    assert v["ok"] is False


def test_originals_do_not_leak_between_runs(monkeypatch, tmp_path):
    """A file overwritten in an earlier run must not count as this run's damage."""
    monkeypatch.setattr(codegen.agent_tools, "REPO_ROOT", tmp_path)
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests" / "test_x.py").write_text(_suite(5))

    _replace_contents("tests/test_x.py", _suite(1))
    assert codegen.agent_tools.originals()

    codegen.agent_tools.forget_originals()
    assert codegen.agent_tools.originals() == {}


def test_only_the_first_overwrite_of_a_run_is_the_baseline(monkeypatch, tmp_path):
    """Later writes are the agent iterating; the baseline is what it started from."""
    monkeypatch.setattr(codegen.agent_tools, "REPO_ROOT", tmp_path)
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests" / "test_x.py").write_text("ORIGINAL")
    codegen.agent_tools.forget_originals()

    _replace_contents("tests/test_x.py", "second")
    _replace_contents("tests/test_x.py", "third")
    assert codegen.agent_tools.originals()["tests/test_x.py"] == "ORIGINAL"


def test_a_newly_created_file_has_no_original(monkeypatch, tmp_path):
    monkeypatch.setattr(codegen.agent_tools, "REPO_ROOT", tmp_path)
    codegen.agent_tools.forget_originals()
    codegen.agent_tools.write_file("tests/test_brand_new.py", _suite(3))
    assert codegen.agent_tools.originals() == {}


# --- parsers reconcile too ---------------------------------------------------

def test_the_reconciliation_gate_is_wired_up_for_parsers(monkeypatch, tmp_path):
    """Scrapers have had this rule all along; parsers only had the suggestion.

    On the bench, three of nine builds were APPROVED while wrong — one having
    extracted a single transaction out of six. Its own test asserted a count of
    one and passed. Only the document's stated total disagrees with a partial
    read, so the check that consults it has to be mandatory, not advised.
    """
    from orchestration import build_parser

    seen = {}

    def capture(task, system, verify, on_event=print, **kwargs):
        seen.update(kwargs)
        return _Result(_wrote("core/parsers/x.py")), {"ok": True}

    monkeypatch.setattr(build_parser, "run_codegen_gated", capture)
    sample = tmp_path / "s.pdf"
    sample.write_bytes(b"x")

    build_parser.build_parser_for_source("acme_bank", sample, on_event=lambda _: None)
    assert seen["reconcile_path"] == "core/parsers/acme_bank.py"

    seen.clear()
    build_parser.revise_parser_for_source("acme_bank", sample, "wrong signs",
                                          on_event=lambda _: None)
    assert seen["reconcile_path"] == "core/parsers/acme_bank.py", "revise too"


def test_the_shipped_parsers_satisfy_the_rule_they_are_copied_from():
    """The builder prompt tells the agent to study these as the pattern. A pattern
    that does not follow the contract teaches the agent not to follow it."""
    from orchestration import verify as _v

    for path in ("core/parsers/buildium_owner_statement.py",
                 "core/parsers/dfcu_financial_bank.py"):
        assert _v.reconciles(path)["ok"], f"{path} does not check its own arithmetic"


# --- every refusal has to be able to say what it was -------------------------
#
# `blockers()` exists so the screen never says "its test failed" about something
# else. It has one fallback — "refused, but no reason was recorded" — and a real
# build hit it: `fold_untested` set ok=False under `untested_code`, and that was
# the single key with no branch. The operator would have seen a refused build
# with no reason at all.
#
# The list of keys is DERIVED from codegen.py rather than kept by hand here,
# because a hand-kept list goes stale in exactly the way that caused this.

import ast                                                      # noqa: E402
from pathlib import Path                                        # noqa: E402

from orchestration import verify as _verify_mod                 # noqa: E402

FALLBACK = "no reason was recorded"

# One representative value per key, enough to trip its branch. A key that turns
# up in codegen.py without an entry here fails the guard below, which is the
# prompt to add both the sample and the sentence.
REFUSAL_SAMPLES = {
    "untested_code": ["core/parsers/x.py"],
    "uncovered_changes": {"core/parsers/x.py": [12, 13]},
    "hardcoded_options": {"core/parsers/x.py": [{"line": 4, "detail": "a 30-day window"}]},
    # The real shape ruff produces — `code` and all, because codegen's retry
    # message formats it with f['code'] and verify's with f['detail'].
    "lint": [{"path": "core/parsers/x.py", "line": 9, "code": "F841",
              "detail": "Local variable `x` is assigned to but never used"}],
    "unreconciled": "nothing checks the extraction",
    "no_changes": True,
    "wholesale_rewrite": {"core/parsers/x.py": 0.05},
    "removed_tests": {"tests/test_x.py": ["test_one"]},
    "extracted_nothing": True,
}


def _verification_key(target) -> str | None:
    """`verification["x"] = ...` -> "x", anything else -> None."""
    if (isinstance(target, ast.Subscript)
            and isinstance(target.value, ast.Name) and target.value.id == "verification"
            and isinstance(target.slice, ast.Constant) and isinstance(target.slice.value, str)):
        return target.slice.value
    return None


def _keys_that_refuse_a_build() -> set[str]:
    """Every verification key set in the same BLOCK as `ok = False`.

    Block-scoped, not function-scoped: `fold_uncovered` records `coverage`
    unconditionally and only sets `uncovered_changes` alongside the refusal, so a
    whole-function scan would demand a blocker sentence for a key that is just
    information. What has to be explainable is the reason a build was refused.
    """
    tree = ast.parse(Path(codegen.__file__).read_text(encoding="utf-8"))
    keys: set[str] = set()
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if not isinstance(block, list):
                continue
            here, refuses = set(), False
            for statement in block:
                for target in getattr(statement, "targets", []):
                    name = _verification_key(target)
                    if name is None:
                        continue
                    if name == "ok" and getattr(statement.value, "value", None) is False:
                        refuses = True
                    elif name != "ok":
                        here.add(name)
            if refuses:
                keys |= here
    return keys


def test_every_key_that_refuses_a_build_has_a_sample():
    """Guard on the guard: a new fold_* must arrive with both."""
    missing = _keys_that_refuse_a_build() - set(REFUSAL_SAMPLES)
    assert not missing, (
        f"codegen sets {missing} when refusing a build, but this test has no sample for "
        f"it — add one, and a branch in verify.blockers(), or the operator gets "
        f"'{FALLBACK}'.")


@pytest.mark.parametrize("key", sorted(REFUSAL_SAMPLES))
def test_no_refusal_reason_falls_through_to_the_fallback(key):
    out = _verify_mod.blockers({"ok": False, key: REFUSAL_SAMPLES[key]})

    assert out, f"{key} refused the build and blockers() said nothing"
    assert not any(FALLBACK in line for line in out), (
        f"{key} reaches the fallback: {out}")


def test_untested_code_names_the_files():
    out = _verify_mod.blockers({"ok": False, "untested_code": ["core/parsers/x.py"]})

    assert any("core/parsers/x.py" in line for line in out)
    assert not any(FALLBACK in line for line in out)


def test_the_fallback_still_exists_for_a_refusal_nothing_explains():
    """Not removed — a verification that is not-ok for no recorded reason is
    itself worth showing, rather than an empty list that reads as approval."""
    out = _verify_mod.blockers({"ok": False})

    assert len(out) == 1 and FALLBACK in out[0]


# --- a retry is part of the same build ---------------------------------------
#
# Found in a real run. Round 1 wrote the parser, registered it, wrote the test
# and ran it; the lint gate refused it for two unused locals. Round 2 did exactly
# what it was told — four edits to the parser — and wrote no `tests/` file,
# because none was needed. `fold_untested` then refused a build whose test
# existed and passed. The better the agent obeyed the correction, the more
# certainly it failed the next gate.

def test_a_retry_that_only_fixes_the_fault_is_not_called_untested(monkeypatch):
    seen = _runner(monkeypatch, [
        _Result(_wrote("core/parsers/harbor.py", "tests/test_parser_harbor.py")),
        # The correction: same file, four edits, no new test — nothing else was wrong.
        _Result([("str_replace", {"path": "core/parsers/harbor.py"}) for _ in range(4)]),
    ])
    verifications = iter([
        {"ok": False, "test": {"ok": True},
         "lint": [{"path": "core/parsers/harbor.py", "line": 131, "code": "F841",
                   "detail": "Local variable `current_property` is assigned to but never used"}]},
        {"ok": True, "test": {"ok": True}},
    ])

    _, v = codegen.run_codegen_gated(
        "build the parser", "SYSTEM", lambda: dict(next(verifications)),
        on_event=lambda _: None)

    assert len(seen["tasks"]) == 2, "the lint finding should have triggered the retry"
    assert "untested_code" not in v, (
        "the test written in round 1 still exists — a build is all of its rounds")
    assert v["ok"] is True


def test_a_build_that_never_writes_a_test_is_still_refused(monkeypatch):
    """The accumulation must not become an amnesty: no test in ANY round is
    still no test."""
    seen = _runner(monkeypatch, [
        _Result(_wrote("core/parsers/harbor.py")),
        _Result(_wrote("core/parsers/harbor.py")),
    ])

    _, v = codegen.run_codegen_gated(
        "build the parser", "SYSTEM", lambda: {"ok": True}, on_event=lambda _: None)

    assert len(seen["tasks"]) == 2
    assert v["untested_code"] == ["core/parsers/harbor.py"]
    assert v["ok"] is False


def test_the_same_file_edited_repeatedly_is_named_once():
    """Four edits to one file are one changed file; the message said it thrice."""
    calls = [("str_replace", {"path": "core/parsers/harbor.py"}) for _ in range(4)]

    assert codegen.files_written(calls) == ["core/parsers/harbor.py"]
    assert codegen.untested_code_files(calls) == ["core/parsers/harbor.py"]
