"""Background worker for GUI-triggered Gmail consent.

Google's installed-app flow opens a browser and BLOCKS until the operator clicks
Allow, so it can't run inside a web request. This runs it in its own process and
reports through a small JSON status file the GUI polls — the same shape as the
login-recovery worker, for the same reason.

Exit code 0 means consent completed and a refresh token was stored.

The client-secret file is deleted as soon as consent finishes, however it
finishes: its contents are already in the encrypted store by then, and leaving a
second copy of a client secret lying around on disk is a needless risk.
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


def run(source_key: str, client_secret_path: Path, status_path: Path) -> int:
    from core.tools import email_oauth

    _write(status_path, {"status": "running",
                         "message": "A browser is open — choose the Google account and click Allow."})
    try:
        email = email_oauth.run_consent(source_key, client_secret_path)
    except Exception as exc:
        _write(status_path, {"status": "failed", "error": str(exc),
                             "traceback": traceback.format_exc()})
        return 1
    finally:
        # The client secret now lives encrypted in the vault; drop the plaintext.
        try:
            client_secret_path.unlink(missing_ok=True)
        except Exception:
            pass

    _write(status_path, {"status": "completed", "account_email": email,
                         "message": f"Connected {email}."})
    return 0


def main() -> int:
    if len(sys.argv) != 4:
        print("usage: python -m core.tools.oauth_consent_worker <source_key> <client_json> <status_file>")
        return 2
    return run(sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]))


if __name__ == "__main__":
    raise SystemExit(main())
