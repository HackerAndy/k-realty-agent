"""Background worker for a GUI-triggered portal demonstration.

Recording a demonstration means opening a real browser and waiting — minutes,
sometimes — while the operator signs in, sets their filters, and gets their data
on screen. No web request can wait for that, so it runs in its own process and
reports through a small JSON status file the GUI polls. Same shape as the login
recovery and Gmail consent workers, for the same reason.

Exit code 0 means a demonstration was captured; the path is in the status file.
"""

from __future__ import annotations

import json
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path


def _write(status_path: Path, payload: dict) -> None:
    payload = {"ts": datetime.now(UTC).isoformat(), **payload}
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps(payload))


def run(source_key: str, url: str, status_path: Path) -> int:
    from core.tools import demo_recorder

    _write(status_path, {
        "status": "running",
        "message": ("A browser is open. Sign in, set your filters, and click Generate/Search "
                    "so YOUR data is on screen — then close the window."),
    })
    try:
        demo_path = demo_recorder.record(source_key, url)
    except Exception as exc:
        _write(status_path, {"status": "failed", "error": str(exc),
                             "traceback": traceback.format_exc()})
        return 1

    demo = json.loads(Path(demo_path).read_text())
    _write(status_path, {
        "status": "completed",
        "demo_path": str(demo_path),
        # The URL the operator actually landed on — the harness stores this as the
        # source's login_url rather than asking them to type it.
        "final_url": demo.get("final_url", ""),
        "start_url": demo.get("start_url", ""),
        "title": demo.get("title", ""),
        "captured_requests": len(demo.get("candidate_requests", [])),
        "recorded_actions": len(demo.get("recorded_actions", [])),
        "message": "Demonstration captured.",
    })
    return 0


def main() -> int:
    if len(sys.argv) != 4:
        print("usage: python -m core.tools.demo_worker <source_key> <url|-> <status_file>")
        return 2
    url = "" if sys.argv[2] == "-" else sys.argv[2]
    return run(sys.argv[1], url, Path(sys.argv[3]))


if __name__ == "__main__":
    raise SystemExit(main())
