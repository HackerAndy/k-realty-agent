from pathlib import Path

from scripts.check_portability import find_violations

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_core_and_evals_have_no_langgraph_or_langchain_imports():
    violations = []
    for dir_name in ("core", "evals"):
        for py_file in (REPO_ROOT / dir_name).rglob("*.py"):
            violations += [(py_file, v) for v in find_violations(py_file)]
    assert violations == [], f"Found banned imports: {violations}"
