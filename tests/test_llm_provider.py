"""LLM provider store/load — both providers set the env vars the agent reads.

Isolated to a temp credential store so it never touches the operator's real
.secrets/. Confirms the seam between "stored provider choice" and the env vars
orchestration/agent.py selects on (LLM_PROVIDER, ANTHROPIC_API_KEY/AGENT_MODEL,
OMLX_*).
"""

import errno
import json
import os
from urllib import error

import pytest

import core.observability as observability
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


def test_stored_api_key_is_scoped_to_its_provider(tmp_path, monkeypatch):
    """An Anthropic key must never be handed to a local OpenAI-compatible server."""
    _isolate(tmp_path, monkeypatch)
    lp.store_llm_credential("anthropic", api_key="sk-test")

    assert lp.stored_api_key("anthropic") == "sk-test"
    assert lp.stored_api_key("openai_compatible") is None
    assert lp.stored_api_key() == "sk-test"  # unscoped: whatever is stored


# --- local-network detection -------------------------------------------------

@pytest.mark.parametrize(
    "host, expected",
    [
        ("klabss-macbook-pro.local", True),   # mDNS name
        ("192.168.0.120", True),              # private LAN
        ("10.1.2.3", True),
        ("169.254.5.5", True),                # link-local
        ("127.0.0.1", False),                 # loopback is never gated
        ("localhost", False),                 # not an IP, not .local
        ("api.anthropic.com", False),         # public
        ("", False),
    ],
)
def test_is_local_network_host(host, expected):
    assert lp.is_local_network_host(host) is expected


def _urlerror(err_no):
    return error.URLError(OSError(err_no, os.strerror(err_no)))


def test_blocked_by_macos_local_network_matches_the_real_signature(monkeypatch):
    """The exact shape seen in the field: EHOSTUNREACH to a LAN host on darwin,
    while curl reaches the same host fine."""
    monkeypatch.setattr(lp.sys, "platform", "darwin")
    assert lp.blocked_by_macos_local_network("192.168.0.120", _urlerror(errno.EHOSTUNREACH)) is True
    # A refused connection is a real dead server, not a privacy block.
    assert lp.blocked_by_macos_local_network("192.168.0.120", _urlerror(errno.ECONNREFUSED)) is False
    # Public hosts are not gated by Local Network privacy.
    assert lp.blocked_by_macos_local_network("api.anthropic.com", _urlerror(errno.EHOSTUNREACH)) is False


def test_blocked_by_macos_local_network_is_darwin_only(monkeypatch):
    monkeypatch.setattr(lp.sys, "platform", "linux")
    assert lp.blocked_by_macos_local_network("192.168.0.120", _urlerror(errno.EHOSTUNREACH)) is False


# --- list_models failures are logged to the project standard -----------------

def _capture_log(tmp_path, monkeypatch):
    monkeypatch.setattr(observability, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(observability, "LOG_FILE", tmp_path / "logs" / "agent.jsonl")
    return tmp_path / "logs" / "agent.jsonl"


def _records(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_list_models_local_network_block_is_named_and_logged(tmp_path, monkeypatch):
    log_file = _capture_log(tmp_path, monkeypatch)
    monkeypatch.setattr(lp.sys, "platform", "darwin")

    def blocked(req, timeout=None):
        raise _urlerror(errno.EHOSTUNREACH)

    monkeypatch.setattr(lp.request, "urlopen", blocked)

    with pytest.raises(RuntimeError) as exc:
        lp.list_models("http://192.168.0.120:9090/v1", api_key="secret-value")

    # The operator-facing string names the cause and what to do about it.
    assert "macOS blocked" in str(exc.value)
    assert "Local Network" in str(exc.value)

    rec = _records(log_file)[-1]
    assert rec["code"] == "LLM_LOCAL_NETWORK_BLOCKED"
    assert rec["component"] == "core.tools.llm_provider"
    assert rec["operation"] == "list_models"
    assert rec["cause"]["type"] == "URLError"
    assert rec["remediation"] and rec["traceback"]
    assert rec["context"]["host"] == "192.168.0.120"
    # The key is recorded as present/absent — never its value.
    assert rec["context"]["api_key"] == "<present>"
    assert "secret-value" not in json.dumps(rec)


def test_list_models_ordinary_unreachable_is_not_blamed_on_macos(tmp_path, monkeypatch):
    log_file = _capture_log(tmp_path, monkeypatch)
    monkeypatch.setattr(lp.sys, "platform", "darwin")

    def refused(req, timeout=None):
        raise _urlerror(errno.ECONNREFUSED)

    monkeypatch.setattr(lp.request, "urlopen", refused)

    with pytest.raises(RuntimeError, match="Could not reach"):
        lp.list_models("http://192.168.0.120:9090/v1")

    rec = _records(log_file)[-1]
    assert rec["code"] == "LLM_SERVER_UNREACHABLE"


def test_list_models_http_error_is_logged_as_reachable(tmp_path, monkeypatch):
    log_file = _capture_log(tmp_path, monkeypatch)

    def unauthorized(req, timeout=None):
        raise error.HTTPError("http://x/v1/models", 401, "Unauthorized", {}, None)

    monkeypatch.setattr(lp.request, "urlopen", unauthorized)

    with pytest.raises(RuntimeError, match="401"):
        lp.list_models("http://192.168.0.120:9090/v1")

    rec = _records(log_file)[-1]
    assert rec["code"] == "LLM_SERVER_HTTP_ERROR"
    assert rec["context"]["status"] == 401
