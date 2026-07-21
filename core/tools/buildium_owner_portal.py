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

from playwright.sync_api import Page

from core.tools.credential_store import CredentialStore


class BuildiumLoginError(RuntimeError):
    pass


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
    store = CredentialStore()
    creds = store.get(service_key)

    page.goto(portal_url, timeout=timeout_ms, wait_until="domcontentloaded")

    email_field = page.get_by_label("Email address")
    password_field = page.get_by_label("Password")
    sign_in_button = page.get_by_role("button", name="Sign in", exact=True)

    email_field.wait_for(state="visible", timeout=timeout_ms)
    email_field.fill(creds["username"])
    password_field.fill(creds["password"])
    sign_in_button.click()

    # A successful login navigates away from the login form. If the email
    # field is still attached after a beat, login didn't go through (bad
    # credentials, or a 2FA/verification prompt appeared instead).
    try:
        email_field.wait_for(state="detached", timeout=timeout_ms)
    except Exception as exc:
        raise BuildiumLoginError(
            f"Login for '{service_key}' did not complete — check stored "
            "credentials, or complete a 2FA/verification prompt manually via "
            "browser_session.bootstrap_login()."
        ) from exc
