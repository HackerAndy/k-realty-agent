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

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import os
from pathlib import Path
import signal
import subprocess
import threading
import time

from playwright.sync_api import Page, sync_playwright

from core import progress

DEFAULT_PROFILE_ROOT = Path(".browser_profiles")


def _open_login_page(page: Page, url: str) -> None:
    """Open exactly the URL given. This module is tier-1 GENERIC: it must not
    know any portal's URL layout. A platform's quirks (e.g. Buildium serving its
    resident site at the bare root) belong in that platform's module, and the
    right per-source sign-in URL belongs in services.yaml."""
    page.bring_to_front()
    page.goto(url, wait_until="domcontentloaded", timeout=45_000)


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
    profile_dir = _profile_dir(service_key, profile_root)
    _kill_pids(_find_pids(f"core.tools.login_recovery_worker {service_key} "), exclude={os.getpid()})
    _kill_pids(_find_pids(_profile_pattern(profile_dir)))


def _profile_dir(service_key: str, profile_root: Path) -> Path:
    profile_dir = profile_root / service_key
    profile_dir.mkdir(parents=True, exist_ok=True)
    return profile_dir


def _profile_pattern(profile_dir: Path) -> str:
    """The pgrep pattern that matches the browser holding this profile.

    Chromium is launched with an ABSOLUTE --user-data-dir (Playwright resolves
    whatever path it is given), so a pattern built from a relative path matches
    nothing — which reads as "the browser is gone" the instant it starts.
    """
    return f"user-data-dir={Path(profile_dir).resolve()}"


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
    # Browser startup is seconds of silence to the operator; name it while it runs.
    progress.step("browser_launch", "Start browser session")
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(profile_dir), headless=headless
        )
        progress.done("browser_launch")
        try:
            page = context.pages[0] if context.pages else context.new_page()
            yield page
        finally:
            progress.step("browser_close", "Close browser session")
            context.close()
            progress.done("browser_close")


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


def _wait_for_the_operator(
    profile_dir: Path,
    max_wait_s: float,
    done: "Callable[[], str | None]",
    tick: "Callable[[], None] | None" = None,
) -> str:
    """Poll until `done()` names a reason to stop. Returns that reason.

    Two stop conditions are added to whatever `done` watches, because the
    obvious "the window closed" signal isn't enough on its own:

    * OS truth: no Chromium process still holds this profile => the browser is
      gone, whatever CDP thinks (an abrupt Cmd-Q never delivers a close event).
      Two consecutive empty checks, so a momentary pgrep miss can't end the
      session early — and absence is only trusted once presence has been seen,
      since a pattern that never matches means "I can't see it", not "it's gone".
    * An absolute deadline, so this can never hang indefinitely.

    `tick` runs on each poll WHILE the browser is still alive — the hook a
    recorder needs, since anything it wants off a page (actions, structure) can
    no longer be read once the operator has closed the window.
    """
    deadline = time.monotonic() + max_wait_s
    pattern = _profile_pattern(profile_dir)
    gone_checks = 0
    seen_alive = False
    while True:
        reason = done()
        if reason:
            return reason
        if _find_pids(pattern):
            seen_alive = True
            gone_checks = 0
        elif seen_alive:
            gone_checks += 1
            if gone_checks >= 2:
                return "no browser process left for this profile"
        if tick is not None:
            try:
                tick()
            except Exception:
                pass   # a snapshot failing must never end the operator's session
        if time.monotonic() > deadline:
            return "deadline"
        time.sleep(0.5)


def wait_until_window_closed(
    page: Page,
    profile_dir: Path,
    max_wait_s: float = 15 * 60,
    on_poll: "Callable[[Page], None] | None" = None,
) -> str:
    """Block until the operator closes THIS page. Returns the reason.

    For a single-page errand (log in here, then close it). Anything where the
    operator navigates freely — and may open or close tabs on the way — wants
    wait_until_browsing_done() instead, which doesn't tie the session's life to
    one tab.
    """

    def done() -> str | None:
        try:
            return "page closed" if page.is_closed() else None
        except Exception:
            return "browser connection lost"

    return _wait_for_the_operator(
        profile_dir,
        max_wait_s,
        done,
        None if on_poll is None else lambda: on_poll(page),
    )


