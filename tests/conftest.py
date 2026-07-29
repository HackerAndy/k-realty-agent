"""Test-wide safety rails.

The suite runs on the operator's own machine, in the operator's own working
directory — where `.secrets/credentials.enc` holds their real provider settings.
Nothing here should read that: a test that resolves the real LLM choice will go
on to call the real provider (that is exactly how a unit test ended up making a
live request to the operator's local model server).
"""

import pytest

import core.tools.credential_store as credential_store


@pytest.fixture(autouse=True)
def isolate_the_credential_store(monkeypatch, tmp_path):
    """Point the default store at an empty temp file, and clear the provider env
    vars, so every test sees "nothing is configured" unless it configures it."""
    monkeypatch.setattr(credential_store, "DEFAULT_STORE_PATH", tmp_path / "credentials.enc")
    monkeypatch.setattr(
        credential_store.CredentialStore.__init__,
        "__defaults__",
        (tmp_path / "credentials.enc",),
    )
    for var in ("LLM_PROVIDER", "ANTHROPIC_API_KEY", "AGENT_MODEL",
                "OMLX_BASE_URL", "OMLX_MODEL", "OMLX_API_KEY"):
        monkeypatch.delenv(var, raising=False)
