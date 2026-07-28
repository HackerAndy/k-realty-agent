"""Connecting an inbox from the app instead of a terminal.

Gmail is the one sign-in that can't be a username and a password (Workspace
killed basic auth for IMAP in May 2025), so the operator's path is: upload the
OAuth client JSON → click Allow in the browser Google opens → say which messages
carry the document. Each of those steps has a way to go wrong quietly, which is
what these pin:

  * the uploaded client secret is deleted once consent finishes, however it
    finishes — a second plaintext copy of a client secret is pure downside;
  * an inbox can only deliver to a source that actually has a parser, otherwise
    the fetch succeeds and the document lands nowhere;
  * consent runs out-of-process, so "no worker" must not read as "connected".
"""

import json

import pytest

import core.tools.credential_store as cs
from interfaces import mcp_tools
from core.tools.service_manifest import FetchConfig, Service


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
    assert st["fetch"] is None


def test_only_sources_that_can_parse_are_offered_as_destinations(sources, no_token):
    """A fetch that delivers to a parserless source pulls a document into a
    dead end — don't put that choice on the screen."""
    offered = {t["key"] for t in mcp_tools.email_status("inbox")["can_deliver_to"]}
    assert offered == {"epic"}, "an inbox can't deliver to itself, nor to a source with no parser"


def test_an_unknown_source_is_refused(sources, no_token):
    with pytest.raises(mcp_tools.ToolError, match="Unknown source"):
        mcp_tools.email_status("nope")


# ── Saying what to fetch ─────────────────────────────────────────────────────

@pytest.fixture
def manifest(monkeypatch):
    """Capture set_fetch instead of writing to the real services.yaml."""
    saved = {}

    class FakeManifest:
        def set_fetch(self, key, config):
            saved[key] = config

    monkeypatch.setattr(mcp_tools, "ServiceManifest", FakeManifest)
    return saved


def test_saving_records_the_route_and_the_search(sources, no_token, manifest):
    mcp_tools.save_email_fetch("inbox", delivers_to="epic",
                               from_address=" statements@epic.com ",
                               subject_contains="Owner Statement",
                               attachment_suffix=".pdf", newer_than_days=30)

    cfg = manifest["inbox"]
    assert isinstance(cfg, FetchConfig)
    assert (cfg.provider, cfg.delivers_to) == ("gmail", "epic")
    assert cfg.from_address == "statements@epic.com", "surrounding spaces are a typo, not a filter"
    assert cfg.newer_than_days == 30


def test_blank_criteria_become_absent_rather_than_empty_strings(sources, no_token, manifest):
    """An empty subject filter would otherwise search for the empty string."""
    mcp_tools.save_email_fetch("inbox", delivers_to="epic", from_address="",
                               subject_contains="   ", newer_than_days=None)

    cfg = manifest["inbox"]
    assert cfg.from_address is None and cfg.subject_contains is None
    assert cfg.newer_than_days is None


def test_delivering_to_a_source_with_no_parser_is_refused(sources, no_token, manifest):
    with pytest.raises(mcp_tools.ToolError, match="no parser"):
        mcp_tools.save_email_fetch("inbox", delivers_to="unbuilt")
    assert not manifest, "nothing should be written when the route is unusable"


def test_delivering_to_a_source_that_does_not_exist_is_refused(sources, no_token, manifest):
    with pytest.raises(mcp_tools.ToolError, match="Unknown destination"):
        mcp_tools.save_email_fetch("inbox", delivers_to="ghost")


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
