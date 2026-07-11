"""Standalone eval harness. Runs against core/ directly — never imports
orchestration/ or any framework, so it can score a harness-swapped
implementation with zero changes.

Usage: python -m evals.runner --golden-set evals/golden_set
"""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from core.models import Decision, Transaction


@dataclass
class EvalResult:
    case_id: str
    correct: bool
    latency_s: float
    expected: Decision
    actual: Decision


def load_golden_set(golden_set_dir: Path) -> list[tuple[str, Transaction, Decision]]:
    cases = []
    for case_file in sorted(golden_set_dir.glob("case_*.json")):
        data = json.loads(case_file.read_text())
        cases.append((
            case_file.stem,
            Transaction.model_validate(data["input"]),
            Decision.model_validate(data["expected"]),
        ))
    return cases


def run_eval(
    golden_set_dir: Path,
    agent_fn: Callable[[Transaction], Decision],
) -> list[EvalResult]:
    results = []
    for case_id, entity, expected in load_golden_set(golden_set_dir):
        start = time.monotonic()
        actual = agent_fn(entity)
        latency_s = time.monotonic() - start
        correct = actual.status == expected.status and actual.recommendation == expected.recommendation
        results.append(EvalResult(case_id, correct, latency_s, expected, actual))
    return results


def summarize(results: list[EvalResult]) -> dict:
    if not results:
        return {"accuracy": 0.0, "avg_latency_s": 0.0, "n": 0}
    accuracy = sum(r.correct for r in results) / len(results)
    avg_latency = sum(r.latency_s for r in results) / len(results)
    return {"accuracy": accuracy, "avg_latency_s": avg_latency, "n": len(results)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--golden-set", type=Path, default=Path("evals/golden_set"))
    args = parser.parse_args()

    raise NotImplementedError(
        "Wire in the agent_fn (core.tools + core.validators) once it exists."
    )


if __name__ == "__main__":
    main()
