"""Run ONE bench case, in whatever repo this module was imported from.

Always invoked as a subprocess with the worktree as its cwd, never in-process:
`agent_tools.REPO_ROOT` and `verify.REPO_ROOT` are module constants derived from
`__file__`, so the only way to point the agent at a throwaway copy of the repo is
to be running inside that copy. That is also what keeps a bench run from writing
`core/parsers/bench_*.py` into the repo you are working in.

    poetry run python -m orchestration.bench.one <case_key> <result.json>
"""

from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path

from orchestration.bench.cases import BY_KEY
from orchestration.build_parser import build_parser_for_source
from orchestration.verify import blockers

# codegen.py emits this line when a gate sends the agent back around. Counting it
# is how a round count is obtained without changing run_codegen_gated, which is
# Phase 2's job.
REJECTION_MARKER = "The harness rejected that run"


def main(argv: list[str]) -> int:
    case = BY_KEY[argv[0]]
    out_path = Path(argv[1])

    events: list[str] = []

    def on_event(text: str) -> None:
        events.append(text)
        print(text, flush=True)

    def tools_from_events() -> list[str]:
        """Tool names recovered from the progress stream.

        The real list lives on the AgentResult, which a crashed run never
        returns — so the one time the tool mix matters most, it used to come
        back empty and read as "the agent did nothing". It had usually done
        twenty turns of work before whatever killed it.
        """
        return [line.strip()[2:].split("(", 1)[0].strip()
                for line in events if line.strip().startswith("→ ")]

    started = time.monotonic()
    record: dict = {"case_key": case.key, "difficulty": case.difficulty}
    try:
        built = build_parser_for_source(
            source_key=case.key,
            sample_path=case.path,
            source_label=case.label,
            on_event=on_event,
        )
        verification = built.get("verification") or {}
        record.update(
            verification=verification,
            agent_summary=built.get("agent_summary", ""),
            tool_calls=[name for name, _ in built.get("tool_calls", [])],
            blockers=blockers(verification),
        )
    except Exception as exc:  # a crash is a result, not a lost run
        record.update(
            verification={"ok": False, "error": f"{type(exc).__name__}: {exc}"},
            crash=traceback.format_exc()[-4000:],
            blockers=[f"The build raised {type(exc).__name__}."],
            tool_calls=tools_from_events(),
            agent_summary="",
        )

    record["seconds"] = round(time.monotonic() - started, 1)
    record["rounds"] = 1 + sum(1 for event in events if REJECTION_MARKER in event)
    record["model"] = next((e for e in events if e.startswith("[model]")), "")
    # The last one wins: on a retry there is a ledger per round, and the run that
    # matters is the one that ended it. Both are in the .log either way.
    context = [e for e in events if e.startswith("[context]")]
    record["context"] = context[-1] if context else ""
    record["context_breakdown"] = next(
        (e for e in reversed(context) if "where it went" in e), "")
    out_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
