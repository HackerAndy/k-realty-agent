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
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

DEFAULT_PROFILE_ROOT = Path(".browser_profiles")


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
        page.goto(url)
        input(
            f"Complete login for '{service_key}' in the opened browser window, "
            "then press Enter here to save the session..."
        )
