"""The code-generation quality gate: run the agent's own test.

Every time the embedded agent writes code (a parser, a scraper), it MUST also
write a self-contained test — and that test must PASS before the operator is
asked to approve/activate. This runs that test file in a fresh subprocess and
reports the result, so the build workflows gate on it and the TUI shows it.

A missing test is a FAILURE, not a pass: the harness does not ship untested code.
"""

from __future__ import annotations

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
