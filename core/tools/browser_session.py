# Template candidate: generic (tier 1) — no client-specific logic.
# See agent-harness-template/docs/promotion-log.md.
"""Generic Playwright browser-session management.

Uses a persistent, per-service browser profile on disk so a login session
(including cookies set after 2FA) survives across runs — avoids needing to
re-authenticate on every scheduled run. Sites without an API are the whole
reason this module exists; nothing here is specific to any one site.

This module must stay framework-free (no langgraph/langchain imports).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
from urllib.parse import urlparse, urlunparse

from playwright.sync_api import Page, sync_playwright

DEFAULT_PROFILE_ROOT = Path(".browser_profiles")


def _preferred_login_url(url: str) -> str:
    """Normalize known portal roots to the page users actually log in on.

    Buildium roots commonly redirect, but opening /manager directly is more
    reliable in persistent recovery sessions.
    """
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    path = parsed.path or ""
    if "managebuilding.com" in host and path in ("", "/"):
        return urlunparse(parsed._replace(path="/manager"))
    return url


def _open_login_page(page: Page, url: str) -> None:
    target = _preferred_login_url(url)
    page.bring_to_front()
    page.goto(target, wait_until="domcontentloaded", timeout=45_000)


def _find_pids(pattern: str) -> list[int]:
    """PIDs of live processes whose command line contains `pattern` (substring)."""
    try:
        result = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, timeout=5)
    except Exception:
        return []
    if result.returncode not in (0, 1):  # 1 == no matches, still a clean run
        return []
    return [int(pid) for pid in result.stdout.split() if pid.strip().isdigit()]


def _kill_pids(pids: list[int], exclude: set[int] | None = None) -> None:
    exclude = exclude or set()
    targets = [pid for pid in pids if pid not in exclude]
    if not targets:
        return
    for pid in targets:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            continue
    time.sleep(1.0)
    for pid in targets:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            continue


def reset_profile(service_key: str, profile_root: Path = DEFAULT_PROFILE_ROOT) -> None:
    """Forcefully clear any process still holding this service's persistent
    browser profile — an orphaned recovery worker and/or its Chromium tree —
    before a new one is launched.

    Without this, attempting to launch against an already-open profile makes
    Chromium silently open a NEW blank tab in the existing (orphaned) window
    and then report a lock error; retrying that launch just piles up more
    blank tabs rather than ever succeeding. One clean kill-then-launch avoids
    that entirely.
    """
    profile_dir = str(_profile_dir(service_key, profile_root))
    _kill_pids(_find_pids(f"core.tools.login_recovery_worker {service_key} "), exclude={os.getpid()})
    _kill_pids(_find_pids(f"user-data-dir={profile_dir}"))


def _profile_dir(service_key: str, profile_root: Path) -> Path:
    profile_dir = profile_root / service_key
    profile_dir.mkdir(parents=True, exist_ok=True)
    return profile_dir


@contextmanager
def launch(
    service_key: str,
    headless: bool = True,
    profile_root: Path = DEFAULT_PROFILE_ROOT,
) -> Iterator[Page]:
    """Launch (or resume) a persistent browser context for `service_key`.

    Cookies/local storage persist in the on-disk profile across calls, so a
    session established via bootstrap_login() survives into later headless
    runs, without needing to repeat login/2FA every time.
    """
    profile_dir = _profile_dir(service_key, profile_root)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(profile_dir), headless=headless
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            yield page
        finally:
            context.close()


def bootstrap_login(
    service_key: str,
    url: str,
    profile_root: Path = DEFAULT_PROFILE_ROOT,
) -> None:
    """Open a visible browser at `url` for the operator to log in manually
    (including any 2FA prompt), then save that session to the persistent
    profile for reuse by later headless runs.
    """
    with launch(service_key, headless=False, profile_root=profile_root) as page:
        _open_login_page(page, url)
        input(
            f"Complete login for '{service_key}' in the opened browser window, "
            "then press Enter here to save the session..."
        )


def bootstrap_login_until_window_closed(
    service_key: str,
    url: str,
    profile_root: Path = DEFAULT_PROFILE_ROOT,
) -> None:
    """Open a visible persistent browser for manual login and keep it alive
    until the user closes all pages/windows. Closing the context then persists
    cookies/session for future headless runs.
    """
    profile_dir = _profile_dir(service_key, profile_root)
    # Clear out any orphaned worker/browser still holding this profile before
    # attempting to launch — a single clean attempt only. Do NOT retry a
    # failed launch here: Chromium's singleton behavior opens a new blank tab
    # in whatever browser already owns the profile on every attempt, so
    # retrying against a still-locked profile piles up blank tabs instead of
    # ever succeeding.
    reset_profile(service_key, profile_root)

    with sync_playwright() as playwright:
        try:
            context = playwright.chromium.launch_persistent_context(
                str(profile_dir), headless=False
            )
        except Exception as exc:
            raise RuntimeError(
                "Recovery browser could not start because this source's browser profile is still "
                "in use. Close any open Chromium/Chrome windows for this source and retry recovery. "
                f"Details: {exc}"
            ) from exc

        try:
            # Always use a fresh page for recovery to avoid reusing stale tabs
            # from prior sessions that can stay blank or unfocused.
            page = context.new_page()
            _open_login_page(page, url)
            # launch_persistent_context always opens its own default blank tab
            # in addition to any page we create. Close it now that our page
            # exists — otherwise it lingers invisibly after the user closes
            # the login tab they were looking at, `context.pages` never goes
            # empty, and this function (and the session-saved signal) never
            # returns even though the user did everything right.
            for stale in list(context.pages):
                if stale is not page:
                    try:
                        stale.close()
                    except Exception:
                        pass
            # Watch the login `page` object itself for its close event rather
            # than polling `context.pages` for emptiness. On macOS, closing the
            # last window doesn't quit the browser app (standard "close but
            # don't quit" behavior) — when Chromium is left with zero windows
            # it can auto-spawn a fresh blank tab (the OS "reopen" signal),
            # which keeps `context.pages` non-empty forever and made this loop
            # hang indefinitely even though the user closed the real page.
            # Tracking our own page reference sidesteps that entirely: any
            # phantom tabs Chromium creates afterward are irrelevant.
            while True:
                try:
                    if page.is_closed():
                        break
                except Exception:
                    # Browser/connection died out from under us — treat as closed.
                    break
                time.sleep(0.5)
        finally:
            # context.close() sends a graceful CDP Browser.close and waits for
            # the underlying Chromium process to actually exit. On macOS that
            # can hang indefinitely: closing the last window doesn't quit the
            # app (see the reopen note above), so the process the CDP command
            # is waiting on never goes away, and this worker — and the
            # session-saved signal the UI is polling for — would hang forever
            # even though the user did everything right. Give the graceful
            # close a few seconds, then forcibly kill the profile's process
            # tree if it hasn't finished (the profile is already flushed to
            # disk by then; killing after close() was requested is safe).
            done = threading.Event()

            def _graceful_close() -> None:
                try:
                    context.close()
                except Exception:
                    pass
                finally:
                    done.set()

            threading.Thread(target=_graceful_close, daemon=True).start()
            if not done.wait(timeout=5.0):
                _kill_pids(_find_pids(f"user-data-dir={profile_dir}"))
