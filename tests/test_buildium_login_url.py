"""Automated login and manual recovery must open the SAME page.

Field failure this pins: Buildium serves its RESIDENT site at the bare root.
A scraper naturally passes its API base (the bare root), so login() landed on
/Resident/public/home — no manager sign-in form — and raised "form not detected".
Recovery, which normalized to /manager, signed in correctly... after which the
retry went back to the bare root and failed identically. Endless loop, and the
saved session was never at fault.

Verified live at the time:
    bare root -> /Resident/public/home                     (resident site)
    /manager  -> /manager/public/authentication/login?...  (manager sign-in)
"""

import pytest

from core.tools import buildium_owner_portal


@pytest.mark.parametrize(
    "given, expected",
    [
        # The bare root is the trap — it serves the resident site.
        ("https://epicpropertymanagement.managebuilding.com",
         "https://epicpropertymanagement.managebuilding.com/manager"),
        ("https://epicpropertymanagement.managebuilding.com/",
         "https://epicpropertymanagement.managebuilding.com/manager"),
        # An explicit path is the caller's choice; don't second-guess it.
        ("https://epicpropertymanagement.managebuilding.com/manager",
         "https://epicpropertymanagement.managebuilding.com/manager"),
        ("https://epicpropertymanagement.managebuilding.com/manager/app/accounting",
         "https://epicpropertymanagement.managebuilding.com/manager/app/accounting"),
        # Non-Buildium hosts are left completely alone.
        ("https://example.com", "https://example.com"),
        ("https://bank.example.com/login", "https://bank.example.com/login"),
    ],
)
def test_preferred_login_url(given, expected):
    assert buildium_owner_portal.preferred_login_url(given) == expected


class _FakeLocator:
    def wait_for(self, **kwargs):
        return None

    def fill(self, value):
        return None

    def click(self):
        return None


class _FakePage:
    """Records where it was told to go, and pretends the sign-in form is present."""

    def __init__(self, url_after_goto):
        self.url = ""
        self._after = url_after_goto
        self.visited = []

    def goto(self, url, **kwargs):
        self.visited.append(url)
        self.url = self._after

    def get_by_label(self, name):
        return _FakeLocator()

    def get_by_role(self, role, name=None, exact=False):
        return _FakeLocator()

    def locator(self, selector):
        return _FakeLocator()

    def title(self):
        return "Sign in"


def test_login_navigates_to_the_manager_page_not_the_bare_root(monkeypatch):
    """The actual regression: login() must not open the resident site."""
    monkeypatch.setattr(
        buildium_owner_portal, "CredentialStore",
        lambda: type("S", (), {"get": staticmethod(lambda k: {"username": "u", "password": "p"})})(),
    )
    monkeypatch.setattr(buildium_owner_portal, "_find_email_field", lambda page, t: _FakeLocator())

    page = _FakePage(url_after_goto="https://epicpropertymanagement.managebuilding.com/manager/public/authentication/login")
    buildium_owner_portal.login(
        page, "https://epicpropertymanagement.managebuilding.com", "epic_property_management"
    )

    assert page.visited == ["https://epicpropertymanagement.managebuilding.com/manager"]
    assert "/Resident" not in page.visited[0]


def test_login_short_circuits_when_the_saved_session_is_still_good(monkeypatch):
    """Landing on /manager/app means the persistent profile is still signed in, so
    no credentials are needed — a valid session must not depend on the vault."""
    def explode(_key):
        raise AssertionError("must not read credentials when already signed in")

    monkeypatch.setattr(buildium_owner_portal, "CredentialStore",
                        lambda: type("S", (), {"get": staticmethod(explode)})())

    page = _FakePage(url_after_goto="https://epicpropertymanagement.managebuilding.com/manager/app/homepage/dashboard")
    buildium_owner_portal.login(page, "https://epicpropertymanagement.managebuilding.com", "epic")

    assert page.visited == ["https://epicpropertymanagement.managebuilding.com/manager"]


def test_resident_landing_reports_where_the_browser_actually_was(monkeypatch):
    """The message that let us find this must keep naming the URL."""
    monkeypatch.setattr(
        buildium_owner_portal, "CredentialStore",
        lambda: type("S", (), {"get": staticmethod(lambda k: {"username": "u", "password": "p"})})(),
    )
    monkeypatch.setattr(buildium_owner_portal, "_find_email_field", lambda page, t: None)

    page = _FakePage(url_after_goto="https://epicpropertymanagement.managebuilding.com/Resident/public/home")
    with pytest.raises(buildium_owner_portal.BuildiumLoginError) as exc:
        buildium_owner_portal.login(page, "https://x.managebuilding.com/somewhere", "epic")

    assert "/Resident/public/home" in str(exc.value)
