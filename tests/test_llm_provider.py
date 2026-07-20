"""LLM provider store/load — both providers set the env vars the agent reads.

Isolated to a temp credential store so it never touches the operator's real
.secrets/. Confirms the seam between "stored provider choice" and the env vars
orchestration/agent.py selects on (LLM_PROVIDER, ANTHROPIC_API_KEY/AGENT_MODEL,
OMLX_*).
"""

import os

import core.tools.credential_store as cs
import core.tools.llm_provider as lp

_ENV_VARS = ("LLM_PROVIDER", "ANTHROPIC_API_KEY", "AGENT_MODEL",
             "OMLX_BASE_URL", "OMLX_MODEL", "OMLX_API_KEY")


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SECRET_KEY", cs.generate_key())
    store = cs.CredentialStore(tmp_path / "creds.enc")
    monkeypatch.setattr(lp, "CredentialStore", lambda: store)
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_anthropic_roundtrip(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    lp.store_llm_credential("anthropic", api_key="sk-test", model="claude-opus-4-8")

    assert lp.load_into_env() is True
    assert os.environ["LLM_PROVIDER"] == "anthropic"
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-test"
    assert os.environ["AGENT_MODEL"] == "claude-opus-4-8"
    assert lp.configured_provider() == "anthropic"
    assert lp.current_config()["api_key"] == "<stored>"  # masked for display


def test_openai_compatible_roundtrip(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    lp.store_llm_credential("openai_compatible", base_url="http://host:9090/v1", model="qwen2.5-coder:7b")

    assert lp.load_into_env() is True
    assert os.environ["LLM_PROVIDER"] == "openai_compatible"
    assert os.environ["OMLX_BASE_URL"] == "http://host:9090/v1"
    assert os.environ["OMLX_MODEL"] == "qwen2.5-coder:7b"
    assert os.environ["OMLX_API_KEY"] == "local"  # defaulted when no key given
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_anthropic_without_key_is_not_ready(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    lp.store_llm_credential("anthropic")  # no key
    assert lp.load_into_env() is False
