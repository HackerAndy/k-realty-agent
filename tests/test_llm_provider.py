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
             "OMLX_BASE_URL", "OMLX_MODEL", "OMLX_API_KEY", "CLAUDE_CODE_MODEL")


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
    assert lp.stored_api_key() == "sk-test"  # unscoped: whatever is active


def test_claude_code_exports_no_key_into_the_environment(tmp_path, monkeypatch):
    """A billing decision, not a detail. The CLI reads ANTHROPIC_API_KEY from its
    environment, so exporting one here would settle who pays for every process
    that inherits it — including ones the operator never pointed at the CLI."""
    _isolate(tmp_path, monkeypatch)
    lp.store_llm_credential("claude_code", api_key="sk-metered", model="opus")

    assert lp.load_into_env() is True
    assert os.environ["LLM_PROVIDER"] == "claude_code"
    assert os.environ["CLAUDE_CODE_MODEL"] == "opus"
    assert "ANTHROPIC_API_KEY" not in os.environ
    # It still reaches the one place that needs it: the CLI's own subprocess.
    assert lp.resolve().api_key == "sk-metered"


# --- three providers, one of them armed --------------------------------------
#
# The store used to hold a single record: the active provider WITH its settings.
# Configuring a second provider therefore overwrote the first one's key, and the
# operator only found out on switching back. Each provider now keeps its own
# settings and the record holds nothing but a pointer.

def test_each_provider_keeps_its_own_settings(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    lp.store_llm_credential("anthropic", api_key="sk-test", model="claude-opus-4-8")
    lp.store_llm_credential("openai_compatible", base_url="http://box:9090/v1", model="qwen-30b")

    assert lp.configured_provider() == "openai_compatible"
    assert lp.stored_api_key("anthropic") == "sk-test", "switching must not discard a key"
    assert lp.resolve(provider="anthropic").model == "claude-opus-4-8"
    assert lp.resolve().model == "qwen-30b"


def test_saving_a_provider_can_leave_the_running_one_alone(tmp_path, monkeypatch):
    """Editing a provider you are not using must not switch the harness onto it."""
    _isolate(tmp_path, monkeypatch)
    lp.store_llm_credential("anthropic", api_key="sk-test")

    lp.store_llm_credential("openai_compatible", base_url="http://box:9090/v1",
                            model="qwen-30b", activate=False)

    assert lp.configured_provider() == "anthropic"
    assert lp.resolve().provider == "anthropic"
    assert lp.settings_for("openai_compatible")["model"] == "qwen-30b"


def test_the_first_provider_saved_is_armed_even_without_asking(tmp_path, monkeypatch):
    """Otherwise a first-time setup stores a provider nothing uses, and the
    harness reports no model for no visible reason."""
    _isolate(tmp_path, monkeypatch)

    lp.store_llm_credential("anthropic", api_key="sk-test", activate=False)

    assert lp.configured_provider() == "anthropic"


def test_a_legacy_store_keeps_its_key_when_another_provider_is_saved(tmp_path, monkeypatch):
    """The old shape, written by an earlier version, must survive the split —
    silently losing the key on first save would be the exact bug this prevents,
    arriving at upgrade time instead."""
    _isolate(tmp_path, monkeypatch)
    lp.CredentialStore().set(lp.LLM_CREDENTIAL_KEY, provider="anthropic",
                             api_key="sk-old", model="claude-opus-4-8")
    assert lp.resolve().api_key == "sk-old", "the legacy shape must read before it migrates"

    lp.store_llm_credential("claude_code", model="sonnet")

    assert lp.configured_provider() == "claude_code"
    assert lp.stored_api_key("anthropic") == "sk-old"
    assert lp.resolve(provider="anthropic").model == "claude-opus-4-8"
    # The pointer record now holds nothing but the pointer.
    assert lp.CredentialStore().get(lp.LLM_CREDENTIAL_KEY) == {"provider": "claude_code"}


# --- resolve(): the one answer to "which model" ------------------------------
#
# Every LLM call in the harness routes through resolve(). The rule it exists to
# enforce: what the operator picked in Settings is what runs. Anything that kept
# its own default was a black box — the extractor did, so an operator on a local
# server was told to "check ANTHROPIC_API_KEY" for a model they never chose.

def test_settings_beat_a_stale_environment_variable(tmp_path, monkeypatch):
    """THE case. The GUI is where the choice is made and shown; an env var left
    over from a shell must not quietly redirect the run somewhere else."""
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("AGENT_MODEL", "claude-opus-4-8")
    lp.store_llm_credential("openai_compatible", base_url="http://box:9090/v1", model="qwen-30b")

    choice = lp.resolve()

    assert (choice.provider, choice.model) == ("openai_compatible", "qwen-30b")
    assert choice.base_url == "http://box:9090/v1"
    assert choice.model_source == "settings"


def test_the_environment_is_used_when_settings_are_silent(tmp_path, monkeypatch):
    """Headless/CI runs still need a way in."""
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("OMLX_MODEL", "from-env")

    choice = lp.resolve()

    assert choice.model == "from-env" and choice.model_source == "environment"


def test_nothing_configured_falls_back_to_the_documented_default(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)

    choice = lp.resolve()

    assert choice.provider == lp.DEFAULT_PROVIDER
    assert choice.model == lp.DEFAULT_MODEL
    assert choice.model_source == "default"


def test_an_explicit_argument_still_wins(tmp_path, monkeypatch):
    """A caller asking for a specific model (a build run pinned by the operator)
    is a deliberate act, not a silent substitution."""
    _isolate(tmp_path, monkeypatch)
    lp.store_llm_credential("anthropic", api_key="sk-test", model="claude-opus-4-8")

    assert lp.resolve(model="claude-haiku-4-5-20251001").model == "claude-haiku-4-5-20251001"


def test_a_stored_model_never_crosses_to_another_provider(tmp_path, monkeypatch):
    """Switching provider must not carry the other one's model name (or key)
    across — 'claude-opus-4-8' sent to a local server is a confusing 404."""
    _isolate(tmp_path, monkeypatch)
    lp.store_llm_credential("anthropic", api_key="sk-test", model="claude-opus-4-8")

    choice = lp.resolve(provider="openai_compatible")

    assert choice.model == lp.DEFAULT_OMLX_MODEL
    assert choice.api_key != "sk-test", "an Anthropic key must not reach a local server"


def test_provider_aliases_resolve_to_one_name(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    assert lp.resolve(provider="claude").provider == "anthropic"
    assert lp.resolve(provider="omlx").provider == "openai_compatible"
    assert lp.resolve(provider="local").provider == "openai_compatible"


def test_describe_says_which_model_and_where_it_came_from(tmp_path, monkeypatch):
    """This string is what the operator reads in a log or on screen."""
    _isolate(tmp_path, monkeypatch)
    lp.store_llm_credential("openai_compatible", base_url="http://box:9090/v1", model="qwen-30b")

    described = lp.resolve().describe()

    assert "qwen-30b" in described and "Settings" in described


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
