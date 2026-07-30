"""Connecting an inbox from the app instead of a terminal.

Gmail is the one sign-in that can't be a username and a password (Workspace
killed basic auth for IMAP in May 2025), so the operator's path is: upload the
OAuth client JSON → click Allow in the browser Google opens. That is ALL an inbox
is — access. What to search for belongs to each source that arrives through it
(see the search half, below), because one mailbox carries many sources.

Each step has a way to go wrong quietly, which is what these pin:

  * the uploaded client secret is deleted once consent finishes, however it
    finishes — a second plaintext copy of a client secret is pure downside;
  * a source can only be pointed at an inbox that is actually signed in;
  * consent runs out-of-process, so "no worker" must not read as "connected".
"""

import json
from pathlib import Path

import pytest

import core.tools.credential_store as cs
from interfaces import mcp_tools
from core.tools.service_manifest import EmailSearch, Service


@pytest.fixture
def sources(monkeypatch):
    """A fake manifest — never the operator's real services.yaml."""
    services = [
        Service(key="inbox", label="Email", input_type="email_trigger"),
        Service(key="epic", label="Epic", parser="core/parsers/epic.py"),
        Service(key="unbuilt", label="No parser yet"),
    ]
    monkeypatch.setattr(mcp_tools, "_load_services", lambda: list(services))
    return services


@pytest.fixture
def no_token(monkeypatch, tmp_path):
    """An empty encrypted vault, so nothing is 'connected' by accident."""
    from core.tools import email_oauth

    monkeypatch.setenv("AGENT_SECRET_KEY", cs.generate_key())
    store = cs.CredentialStore(tmp_path / "creds.enc")
    monkeypatch.setattr(cs, "CredentialStore", lambda *a, **k: store)
    # email_oauth imported the class by name, so patch it there too — otherwise
    # the test reads the operator's real vault.
    monkeypatch.setattr(email_oauth, "CredentialStore", lambda *a, **k: store)
    return store


# ── What the screen shows ────────────────────────────────────────────────────

def test_an_inbox_with_no_token_reads_as_not_connected(sources, no_token):
    st = mcp_tools.email_status("inbox")
    assert st["connected"] is False
    assert st["account_email"] is None
    assert st["searched_by"] == []


def test_an_inbox_reports_the_sources_that_search_it(sources, no_token, monkeypatch):
    """One mailbox, several sources. The inbox screen shows who uses it — it does
    not own their search terms, which is the whole point of the split."""
    sources.append(Service(key="bank", label="Bank", parser="p",
                           email_search=EmailSearch(carrier="inbox", from_address="bank@x.com")))
    sources[1].email_search = EmailSearch(carrier="inbox", subject_contains="Owner Statement")

    st = mcp_tools.email_status("inbox")

    assert {s["key"] for s in st["searched_by"]} == {"epic", "bank"}
    assert st["searched_by"][0]["search"]["subject_contains"] == "Owner Statement"


def test_an_unknown_source_is_refused(sources, no_token):
    with pytest.raises(mcp_tools.ToolError, match="Unknown source"):
        mcp_tools.email_status("nope")


# ── Saying what to look for — configuration, and it lives on the SOURCE ──────

@pytest.fixture
def manifest(monkeypatch):
    """Capture writes instead of touching the real services.yaml."""
    saved = {}

    class FakeManifest:
        def set_email_search(self, key, search):
            saved[key] = search

        def clear_email_search(self, key):
            saved[key] = None

    monkeypatch.setattr(mcp_tools, "ServiceManifest", FakeManifest)
    monkeypatch.setattr(mcp_tools, "_inbox_connected", lambda key: key == "inbox")
    return saved


