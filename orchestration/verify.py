"""The code-generation quality gate: run the agent's own test.

Every time the embedded agent writes code (a parser, a scraper), it MUST also
write a self-contained test — and that test must PASS before the operator is
asked to approve/activate. This runs that test file in a fresh subprocess and
reports the result, so the build workflows gate on it and the TUI shows it.

A missing test is a FAILURE, not a pass: the harness does not ship untested code.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_MAX_OUTPUT = 8000


def test_path_for(kind: str, source_key: str) -> str:
    """Where the agent must write the test for a given build (parser or scraper).
    Distinct names so a source that has BOTH a parser and a scraper doesn't collide."""
    return f"tests/test_{kind}_{source_key}.py"


def run_test_file(test_path: str, timeout: int = 300) -> dict:
    """Run one pytest file. Returns {ok, missing, test_path, output}. A missing
    file is ok=False (untested code does not pass the gate)."""
    full = REPO_ROOT / test_path
    if not full.exists():
        return {
            "ok": False,
            "missing": True,
            "test_path": test_path,
            "output": f"No test written at {test_path}. The harness must write a "
                      "self-contained test for the code it generates before it can be approved.",
        }
    proc = subprocess.run(
        ["poetry", "run", "pytest", test_path, "-q"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    out = (proc.stdout + proc.stderr).strip()
    if len(out) > _MAX_OUTPUT:
        out = out[-_MAX_OUTPUT:]
    return {"ok": proc.returncode == 0, "missing": False, "test_path": test_path, "output": out}


def hardcoded_options(path: str) -> list[dict]:
    """Configuration-shaped literals baked into a source module.

    Infrastructure rule: the choices a portal asks for before handing over data
    (a lookback window, an accounting basis, a property selection) belong in
    core/settings.py, NOT in the code — otherwise changing one costs a code edit,
    a test run and an approval. The scraper-builder prompt says so, but a prompt
    is advice; this makes it a gate.

    Two shapes are looked for, both low-noise:
      - `timedelta(days=30)` and friends — a hardcoded window.
      - literal scalars inside a request-payload dict (>=3 string keys), which is
        how a portal's filter selections get frozen.

    An `_extract()` mapping is unaffected: its dict values are expressions over
    the response, not literals.

    The SETTINGS declaration itself is exempt, and has to be: a schema entry is a
    dict of literal strings — `{"key": ..., "label": ..., "default": ...}` — i.e.
    exactly the shape this looks for. Without this the gate reports the very fix it
    just demanded, the agent declares settings, gets told it hardcoded choices, and
    goes round again. That happened: 22 findings, all of them inside a correct
    SETTINGS block.

    ESCAPE HATCH: a line carrying `# fixed:` is exempt. Some values genuinely are
    protocol, not preference, and the right answer there is a written reason
    rather than silence — which is also why the exemption is per line and has to
    say something.
    """
    full = REPO_ROOT / path
    try:
        source = full.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (OSError, SyntaxError):
        return []

    lines = source.splitlines()

    # Every line spanned by the module-level SETTINGS assignment — the declaration
    # is the answer, not an offence.
    settings_lines: set[int] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == "SETTINGS" for t in node.targets
        ):
            last = getattr(node, "end_lineno", node.lineno)
            settings_lines.update(range(node.lineno, last + 1))

    def exempt(lineno: int) -> bool:
        line = lines[lineno - 1] if 0 < lineno <= len(lines) else ""
        return "# fixed:" in line or lineno in settings_lines

    def literal(node) -> bool:
        return isinstance(node, ast.Constant) and isinstance(node.value, (str, int, float, bool))

    findings: list[dict] = []

    def add(node, kind: str, detail: str) -> None:
        if not exempt(node.lineno):
            findings.append({"line": node.lineno, "kind": kind, "detail": detail})

    for node in ast.walk(tree):
        # a hardcoded time window
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name == "timedelta":
                for kw in node.keywords:
                    if literal(kw.value):
                        add(node, "time_window", f"timedelta({kw.arg}={kw.value.value!r})")

        # a frozen filter payload
        if isinstance(node, ast.Dict) and len(node.keys) >= 3:
            string_keys = [k for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)]
            if len(string_keys) < 3:
                continue
            for key, value in zip(node.keys, node.values):
                if literal(value) and isinstance(key, ast.Constant):
                    add(value, "payload_literal", f"{key.value}={value.value!r}")

    return findings


