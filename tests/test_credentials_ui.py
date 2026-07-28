"""Sign-ins managed from the app instead of a terminal.

The operator's call: Settings should hold everything the harness authenticates
with — portal logins, the inbox, the LLM key — rather than sending them to
scripts/manage_secrets.py.

The properties worth pinning are about what must NOT happen: a stored password
must never come back out of the tool surface, and correcting a username must not
silently wipe the password (CredentialStore.set() replaces the whole record,
which already caused exactly that bug once with the LLM key).
"""

import pytest

import core.tools.credential_store as cs
from interfaces import mcp_tools
from core.tools.service_manifest import Service


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """A throwaway encrypted store — never the operator's real .secrets/."""
    monkeypatch.setenv("AGENT_SECRET_KEY", cs.generate_key())
    store = cs.CredentialStore(tmp_path / "creds.enc")
    monkeypatch.setattr(cs, "CredentialStore", lambda *a, **k: store)
    monkeypatch.setattr(
        mcp_tools, "_load_services",
        lambda: [
            Service(key="epic", label="Epic", login_url="https://epic.example.com"),
            Service(key="inbox", label="Email", input_type="email_trigger"),
            Service(key="statement_only", label="A source with no login"),
        ],
    )
    return store


def test_only_things_you_sign_in_to_are_listed(vault):
    keys = [c["key"] for c in mcp_tools.credential_status()]
    assert keys == ["epic", "inbox"], "a plain document source has nothing to sign in to"


def test_a_stored_password_never_comes_back_out(vault):
    """The property that matters most: a screen that can show a password is a
    screen that can leak one."""
    mcp_tools.set_credentials("epic", username="andy@example.com", password="hunter2")

    for row in mcp_tools.credential_status():
        assert "password" not in row, "presence only — never the value"
        assert "hunter2" not in repr(row)

    saved = mcp_tools.set_credentials("epic", username="andy@example.com", password="hunter2")
    assert "hunter2" not in repr(saved)
    assert saved["has_password"] is True


def test_the_username_IS_shown_because_it_is_not_a_secret(vault):
    """You need to know WHICH account is stored, and a username is not a secret."""
    mcp_tools.set_credentials("epic", username="andy@example.com", password="pw")
    row = next(c for c in mcp_tools.credential_status() if c["key"] == "epic")
    assert row["username"] == "andy@example.com"
    assert row["has_username"] and row["has_password"]


def test_a_blank_password_keeps_the_stored_one(vault):
    """Correcting a username must not wipe the password. CredentialStore.set()
    replaces the whole record, which caused exactly this bug with the LLM key."""
    mcp_tools.set_credentials("epic", username="old@example.com", password="keepme")
    mcp_tools.set_credentials("epic", username="new@example.com", password="")

    assert vault.get("epic") == {"username": "new@example.com", "password": "keepme"}


def test_a_blank_username_keeps_the_stored_one(vault):
    mcp_tools.set_credentials("epic", username="andy@example.com", password="pw1")
    mcp_tools.set_credentials("epic", username="", password="pw2")

    assert vault.get("epic")["username"] == "andy@example.com"
    assert vault.get("epic")["password"] == "pw2"


def test_a_password_of_spaces_is_not_trimmed_away(vault):
    """Spaces can be part of a password; only the username is stripped."""
    mcp_tools.set_credentials("epic", username="  andy@example.com  ", password="  pw  ")
    assert vault.get("epic") == {"username": "andy@example.com", "password": "  pw  "}


def test_saving_nothing_at_all_is_refused(vault):
    with pytest.raises(mcp_tools.ToolError, match="username and a password"):
        mcp_tools.set_credentials("epic")


def test_unrelated_stored_fields_survive_an_update(vault):
    """A source may carry more than a username/password; don't drop the rest."""
    vault.set("epic", username="a@b.com", password="pw", account_number="12345")
    mcp_tools.set_credentials("epic", username="c@d.com", password="")

    assert vault.get("epic")["account_number"] == "12345"


def test_an_unknown_source_is_refused(vault):
    with pytest.raises(mcp_tools.ToolError, match="Unknown source"):
        mcp_tools.set_credentials("nope", username="u", password="p")


def test_forgetting_a_sign_in_removes_it(vault):
    mcp_tools.set_credentials("epic", username="a@b.com", password="pw")
    result = mcp_tools.forget_credentials("epic")

    assert result["has_password"] is False
    row = next(c for c in mcp_tools.credential_status() if c["key"] == "epic")
    assert row["has_username"] is False and row["username"] == ""


def test_forgetting_one_leaves_the_others_alone(vault):
    mcp_tools.set_credentials("epic", username="a@b.com", password="pw")
    vault.set("inbox", username="me@gmail.com", password="x")

    mcp_tools.forget_credentials("epic")

    assert vault.get("inbox")["username"] == "me@gmail.com"


def test_an_empty_vault_reports_cleanly_rather_than_erroring(vault):
    rows = mcp_tools.credential_status()
    assert all(r["has_password"] is False for r in rows)
    assert all(r["username"] == "" for r in rows)


def test_the_audit_log_records_the_event_but_not_the_secret(vault, tmp_path, monkeypatch):
    import core.observability as observability
    import json

    monkeypatch.setattr(observability, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(observability, "LOG_FILE", tmp_path / "logs" / "agent.jsonl")

    mcp_tools.set_credentials("epic", username="andy@example.com", password="hunter2")

    text = (tmp_path / "logs" / "agent.jsonl").read_text()
    assert "CREDENTIALS_SAVED" in text
    assert "hunter2" not in text, "a secret must never reach the log"
    assert json.loads(text.splitlines()[-1])["context"]["source_key"] == "epic"