def test_the_search_is_saved_on_the_source_not_the_inbox(sources, no_token, manifest):
    """It used to be stored on the inbox, which capped a connected account at one
    source and put ingestion settings in the credentials screen."""
    mcp_tools.save_email_search("epic", carrier="inbox",
                                from_address=" statements@epic.com ",
                                subject_contains="Owner Statement",
                                attachment_suffix=".pdf", newer_than_days=30)

    assert "inbox" not in manifest, "the inbox holds the sign-in, nothing else"
    cfg = manifest["epic"]
    assert isinstance(cfg, EmailSearch) and cfg.carrier == "inbox"
    assert cfg.from_address == "statements@epic.com", "surrounding spaces are a typo, not a filter"
    assert cfg.newer_than_days == 30


def test_one_inbox_can_carry_two_sources(sources, no_token, manifest):
    """The reason the search moved off the inbox in the first place."""
    sources.append(Service(key="bank", label="Bank", parser="p"))

    mcp_tools.save_email_search("epic", carrier="inbox", subject_contains="Owner Statement")
    mcp_tools.save_email_search("bank", carrier="inbox", from_address="bank@x.com")

    assert manifest["epic"].carrier == manifest["bank"].carrier == "inbox"
    assert manifest["epic"].subject_contains != manifest["bank"].from_address


def test_blank_criteria_become_absent_rather_than_empty_strings(sources, no_token, manifest):
    """An empty subject filter would otherwise search for the empty string."""
    mcp_tools.save_email_search("epic", carrier="inbox", from_address="",
                                subject_contains="   ", newer_than_days=None)

    cfg = manifest["epic"]
    assert cfg.from_address is None and cfg.subject_contains is None
    assert cfg.newer_than_days is None


def test_an_inbox_that_is_not_signed_in_is_refused(sources, no_token, manifest, monkeypatch):
    """Otherwise the route looks configured and every fetch fails."""
    monkeypatch.setattr(mcp_tools, "_inbox_connected", lambda key: False)

    with pytest.raises(mcp_tools.ToolError, match="isn't signed in"):
        mcp_tools.save_email_search("epic", carrier="inbox")
    assert not manifest, "nothing should be written when the inbox can't be read"


def test_pointing_at_something_that_is_not_an_inbox_is_refused(sources, no_token, manifest):
    with pytest.raises(mcp_tools.ToolError, match="isn't an inbox"):
        mcp_tools.save_email_search("epic", carrier="unbuilt")


def test_pointing_at_an_inbox_that_does_not_exist_is_refused(sources, no_token, manifest):
    with pytest.raises(mcp_tools.ToolError, match="Unknown inbox"):
        mcp_tools.save_email_search("epic", carrier="ghost")


def test_an_inbox_cannot_arrive_by_email(sources, no_token, manifest):
    """An inbox is a way in, not a body of data — it has nothing to parse."""
    with pytest.raises(mcp_tools.ToolError, match="is an inbox"):
        mcp_tools.save_email_search("inbox", carrier="inbox")


def test_removing_the_route_leaves_the_inbox_connected(sources, no_token, manifest):
    """Dropping one source's email route must not sign the mailbox out — others
    may still arrive through it."""
    sources[1].email_search = EmailSearch(carrier="inbox")

    mcp_tools.remove_email_search("epic")

    assert manifest["epic"] is None
    assert mcp_tools.email_status("inbox")["connected"] is False   # unchanged by the removal


def test_the_route_screen_offers_the_inboxes_there_are(sources, no_token, monkeypatch):
    monkeypatch.setattr(mcp_tools, "_inbox_connected", lambda key: key == "inbox")
    monkeypatch.setattr(mcp_tools, "_inbox_account", lambda key: "me@k.org")

    route = mcp_tools.source_email_route("epic")

    assert [i["key"] for i in route["inboxes"]] == ["inbox"]
    assert route["inboxes"][0]["connected"] is True
    assert route["search"] is None, "this source has no email route yet"


# ── Consent, which runs in its own process ───────────────────────────────────

def test_no_worker_and_no_token_reads_as_idle_not_connected(sources, no_token):
    mcp_tools._CONSENT_PROCS.pop("inbox", None)
    assert mcp_tools.gmail_consent_status("inbox")["status"] == "idle"