def _live_pages(context) -> list[Page]:
    """The pages the operator is actually looking at.

    A blank tab doesn't count. Chromium opens one at launch, and on macOS can
    auto-spawn another when the last window closes ("reopen" behaviour), so
    counting blanks would mean the session either ends before they start or
    never ends at all.
    """
    live: list[Page] = []
    for page in list(context.pages):
        try:
            if page.is_closed():
                continue
            url = page.url or ""
        except Exception:
            continue
        if url and not url.startswith(("about:", "chrome://", "chrome-error://")):
            live.append(page)
    return live


def wait_until_browsing_done(
    context,
    profile_dir: Path,
    max_wait_s: float = 15 * 60,
    on_poll: "Callable[[list[Page]], None] | None" = None,
) -> str:
    """Block until the operator has closed everything they opened.

    Watching a single page object is wrong for a demonstration: the operator
    opens a new tab to reach the portal, a link opens one for them, or they
    close the blank tab the browser started with — and the tab we happened to
    hold a reference to closes while the real work is still on screen. That
    ended demonstrations seconds after they began, with nothing captured.

    So the session lives as long as ANY real page does, and `on_poll` gets all
    of them — the operator's demonstration is whatever they did across tabs.
    """
    state: dict = {"pages": [], "seen": False}

    def done() -> str | None:
        try:
            pages = _live_pages(context)
        except Exception:
            return "browser connection lost"
        state["pages"] = pages
        if pages:
            state["seen"] = True
            return None
        # No real page. Before they've opened one, that's just the blank tab
        # they haven't typed into yet — keep waiting.
        return "all windows closed" if state["seen"] else None

    return _wait_for_the_operator(
        profile_dir,
        max_wait_s,
        done,
        None if on_poll is None else lambda: on_poll(state["pages"]),
    )


def close_context(context, profile_dir: Path, timeout_s: float = 5.0) -> None:
    """Close a headed context without ever hanging on it.

    context.close() sends a graceful CDP Browser.close and waits for the
    Chromium process to actually exit. On macOS that can wait forever: closing
    the last window doesn't quit the app, so the process it's waiting on never
    goes away — and the worker (plus the UI polling it) hangs even though the
    operator did everything right. So arm a watchdog that kills the profile's
    processes if the close is still waiting after `timeout_s` — the process it
    was blocked on is then gone and the close returns. The profile is already
    on disk by that point, so killing is safe.

    The close itself must run on THIS thread: sync Playwright objects belong to
    the greenlet that made them, and calling close() from a helper thread only
    raises (which, silently caught, meant the close never happened at all — and
    a demonstration's HAR is written by that close).
    """
    watchdog = threading.Timer(
        timeout_s, lambda: _kill_pids(_find_pids(_profile_pattern(profile_dir)))
    )
    watchdog.daemon = True
    watchdog.start()
    try:
        context.close()
    except Exception:
        pass
    finally:
        watchdog.cancel()


def bootstrap_login_until_window_closed(
    service_key: str,
    url: str,
    profile_root: Path = DEFAULT_PROFILE_ROOT,
    max_wait_s: float = 15 * 60,
) -> None:
    """Open a visible persistent browser for manual login and keep it alive
    until the user closes the window OR quits the browser. Closing the context
    then persists cookies/session for future headless runs.

    Gives up after `max_wait_s` rather than waiting forever, so the worker (and
    the UI polling it) can never hang indefinitely.
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
            #
            # page.is_closed() alone is NOT enough: it reads locally cached state
            # that only updates when Chromium delivers a CDP close event. Quit the
            # browser abruptly (Cmd-Q, crash, force quit) and that event never
            # arrives — is_closed() stays False forever and this loop spun while
            # the operator had in fact logged in and quit, exactly as instructed.
            # So also consult OS truth: no Chromium process is still holding this
            # profile => the browser is gone, whatever CDP thinks. Plus an absolute
            # deadline, so this can never hang indefinitely again.
            reason = wait_until_window_closed(page, profile_dir, max_wait_s)
            # Written to the worker's log file, so a future hang is diagnosable
            # instead of leaving a 0-byte log behind.
            print(f"[browser_session] recovery for '{service_key}' finished: {reason}", flush=True)
            if reason == "deadline":
                raise RuntimeError(
                    f"Gave up waiting for the '{service_key}' login after {int(max_wait_s / 60)} minutes. "
                    "The session was not saved. Retry recovery and close the browser window when done."
                )
        finally:
            close_context(context, profile_dir)