# Pyflakes plus the runtime-error checks — the rules that catch code which does
# not do what it appears to do. Cosmetic rules are deliberately excluded: a gate
# that blocks a build over line length teaches the agent to fight the formatter.
LINT_RULES = "F,E9"

# Which findings actually fail a build.
#
# The gate used to have one knob: a rule was selected or it did not exist. So the
# only way to stop it refusing correct work over a triviality was to delete the
# whole rule, and deleting a rule loses the finding entirely — nobody is told
# again, ever. That happened once with F401 and was about to happen to F841,
# which on the same bench run was catching a genuinely broken parser.
#
# So findings are CLASSIFIED instead of filtered. Everything ruff reports is
# kept; a finding blocks unless it is listed below as advisory, with a reason.
# Note the direction: this list fails CLOSED. A rule ruff gains tomorrow blocks a
# build until someone decides otherwise in writing, rather than slipping through
# because nobody thought about it.
#
# The gate's claim, which decides what belongs here: a value computed and then
# discarded is usually a wire left unconnected — if it came from the operator's
# settings, the setting is silently being ignored. Anything that isn't that, and
# doesn't change behaviour, is advisory.
ADVISORY_RULES: dict[str, str] = {
    "F401": "an unused import changes no behaviour and hides no setting — tidiness, "
            "not a disconnected wire",
}

# An unused `except ... as e` binding was briefly listed above, and should not be.
# It looked like the same shape of complaint as F401 — a linter being fussy about
# a name — but it is not tidiness: this project logs every deterministic failure
# as a structured record, so a caught exception is meant to end up IN one. A
# binding nobody uses means an error was caught and nothing was recorded, which
# is the silent failure the observability standard exists to prevent.
#
# Forgiving it also cost more than it saved. Telling F841 apart from itself meant
# joining ruff's reported line number to an AST node — an inference ruff does not
# actually export, in a gate whose whole point is that findings are classified by
# what they MEAN. The contracts now say to bind the exception only when it is
# used, which is always satisfiable, so the case mostly stops arising.