def test_no_worker_but_a_stored_token_reads_as_completed(sources, no_token):
    mcp_tools._CONSENT_PROCS.pop("inbox", None)
    no_token.set("email_oauth::inbox", refresh_token="rt", account_email="me@k.org")

    st = mcp_tools.gmail_consent_status("inbox")
    assert st["status"] == "completed" and st["account_email"] == "me@k.org"


def test_consent_needs_the_client_json_to_be_there(sources, no_token, tmp_path):
    with pytest.raises(mcp_tools.ToolError, match="OAuth client JSON"):
        mcp_tools.start_gmail_consent("inbox", str(tmp_path / "missing.json"))


def test_the_uploaded_client_secret_is_deleted_once_consent_succeeds(tmp_path, monkeypatch):
    """It holds a client secret and its contents are in the vault by then."""
    from core.tools import email_oauth, oauth_consent_worker

    client = tmp_path / "client.json"
    client.write_text("{}")
    status = tmp_path / "consent.json"
    monkeypatch.setattr(email_oauth, "run_consent", lambda k, p: "me@k.org")

    assert oauth_consent_worker.run("inbox", client, status) == 0
    assert not client.exists()
    assert json.loads(status.read_text())["account_email"] == "me@k.org"


def test_the_client_secret_is_deleted_even_when_consent_fails(tmp_path, monkeypatch):
    from core.tools import email_oauth, oauth_consent_worker

    client = tmp_path / "client.json"
    client.write_text("{}")
    status = tmp_path / "consent.json"

    def boom(key, path):
        raise RuntimeError("operator closed the window")

    monkeypatch.setattr(email_oauth, "run_consent", boom)

    assert oauth_consent_worker.run("inbox", client, status) == 1
    assert not client.exists(), "an abandoned consent must not leave a secret on disk"
    payload = json.loads(status.read_text())
    assert payload["status"] == "failed" and "closed the window" in payload["error"]


# ── The upload endpoint ──────────────────────────────────────────────────────

@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from interfaces import rest_server

    monkeypatch.setattr(rest_server, "REPO_ROOT", tmp_path)
    return TestClient(rest_server.app), tmp_path


def test_an_oauth_client_json_lands_in_secrets_readable_only_by_the_owner(client):
    api, root = client
    body = {"installed": {"client_id": "x.apps.googleusercontent.com", "client_secret": "shh"}}

    res = api.post("/api/upload_oauth_client/inbox",
                   files={"file": ("client.json", json.dumps(body), "application/json")})

    assert res.status_code == 200
    dest = root / ".secrets" / "oauth-client-inbox.json"
    assert dest.exists() and res.json()["client_secret_path"] == str(dest)
    assert oct(dest.stat().st_mode)[-3:] == "600", "it carries a client secret"


def test_the_wrong_file_is_rejected_here_rather_than_deep_inside_googles_flow(client):
    """Uploading the wrong JSON is easy; the error Google gives for it is opaque."""
    api, root = client

    res = api.post("/api/upload_oauth_client/inbox",
                   files={"file": ("notes.json", json.dumps({"hello": "world"}), "application/json")})

    assert res.status_code == 400
    assert "OAuth client" in res.json()["detail"]["message"]
    assert not (root / ".secrets" / "oauth-client-inbox.json").exists()


def test_a_file_that_is_not_json_at_all_is_rejected(client):
    api, _ = client
    res = api.post("/api/upload_oauth_client/inbox",
                   files={"file": ("statement.pdf", b"%PDF-1.4 not json", "application/pdf")})
    assert res.status_code == 400


def test_a_source_key_cannot_escape_the_secrets_directory(client):
    """The key reaches a filename, so path traversal is worth pinning."""
    api, root = client
    body = json.dumps({"installed": {"client_id": "x"}})

    res = api.post("/api/upload_oauth_client/..%2F..%2Fescaped",
                   files={"file": ("client.json", body, "application/json")})

    if res.status_code == 200:
        written = root / ".secrets" / "oauth-client-escaped.json"
        assert written.exists(), "the separators must be stripped, not honoured"
    assert not (root.parent / "escaped").exists()


