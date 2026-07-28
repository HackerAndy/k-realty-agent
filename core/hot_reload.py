# Template candidate: generic (tier 1) — no client-specific logic.
# See agent-harness-template/docs/promotion-log.md.
"""Pick up code the harness just wrote, without restarting the server.

This harness rewrites its OWN domain code — the agent edits
`core/scrapers/<key>.py` or `core/parsers/<key>.py` while `agent-web` is running.
Python caches imported modules in `sys.modules`, so the server keeps executing
the version it imported at startup: the agent reports a successful fix, the
operator runs it, and the OLD code runs. Restarting a live app after every fix is
not an acceptable answer for a self-maintaining harness.

Reloading needs four steps, in order, and skipping any one of them silently
leaves stale code in place:

1. `importlib.invalidate_caches()` — the finder caches directory listings, so a
   brand-new module file may otherwise not be found at all.
2. DELETE the cached bytecode. This is the one that bites: a `.pyc` is validated
   against the source's (mtime, size), both at whole-second resolution. An agent
   edit that lands in the same second and happens to be the same byte length
   looks "unchanged", and Python reuses the stale bytecode — invalidate_caches()
   does NOT cover this, because it clears finder caches, not .pyc validation.
   Verified by test: with only steps 1/3/4, a same-length rewrite reloaded to the
   PREVIOUS code.
3. reload the module that actually changed.
4. reload the package `__init__`, because REGISTRY binds the function object
   (`from core.scrapers.x import retrieve`) at import time. Without this the
   registry still points at the OLD function even though the module is new.

This module must stay framework-free (no langgraph/langchain imports).
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

from core.observability import get_logger

log = get_logger("core.hot_reload")

# kind -> the package whose REGISTRY binds the per-source callables.
_PACKAGES = {"parser": "core.parsers", "scraper": "core.scrapers"}


def _drop_bytecode(source_path: str | None) -> None:
    """Remove a module's cached .pyc so the source is genuinely re-read.

    Without this, an edit with the same byte length landing in the same second as
    the last one is indistinguishable from no edit at all, and Python happily
    reuses the old bytecode.
    """
    if not source_path:
        return
    try:
        Path(importlib.util.cache_from_source(source_path)).unlink(missing_ok=True)
    except Exception:
        pass  # best effort; the reload below is still attempted


def reload_source_code(kind: str, source_key: str) -> dict:
    """Re-import a source's freshly written module and rebind its registry.

    Returns {reloaded, kind, source_key, error}. Never raises: a reload failure
    must not take down the request that triggered it — the caller reports it and
    the operator can still restart by hand.
    """
    package = _PACKAGES.get(kind)
    if package is None:
        return {"reloaded": False, "kind": kind, "source_key": source_key,
                "error": f"Unknown kind '{kind}'. Use {sorted(_PACKAGES)}."}

    module_name = f"{package}.{source_key}"
    try:
        importlib.invalidate_caches()          # (1) see the module docstring
        module = sys.modules.get(module_name)
        if module is None:
            importlib.import_module(module_name)   # never imported: a plain import is enough
        else:
            _drop_bytecode(getattr(module, "__file__", None))   # (2) the one that bites
            importlib.reload(module)           # (3)
        pkg = sys.modules.get(package)
        if pkg is not None:
            importlib.reload(pkg)              # (4) rebind REGISTRY to the new function
    except Exception as exc:
        return {
            "reloaded": False, "kind": kind, "source_key": source_key,
            "error": log.failure(
                operation="reload_source_code",
                code="HOT_RELOAD_FAILED",
                message=f"Could not reload {module_name} after it was rewritten.",
                remediation="Restart the app (poetry run agent-web) to pick up the new code.",
                context={"kind": kind, "source_key": source_key, "module": module_name},
                exc=exc,
            ),
        }

    log.event(
        operation="reload_source_code",
        code="HOT_RELOAD_OK",
        message=f"Reloaded {module_name} after the agent rewrote it.",
        context={"kind": kind, "source_key": source_key},
    )
    return {"reloaded": True, "kind": kind, "source_key": source_key}
