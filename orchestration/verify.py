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

    if verification.get("registered") is False:
        out.append(verification.get("registration_detail")
                   or "The module isn't registered, so nothing can call it yet.")

    if not out:
        out.append(verification.get("error") or "The build was refused, but no reason was recorded.")
    return out