# ── Managing the inboxes themselves ──────────────────────────────────────────

@pytest.fixture
def registry(monkeypatch, sources):
    """A fake manifest that add/remove actually mutate."""
    class FakeManifest:
        def add(self, service):
            sources.append(service)

        def remove(self, key):
            sources[:] = [s for s in sources if s.key != key]

        def set_email_search(self, key, search):
            for i, s in enumerate(sources):
                if s.key == key:
                    sources[i] = s.model_copy(update={"email_search": search})

    monkeypatch.setattr(mcp_tools, "ServiceManifest", FakeManifest)
    return sources


def test_a_second_inbox_can_be_added(registry, no_token):
    """Several mailboxes is a normal state — a business one and a personal one."""
    st = mcp_tools.add_inbox("Rentals mailbox")

    added = next(s for s in registry if s.key == "rentals_mailbox")
    assert added.input_type == "email_trigger" and added.provider == "gmail"
    assert added.parser is None, "an inbox reads nothing itself"
    assert st["connected"] is False, "it still has to be signed in"


def test_a_nameless_inbox_is_refused(registry, no_token):
    with pytest.raises(mcp_tools.ToolError, match="name"):
        mcp_tools.add_inbox("   ")


def test_adding_an_inbox_that_is_already_here_is_refused(registry, no_token):
    mcp_tools.add_inbox("Rentals mailbox")
    with pytest.raises(mcp_tools.ToolError, match="already here"):
        mcp_tools.add_inbox("rentals mailbox")


def test_deleting_an_inbox_forgets_its_token_and_the_entry(registry, no_token):
    no_token.set("email_oauth::inbox", refresh_token="rt", account_email="me@k.org")

    result = mcp_tools.delete_inbox("inbox")

    assert result["deleted"] is True
    assert not any(s.key == "inbox" for s in registry)
    from core.tools import email_oauth
    assert email_oauth.is_configured("inbox") is False


def test_deleting_an_inbox_a_source_still_uses_is_refused(registry, no_token):
    """It would leave that source with a route to nowhere. The fix is a decision
    about the source, so it is made there."""
    for i, s in enumerate(registry):
        if s.key == "epic":
            registry[i] = s.model_copy(update={"email_search": EmailSearch(carrier="inbox")})

    with pytest.raises(mcp_tools.ToolError, match="still used by Epic"):
        mcp_tools.delete_inbox("inbox")
    assert any(s.key == "inbox" for s in registry), "nothing was deleted"


def test_deleting_something_that_is_not_an_inbox_is_refused(registry, no_token):
    with pytest.raises(mcp_tools.ToolError, match="isn't an inbox"):
        mcp_tools.delete_inbox("epic")


def test_reapproving_reuses_the_stored_oauth_client(registry, no_token, monkeypatch, tmp_path):
    """The client id/secret are already in the vault (they must be, to refresh
    tokens), so a fresh token must not send the operator back to Google Cloud."""
    from core.tools import email_oauth

    # Write the rebuilt client into a temp dir, never the operator's .secrets/.
    monkeypatch.setattr(email_oauth.client_config_file, "__defaults__", (tmp_path,))
    no_token.set("email_oauth::inbox", refresh_token="rt", account_email="me@k.org",
                 client_id="cid", client_secret="csecret")
    started = {}
    monkeypatch.setattr(mcp_tools, "start_gmail_consent",
                        lambda source_key, client_secret_path: started.update(
                            key=source_key, path=client_secret_path) or {"status": "running"})

    result = mcp_tools.reapprove_inbox("inbox")

    assert result["status"] == "running"
    assert started["key"] == "inbox"
    written = json.loads(Path(started["path"]).read_text())
    assert written["installed"]["client_id"] == "cid"
    assert oct(Path(started["path"]).stat().st_mode)[-3:] == "600", "a client secret, so owner-only"


def test_reapproving_an_inbox_that_was_never_connected_says_so(registry, no_token):
    with pytest.raises(mcp_tools.ToolError, match="never been connected"):
        mcp_tools.reapprove_inbox("inbox")