def lint(paths: list[str], timeout: int = 60) -> list[dict]:
    """Code that reads as if it works and doesn't.

    The case that put this here: a scraper computed `property_filter` from the
    operator's chosen property and then never used it, so the request always
    asked for every property. Every other gate passed — the settings were
    declared, read at run time, and the test was green — because the tests
    checked the SHAPE of the declaration and nothing checked that the value
    reached the request. Picking a property silently did nothing.

    A dead store is exactly that failure, and it is free to detect: `ruff
    --select F` names it F841. The gate already refuses untested code; refusing
    code that discards its own inputs is the same principle.

    Returns [] when ruff itself can't run — a missing linter is a reason to say
    so, not a reason to fail every build.
    """
    # Only what is actually on disk. A path the agent reported writing but never
    # wrote is a real problem — and one the no-changes and untested-code gates
    # already name properly; letting ruff report it as "no such file" would bury
    # that behind a linter error about a file, which explains nothing.
    targets = [p for p in paths if p.endswith(".py") and (REPO_ROOT / p).is_file()]
    if not targets:
        return []
    try:
        # Run ruff as a module of THIS interpreter rather than through `poetry
        # run`: the tool is then found from the environment already in use, not
        # from whatever directory the check happens to run in.
        proc = subprocess.run(
            [sys.executable, "-m", "ruff", "check", f"--select={LINT_RULES}",
             "--output-format=json", *[str(REPO_ROOT / p) for p in targets]],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    try:
        import json
        findings = json.loads(proc.stdout or "[]")
    except (ValueError, TypeError):
        return []

    out: list[dict] = []
    for item in findings:
        location = item.get("location") or {}
        path = item.get("filename", "").replace(str(REPO_ROOT) + "/", "")
        code = item.get("code") or ""
        line = location.get("row")
        advice = ADVISORY_RULES.get(code)
        out.append({
            "path": path,
            "line": line,
            "code": code,
            "detail": item.get("message", ""),
            # Kept whatever the verdict: a finding that doesn't block is still a
            # finding, and dropping it is how the last two of these went missing.
            "blocking": advice is None,
            "advice": advice or "",
        })
    return out


def blocking(findings: list[dict]) -> list[dict]:
    """The findings that fail a build. Everything else is advisory and kept."""
    return [f for f in findings if f.get("blocking", True)]




def declares_settings(path: str) -> bool:
    """Does this module declare a non-empty SETTINGS list AND read it at run time?
    Declaring without reading is just as hardcoded, only less honest."""
    full = REPO_ROOT / path
    try:
        tree = ast.parse(full.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return False

    declared = any(
        isinstance(node, ast.Assign)
        and any(getattr(t, "id", None) == "SETTINGS" for t in node.targets)
        and isinstance(node.value, ast.List) and node.value.elts
        for node in ast.walk(tree)
    )
    reads = any(
        isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "values_for"
        for node in ast.walk(tree)
    )
    return declared and reads


# Below this fraction of surviving lines, a "fix" is a rewrite. Calibrated on the
# recorded builds in data/logs/builds: every genuine iterative edit the agent has
# ever made to an existing file scored 0.84-1.00, and the rewrite that destroyed a
# working test file scored 0.08. The gap is enormous; 0.4 sits in the middle of it.
MIN_REVISE_SIMILARITY = 0.4


def snapshot_files(paths: list[str]) -> dict[str, str]:
    """The current contents of `paths` that exist, for comparing against later."""
    out: dict[str, str] = {}
    for path in paths:
        full = REPO_ROOT / path
        try:
            if full.is_file():
                out[path] = full.read_text(encoding="utf-8")
        except OSError:
            continue
    return out


def wholesale_rewrites(before: dict[str, str]) -> dict[str, float]:
    """Files whose content was REPLACED rather than edited. {path: similarity}.

    A revise is a fix, and a fix is targeted. Asked three times running to correct
    a single undefined name, the agent rewrote a 272-line test file from scratch —
    twice producing new bugs, and the third time hitting the turn cap mid-write and
    leaving a file that would not parse. Each rewrite passed every other gate on
    the way in, because each one was, in isolation, a plausible file.

    The damage is specific and not otherwise detectable: work disappears. That
    rewrite silently dropped the reconciliation tests, so the scraper kept its
    control-total check while nothing was left to prove it. No gate that looks at
    the new file alone can see what used to be in the old one.

    Deliberately NOT applied to a build: the first version of a file has nothing
    to be similar to, and successive drafts within one build are how the agent
    works.
    """
    import difflib

    out: dict[str, float] = {}
    for path, old in before.items():
        full = REPO_ROOT / path
        try:
            new = full.read_text(encoding="utf-8")
        except OSError:
            continue
        if new == old or not old.strip():
            continue
        ratio = difflib.SequenceMatcher(None, old.splitlines(), new.splitlines()).ratio()
        if ratio < MIN_REVISE_SIMILARITY:
            out[path] = round(ratio, 2)
    return out


def _test_names(source: str) -> set[str] | None:
    """Every test function and test class in a module. None if it doesn't parse."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            names.add(node.name)
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            names.add(node.name)
    return names


def removed_tests(before: dict[str, str]) -> dict[str, list[str]]:
    """Tests that existed before the agent's edit and don't afterwards.

    The companion to `wholesale_rewrites`, and it exists because that check has a
    blind spot this closes. Similarity only notices REPLACEMENT: a revise that
    took a 778-line test file down to 455 kept enough lines to score well above
    the threshold and sailed through, having quietly dropped six tests. Deleting a
    third of a file is the same damage as replacing it, in a shape the ratio can't
    see.

    Tests specifically, because their loss is the silent kind. Delete a function
    the code needs and something fails immediately; delete the test that proves
    the code works and everything stays green — which is exactly how the
    reconciliation coverage disappeared while the scraper kept reconciling.

    A rename shows up here as a removal, and that is the intended behaviour: the
    agent should say it renamed something rather than have it vanish silently.
    """
    out: dict[str, list[str]] = {}
    for path, old in before.items():
        # No filename guessing about which file is "the test file". Only files
        # that HAD tests can lose them, so the AST already answers it — and a
        # name-shaped guess is one more thing to be subtly wrong about.
        full = REPO_ROOT / path
        try:
            new_source = full.read_text(encoding="utf-8")
        except OSError:
            continue
        was, now = _test_names(old), _test_names(new_source)
        # A file that no longer parses is a different failure, and `lint` names it
        # properly. Reporting every test as "removed" would bury that.
        if was is None or now is None:
            continue
        gone = sorted(was - now)
        if gone:
            out[path] = gone
    return out


NO_TOTALS_DECLARATION = "NO_CONTROL_TOTALS"


def reconciles(path: str) -> dict:
    """Does this scraper check its extraction against the source's own arithmetic?

    Returns {"ok": bool, "detail": str}.

    This gate exists because reconciliation was the ONE rule in the scraper
    prompt with nothing enforcing it, and the difference showed immediately: the
    Epic scraper reconciles, the DFCU scraper — written from the same
    instructions — does not, even though the bank returns a `runningBalance` on
    every row. An instruction the harness doesn't check is a suggestion, and a
    model under context pressure drops suggestions first.

    It matters more than the other gates, not less. A passing test says the
    parsing logic is unchanged. A successful run says the portal answered.
    Neither notices that a date window clipped rows, an account was skipped, or
    pagination stopped early — the run stays green while the numbers are quietly
    wrong, which for financial data is the worst failure mode available.

    The escape hatch is a declaration, not a silence: a source that genuinely
    publishes no totals says so as a module-level
    `NO_CONTROL_TOTALS = "<why>"`, which is greppable and reviewable, in the same
    spirit as `# fixed: <reason>`.
    """
    full = REPO_ROOT / path
    try:
        tree = ast.parse(full.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return {"ok": True, "detail": "not checked — the module could not be parsed"}

    records = any(
        isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "record"
        and getattr(getattr(node.func, "value", None), "id", None) == "reconcile"
        for node in ast.walk(tree)
    )
    if records:
        return {"ok": True, "detail": "calls reconcile.record()"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == NO_TOTALS_DECLARATION for t in node.targets
        ):
            reason = getattr(node.value, "value", None)
            if isinstance(reason, str) and len(reason.strip()) >= 15:
                return {"ok": True, "detail": f"declares no control totals: {reason.strip()}"}
            return {
                "ok": False,
                "detail": f"{NO_TOTALS_DECLARATION} is set but says nothing useful — "
                          "give the reason this source publishes no totals.",
            }

    return {
        "ok": False,
        "detail": "nothing checks the extraction against the source's own totals, and "
                  f"no {NO_TOTALS_DECLARATION} explains why there are none.",
    }


def changed_lines(path: str) -> set[int]:
    """Line numbers this file has ADDED or MODIFIED versus HEAD.

    The gate's question is not "does a test exist" but "does a test exercise what
    you just wrote", and that's the set of lines to ask about.
    """
    proc = subprocess.run(
        ["git", "diff", "-U0", "HEAD", "--", path],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        return set()
    lines: set[int] = set()
    for hunk in re.finditer(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@", proc.stdout, re.M):
        start = int(hunk.group(1))
        count = int(hunk.group(2) or 1)
        lines.update(range(start, start + count))
    return lines


def covers_changes(test_path: str, code_paths: list[str], timeout: int = 300) -> dict:
    """Did running `test_path` actually EXECUTE the lines changed in `code_paths`?

    Existence of a test proves nothing: this project's scraper had two passing
    tests the entire time it was 403-broken, because they only covered a pure
    helper the fix never touched. A stale green test and real coverage look
    identical to a "was a test written?" check — and different to this one.

    Returns {ok, checked, uncovered, detail}. `ok` is True when every changed,
    executable line ran. Lines that are not executable statements (comments,
    blank lines, docstrings) are ignored — coverage never reports those, so
    counting them would make the gate impossible to pass.
    """
    targets = {p: changed_lines(p) for p in code_paths}
    targets = {p: ls for p, ls in targets.items() if ls}
    if not targets:
        return {"ok": True, "checked": False,
                "detail": "No changed lines to check coverage for."}

    data_file = REPO_ROOT / ".coverage.gate"
    proc = subprocess.run(
        ["poetry", "run", "coverage", "run", f"--data-file={data_file}",
         "--include=" + ",".join(code_paths), "-m", "pytest", test_path, "-q"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        # The test itself failed; run_test_file reports that. Don't double-report.
        data_file.unlink(missing_ok=True)
        return {"ok": True, "checked": False,
                "detail": "Test did not pass, so coverage was not assessed."}

    try:
        import coverage as _coverage
        cov = _coverage.Coverage(data_file=str(data_file))
        cov.load()
        data = cov.get_data()
        uncovered: dict[str, list[int]] = {}
        for path, wanted in targets.items():
            full = str((REPO_ROOT / path).resolve())
            executed = set(data.lines(full) or [])
            # Only statements coverage could ever report; ignore comments/blanks.
            analysis = cov.analysis2(full)
            statements = set(analysis[1])
            missing = sorted((wanted & statements) - executed)
            if missing:
                uncovered[path] = missing
    except Exception as exc:
        return {"ok": True, "checked": False, "detail": f"Coverage unavailable: {exc}"}
    finally:
        data_file.unlink(missing_ok=True)

    return {
        "ok": not uncovered,
        "checked": True,
        "uncovered": uncovered,
        "detail": "Every changed line ran under the test." if not uncovered else
                  "; ".join(f"{p} lines {ls}" for p, ls in uncovered.items()),
    }


def notes(verification: dict) -> list[str]:
    """What was seen but did not refuse the build, in the operator's words.

    The counterpart to `blockers`, and the reason findings are classified rather
    than filtered. Deleting a rule to stop it refusing correct work also deletes
    the finding — nobody is ever told again. Advisory means "not worth a
    rebuild", not "not worth knowing", and that is only true if it reaches the
    screen.
    """
    out: list[str] = []
    for finding in verification.get("lint_advisory") or []:
        where = f"{finding.get('path')} line {finding.get('line')}"
        out.append(f"{where}: {finding.get('detail')} — {finding.get('advice')}.")
    return out


def blockers(verification: dict) -> list[str]:
    """Why a build isn't approvable, in the operator's words, most actionable first.

    The GUI used to say "its test did NOT pass" for every refusal, whatever the
    actual reason. That is wrong most of the time and actively misleading some of
    it: a run once failed only because the agent wrote no file on its final turn,
    while its test passed 17/17 — and the screen sent the operator to debug a test
    that was fine. Whatever refused the build has to be the thing that's named.
    """
    if not verification or verification.get("ok"):
        return []

    out: list[str] = []
    if verification.get("extracted_nothing"):
        out.append("The parser ran on the real document and found no transactions in it "
                   "at all. Whatever its test proves, it is not reading this source — "
                   "activating it would ingest nothing, silently.")

    test = verification.get("test") or {}
    if test.get("missing"):
        out.append("No test was written for the change, and the harness won't ship untested code.")
    elif test and not test.get("ok"):
        # No `ok` at all counts as failing: a refusal with test output attached and
        # no verdict must not come out as "no reason was recorded".
        out.append("Its test failed when the harness re-ran it independently.")

    if verification.get("no_changes"):
        out.append("The agent reported success without writing any file, so nothing changed. "
                   "(Its earlier edits, if any, are still on disk — check the diff.)")

    rewritten = verification.get("wholesale_rewrite") or {}
    if rewritten:
        detail = "; ".join(
            f"{path} (only {int(ratio * 100)}% of its lines survived)"
            for path, ratio in rewritten.items())
        out.append(f"The agent replaced a file rather than editing it — {detail}. Work "
                   f"that was already there may have been dropped; check the diff before "
                   f"trusting this.")

    dropped = verification.get("removed_tests") or {}
    if dropped:
        detail = "; ".join(
            f"{path}: {', '.join(names)}" for path, names in dropped.items())
        out.append(f"Tests that were passing have been deleted — {detail}. Whatever they "
                   f"proved is no longer being checked.")

    if verification.get("unreconciled"):
        out.append("Nothing checks the numbers against the source's own totals, so a run "
                   "can't tell a complete pull from a partial one — "
                   f"{verification['unreconciled']}")

    uncovered = verification.get("uncovered_changes") or {}
    if uncovered:
        detail = "; ".join(f"{path} lines {sorted(lines)}" for path, lines in uncovered.items())
        out.append(f"The test passes but never runs the lines that changed — {detail}.")

    hardcoded = verification.get("hardcoded_options") or {}
    if hardcoded:
        detail = "; ".join(
            f"{path} line {item['line']} ({item['detail']})"
            for path, found in hardcoded.items() for item in found)
        out.append(f"Choices the operator should be able to change are still baked into the "
                   f"code — {detail}.")

    findings = verification.get("lint") or []
    if findings:
        detail = "; ".join(
            f"{f['path']} line {f['line']} ({f['detail']})" for f in findings)
        out.append(f"The code contains something that does nothing, which usually means a wire "
                   f"was left unconnected — {detail}.")

    if verification.get("agent_stopped"):
        out.append(verification["agent_stopped"])

    if verification.get("registered") is False:
        out.append(verification.get("registration_detail")
                   or "The module isn't registered, so nothing can call it yet.")

    if not out:
        out.append(verification.get("error") or "The build was refused, but no reason was recorded.")
    return out
