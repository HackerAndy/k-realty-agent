"""Test-wide safety rails.

The suite runs on the operator's own machine, in the operator's own working
directory — where `.secrets/credentials.enc` holds their real provider settings
and `data/logs/agent.jsonl` is the log they and the embedded agent debug from.
Nothing here should touch either.

Two real incidents, one per fixture below:

- A test that resolved the real LLM choice went on to call the real provider —
  a unit test making a live request to the operator's model server.
- The suite appended 788 records to the operator's diagnostic log: fixtures
  named 'epic', 'portal', 'some_bank', credential errors pointing at pytest
  temp directories. That log is what `read_logs` hands the embedded agent, so an
  agent debugging a live failure was reading invented ones — and it made a real
  timeline nearly impossible to read.
"""

import pytest

import core.observability as observability
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


@pytest.fixture(autouse=True)
def isolate_the_log(monkeypatch, tmp_path):
    """Send this test's log records to a temp directory.

    Both the env var and the module attribute: the env var covers subprocesses
    the suite spawns, the attribute covers this process (LOG_DIR is read at
    import, and _log_file() re-reads it at every write)."""
    log_dir = tmp_path / "logs"
    monkeypatch.setenv("AGENT_LOG_DIR", str(log_dir))
    monkeypatch.setattr(observability, "LOG_DIR", log_dir)
    monkeypatch.setattr(observability, "LOG_FILE", log_dir / "agent.jsonl")
