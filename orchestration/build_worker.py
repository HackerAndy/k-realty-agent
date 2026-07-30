# Template candidate: generic (tier 1, pattern) — a background runner for the
# agent's long code-gen jobs, so a GUI can start one and poll it.
# See agent-harness-template/docs/promotion-log.md.
"""Background worker for GUI-triggered code-gen builds.

An agent build takes minutes, so no front-end can wait on it inside a request.
This runs one build in its own process and streams progress to a JSONL run file
that the GUI polls — same shape as the login-recovery worker, but carrying the
agent's events and its final verification result instead of just an exit code.

Run-file protocol (one JSON object per line, append-only):

    {"type": "event",  "ts": ..., "text": "..."}    the agent's on_event stream
    {"type": "result", "ts": ..., "result": {...}}  the workflow's return value
    {"type": "failed", "ts": ..., "error": "...", "traceback": "..."}

A build that stops making progress is ended by orchestration/watchdog.py rather
than left to hang: one sat for 21 minutes holding an open socket to the operator's
model with nothing arriving, and the app had no way to stop it. Every event feeds
the watchdog, so the allowance follows the run's own pace instead of a constant
that is wrong for a slow model in one direction and a wedged one in the other.

Exit code 0 means the build RAN, not that it passed — whether the code is
acceptable is the `verification.ok` flag in the result, which the operator
reviews and approves. Activation stays the human's call.

Lives in orchestration/ because it drives the agent layer.
"""

from __future__ import annotations

import argparse
import faulthandler
import json
import os
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path

from orchestration.watchdog import ProgressWatchdog, Stall

REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_DIR = Path("data/logs/builds")

KINDS = ("parser", "scraper")
MODES = ("build", "revise")


def _emit(run_file: Path, payload: dict) -> None:
    """Append one protocol line and flush — the GUI polls this file while we run,
    so a buffered write would look like a hung build."""
    payload = {"ts": datetime.now(UTC).isoformat(), **payload}
    with run_file.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload) + "\n")
        fh.flush()


def run(kind: str, mode: str, source_key: str, run_file: Path,
        sample_path: str | None = None, feedback: str = "",
        source_label: str = "", portal_url: str = "",
        demo_path: str | None = None) -> int:
    """Dispatch to the matching workflow, streaming its events to `run_file`."""
    # Imported here, not at module import, so a bad LLM config surfaces as a
    # `failed` line in the run file rather than an import crash the GUI can't see.
    from orchestration.build_parser import build_parser_for_source, revise_parser_for_source
    from orchestration.build_scraper import build_scraper_for_source, revise_scraper_for_source

    def on_stall(stall: Stall) -> None:
        """Called on the watchdog's thread, because the working thread is the one
        that's stuck. Record WHERE it was stuck before exiting: a hang's stack is
        the one thing that can't be recovered afterwards, and the last one was
        lost because the process died before anyone could look."""
        stacks = run_file.with_suffix(".stacks.txt")
        try:
            with stacks.open("w", encoding="utf-8") as fh:
                fh.write(f"{stall.describe()}\n\n")
                faulthandler.dump_traceback(file=fh, all_threads=True)
        except Exception:
            pass
        _emit(run_file, {
            "type": "failed",
            "error": stall.describe(),
            "stall": {"reason": stall.reason, "idle_s": round(stall.idle_s, 1),
                      "budget_s": round(stall.budget_s, 1),
                      "elapsed_s": round(stall.elapsed_s, 1),
                      "stacks": str(stacks)},
        })
        # The main thread is blocked in a syscall — a normal exit would join it and
        # hang exactly as before, so leave hard. The run file is already flushed.
        os._exit(2)

    watchdog = ProgressWatchdog(on_stall=on_stall)

    def on_event(text: str) -> None:
        watchdog.beat(str(text))
        _emit(run_file, {"type": "event", "text": str(text)})

    watchdog.start()
    try:
        if kind == "parser":
            if not sample_path:
                raise ValueError("a sample document path is required to build a parser")
            sample = Path(sample_path)
            if not sample.exists():
                raise FileNotFoundError(f"no sample document at {sample}")
            if mode == "build":
                result = build_parser_for_source(source_key, sample, source_label, on_event=on_event)
            else:
                result = revise_parser_for_source(source_key, sample, feedback, source_label, on_event=on_event)
        else:
            if mode == "build":
                # A captured demonstration already carries where the operator
                # went; only a build that still has to record one needs a URL.
                if not portal_url and not demo_path:
                    raise ValueError("a portal URL or a captured demonstration is required "
                                     "to build a scraper")
                result = build_scraper_for_source(
                    source_key, portal_url, source_label, on_event=on_event, demo_path=demo_path
                )
            else:
                result = revise_scraper_for_source(source_key, feedback, on_event=on_event)
    except Exception as exc:
        _emit(run_file, {"type": "failed", "error": str(exc), "traceback": traceback.format_exc()})
        return 1
    finally:
        watchdog.stop()

    _emit(run_file, {"type": "result", "result": result})
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m orchestration.build_worker")
    ap.add_argument("--kind", required=True, choices=KINDS)
    ap.add_argument("--mode", required=True, choices=MODES)
    ap.add_argument("--source-key", required=True)
    ap.add_argument("--run-file", required=True)
    ap.add_argument("--sample-path", default=None)
    ap.add_argument("--feedback", default="")
    ap.add_argument("--source-label", default="")
    ap.add_argument("--portal-url", default="")
    ap.add_argument("--demo-path", default=None)
    args = ap.parse_args()

    run_file = Path(args.run_file)
    run_file.parent.mkdir(parents=True, exist_ok=True)
    return run(
        kind=args.kind,
        mode=args.mode,
        source_key=args.source_key,
        run_file=run_file,
        sample_path=args.sample_path,
        feedback=args.feedback,
        source_label=args.source_label,
        portal_url=args.portal_url,
        demo_path=args.demo_path,
    )


if __name__ == "__main__":
    sys.exit(main())
