"""Background worker for GUI-triggered portal login recovery.

Runs a visible persistent browser for one source and exits only when the user
closes all browser pages/windows. This allows one-click recovery from the web UI
without terminal prompts.
"""

from __future__ import annotations

import sys
import traceback

from core.tools.browser_session import bootstrap_login_until_window_closed


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: python -m core.tools.login_recovery_worker <source_key> <url>")
        return 2

    source_key = sys.argv[1]
    url = sys.argv[2]
    try:
        bootstrap_login_until_window_closed(source_key, url)
        return 0
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
