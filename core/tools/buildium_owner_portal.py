# Template candidate: platform-reusable (tier 2) — reusable for any future
# client whose property manager also runs on Buildium (managebuilding.com),
# not universally generic. See agent-harness-template/docs/promotion-log.md.
"""Login helper for Buildium-powered owner/manager portals.

Buildium (managebuilding.com) is a widely used property-management SaaS;
many property management companies white-label a Buildium instance for
their owners/residents. This module handles the login flow only — anything
past login (navigation, data extraction) is specific to each property
manager's actual portal content and belongs in an agent-authored scraper
(see core/scrapers/epic_property_management.py for K-Realty's).

This module must stay framework-free (no langgraph/langchain imports).
"""

from __future__ import annotations

import time
from urllib.parse import urlparse, urlunparse

from playwright.sync_api import Page

from core import progress
from core.tools.credential_store import CredentialStore


class BuildiumLoginError(RuntimeError):
    pass


def preferred_login_url(url: str) -> str:
    """Normalize a Buildium portal root to the MANAGER sign-in page.

    Buildium serves its RESIDENT site at the bare root: opening
    `https://<client>.managebuilding.com` lands on /Resident/public/home, which
    has no manager sign-in form at all, while /manager redirects to the real
    manager login. A scraper naturally passes its API base (the bare root), so
    without this, automated login sat on the resident page failing to find a
    form — while manual recovery signed in correctly on /manager. That mismatch
    produced an endless recover/retry loop.

    Platform knowledge lives HERE (tier 2), not in the generic browser session:
    an explicit path is always left alone, and non-Buildium hosts are untouched.
    """
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    path = parsed.path or ""
    if "managebuilding.com" in host and path in ("", "/"):
        return urlunparse(parsed._replace(path="/manager"))
    return url


def _looks_authenticated(page: Page) -> bool:
    """Best-effort signal that the persistent profile is already signed in."""
    url = (page.url or "").lower()
    return "/manager/app" in url


def _find_email_field(page: Page, timeout_ms: int):
    """Try known Buildium login form selectors, newest/most-specific first."""
    candidates = [
        page.get_by_label("Email address"),
        page.get_by_label("Email"),
        page.locator("input[type='email']"),
    ]

    deadline = time.time() + (timeout_ms / 1000.0)
    while time.time() < deadline:
        for candidate in candidates:
            try:
                candidate.wait_for(state="visible", timeout=500)
                return candidate
            except Exception:
                continue
        if _looks_authenticated(page):
            return None
    return None


def login(page: Page, portal_url: str, service_key: str, timeout_ms: int = 45000) -> None:
    """Log in to a Buildium-powered owner/manager portal.

    `service_key` is the key this client's credentials were stored under
    (see scripts/manage_secrets.py). Assumes email+password login, not
    Google SSO — verified live against the real login form before writing
    this; if the portal ever only offers Google sign-in, this raises rather
    than silently misbehaving.

    Note: this site never reaches Playwright's "networkidle" state (likely
    background polling), so navigation uses "domcontentloaded" plus an
    explicit wait for the email field instead.
    """
    # Normalize to the MANAGER login page. A scraper naturally passes its API
    # base (the bare root), but Buildium serves the RESIDENT site there — which
    # has no manager sign-in form, so login failed with "form not detected" and
    # then looped: recovery signed in on /manager while this kept reloading the
    # resident page. Same helper the recovery path uses, so they can't diverge.
    login_url = preferred_login_url(portal_url)
    progress.step("portal_open", "Open the portal")
    page.goto(login_url, timeout=timeout_ms, wait_until="domcontentloaded")
    progress.done("portal_open", details={"url": login_url})

    if _looks_authenticated(page):
        # The common case: the saved session is still good, so no sign-in at all.
        progress.step("sign_in", "Reuse the saved session (already signed in)", status="success")
        return

    progress.step("sign_in", "Sign in")

    email_field = _find_email_field(page, timeout_ms)
    if email_field is None:
        if _looks_authenticated(page):
            return
        # Say WHERE the browser actually was. "Form not detected" on its own is
        # ambiguous — already signed in, still rendering, or redirected all look
        # identical — and sent the operator hunting a redesign that never happened.
        try:
            where = f"{page.url} (title: {page.title()!r})"
        except Exception:
            where = "unknown — the page could not be inspected"
        progress.failed("sign_in", error=f"sign-in form not found; browser was at {where}")
        raise BuildiumLoginError(
            f"Login form for '{service_key}' was not detected. The browser was at: {where}. "
            "If that is not the sign-in page, the portal redirected; if it is, the form may "
            "still have been rendering or the page has changed."
        )

    # Read credentials only now that we know a sign-in is actually required — a
    # still-valid saved session shouldn't depend on them being in the vault.
    creds = CredentialStore().get(service_key)

    password_field = page.get_by_label("Password")
    sign_in_button = page.get_by_role("button", name="Sign in", exact=True)

    email_field.fill(creds["username"])
    password_field.fill(creds["password"])
    sign_in_button.click()

    # A successful login navigates away from the login form. If the email
    # field is still attached after a beat, login didn't go through (bad
    # credentials, or a 2FA/verification prompt appeared instead).
    try:
        email_field.wait_for(state="detached", timeout=timeout_ms)
        progress.done("sign_in")
    except Exception as exc:
        progress.failed("sign_in", error="sign-in did not complete (credentials or 2FA)")
        raise BuildiumLoginError(
            f"Login for '{service_key}' did not complete — check stored "
            "credentials, or complete a 2FA/verification prompt manually via "
            "browser_session.bootstrap_login()."
        ) from exc
