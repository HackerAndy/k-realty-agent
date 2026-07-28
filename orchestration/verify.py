"""The code-generation quality gate: run the agent's own test.

Every time the embedded agent writes code (a parser, a scraper), it MUST also
write a self-contained test — and that test must PASS before the operator is
asked to approve/activate. This runs that test file in a fresh subprocess and
reports the result, so the build workflows gate on it and the TUI shows it.

A missing test is a FAILURE, not a pass: the harness does not ship untested code.
"""

from __future__ import annotations

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
