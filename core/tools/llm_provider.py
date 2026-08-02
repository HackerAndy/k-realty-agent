# Template candidate: generic (tier 1) — no client-specific logic.
# See agent-harness-template/docs/promotion-log.md.
"""LLM provider configuration — the harness's own LLM key is just another
secret in the encrypted store.

The harness is meant to be provider-agnostic over time. Today it assumes the
Claude API, but the shape here anticipates swapping: the stored credential
records which `provider` it is, and PROVIDER_ENV maps each provider to the
environment variable its SDK reads. load_into_env() sets that variable from
the stored key so the rest of the code (which just constructs the provider's
SDK client) needs no changes.

This module must stay framework-free (no langgraph/langchain imports).
"""

from __future__ import annotations

from dataclasses import dataclass
import errno as _errno
import ipaddress
import json
import os
import sys
from urllib import error, request
from urllib.parse import urlparse

from core.observability import get_logger
from core.tools.credential_store import CredentialStore, CredentialStoreError

log = get_logger("core.tools.llm_provider")

# Reserved key under which the LLM credential lives in the credential store —
# stored "alongside the rest" of the secrets, but not a data source.
#
# It holds ONE field: which provider is active. Each provider's own settings —
# key, model, base URL — live beside it under `llm_provider:<provider>`, so
# switching provider is a pointer move and nothing is lost. The old shape kept
# the active provider's details in this record, which meant configuring a second
# provider overwrote the first one's key: the operator would switch back and
# find it gone, with nothing on screen having said so. `_migrate_pointer()`
# lifts an old store into the new shape the first time anything is written.
LLM_CREDENTIAL_KEY = "llm_provider"

DEFAULT_PROVIDER = "anthropic"

# The providers the harness (orchestration/agent.py) can drive:
#   anthropic          — the Claude API (needs an api_key)
#   openai_compatible  — any local/hosted OpenAI-compatible server (OMLX, Ollama,
#                        LM Studio, vLLM …); needs base_url + model, key optional.
#   claude_code        — the Claude Code CLI on this machine. NOT an endpoint:
#                        it is a whole agent with its own tool loop, so the
#                        harness hands it the task instead of driving turns. See
#                        `LLMChoice.is_agent` and orchestration/claude_code.py.
PROVIDERS = ("anthropic", "openai_compatible", "claude_code")

# Aliases the operator (or an env var, or a caller) may use for each provider.
_ALIASES = {
    "anthropic": "anthropic", "claude": "anthropic",
    "openai_compatible": "openai_compatible", "openai": "openai_compatible",
    "omlx": "openai_compatible", "local": "openai_compatible",
    "claude_code": "claude_code", "claude-code": "claude_code",
    "cli": "claude_code", "claude_cli": "claude_code",
}

# Last-resort defaults, used only when nothing is stored and nothing is in the
# environment. They live HERE, next to the stored settings, so there is exactly
# one answer to "which model is this harness using" — a second copy in a caller
# is how a harness ends up running a model the operator never chose.
DEFAULT_MODEL = "claude-opus-4-8"
DEFAULT_OMLX_MODEL = "qwen2.5-coder:7b"
DEFAULT_OMLX_BASE_URL = "http://klabss-MacBook-Pro.local:9090/v1"
DEFAULT_OMLX_API_KEY = "local"
# An alias rather than a pinned name, so the CLI resolves whatever is current
# instead of the harness pinning a model that will age out.
DEFAULT_CLAUDE_CODE_MODEL = "sonnet"
# The CLI usually authenticates itself (subscription login or its own keychain
# entry), so a stored key is optional here — unlike every other provider. When
# one IS stored it is exported to the subprocess, which is what an operator on
# a metered API key will want.
CLAUDE_CODE_BINARY = "claude"

_WHERE = {
    "settings": "chosen in Settings",
    "environment": "from the environment",
    "default": "the built-in default — nothing is set in Settings",
}


def normalise_provider(provider: str | None) -> str:
    return _ALIASES.get((provider or "").strip().lower(), (provider or "").strip().lower())


@dataclass(frozen=True)
class LLMChoice:
    """WHICH model this harness is about to talk to, and where that came from.

    Every LLM call in the harness resolves through here. The operator picks a
    provider and model in Settings; anything that reached for its own hardcoded
    model would be running something they never chose, with no way to tell from
    the screen — so `model_source` is carried along and shown.
    """

    provider: str
    model: str
    base_url: str | None = None
    api_key: str | None = None
    provider_source: str = "default"
    model_source: str = "default"

    @property
    def is_anthropic(self) -> bool:
        return self.provider == "anthropic"

    @property
    def is_agent(self) -> bool:
        """True when this provider is a whole agent, not a model endpoint.

        The harness drives its own tool loop against an endpoint. Claude Code
        brings its own loop, its own tools and its own context management, so it
        is handed the task and left to it — a different code path entirely, and
        the reason this is a property rather than another adapter.
        """
        return self.provider == "claude_code"

    def describe(self) -> str:
        """One line, for a log or a screen: what ran, and why it was picked."""
        return f"{self.model} ({self.provider}, {_WHERE.get(self.model_source, self.model_source)})"


def _settings_key(provider: str) -> str:
    """Where one provider's own settings live, active or not."""
    return f"{LLM_CREDENTIAL_KEY}:{normalise_provider(provider)}"


def _pointer() -> dict:
    """The record naming the active provider. `{}` when nothing is set up."""
    try:
        return dict(CredentialStore().get(LLM_CREDENTIAL_KEY))
    except CredentialStoreError:
        return {}


def settings_for(provider: str) -> dict:
    """That provider's saved settings — key, model, base URL — whether or not it
    is the active one.

    Kept per provider so the three can be configured independently and switched
    between. Falls back to the legacy single-record shape when the store predates
    the split, so an existing setup keeps working before it is ever rewritten.
    """
    provider = normalise_provider(provider)
    if not provider:
        return {}
    try:
        own = CredentialStore().try_get(_settings_key(provider))
    except CredentialStoreError:
        return {}
    if own is not None:
        return dict(own)
    legacy = _pointer()
    if normalise_provider(legacy.get("provider")) == provider:
        return {k: v for k, v in legacy.items() if k != "provider"}
    return {}


def _migrate_pointer(store: CredentialStore) -> None:
    """Lift a legacy store into the split shape, before anything overwrites it.

    Idempotent, and only ever moves details DOWN into the provider they already
    belonged to — so running it can't hand one provider another's key. A store
    already in the new shape has nothing to move.
    """
    pointer = _pointer()
    provider = normalise_provider(pointer.get("provider"))
    details = {k: v for k, v in pointer.items() if k != "provider"}
    if not provider or not details:
        return
    if store.try_get(_settings_key(provider)) is None:
        store.set(_settings_key(provider), **details)
    store.set(LLM_CREDENTIAL_KEY, provider=provider)


def _pick(explicit: str | None, stored: str | None, env_var: str, default: str) -> tuple[str, str]:
    """Value + where it came from, in the order that keeps Settings honest."""
    if explicit:
        return explicit, "caller"
    if stored:
        return stored, "settings"
    from_env = os.getenv(env_var)
    if from_env:
        return from_env, "environment"
    return default, "default"


def resolve(
    provider: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
) -> LLMChoice:
    """The one place that answers "which model, on which provider".

    Precedence: an explicit argument, then what the operator stored in Settings,
    then the environment, then the built-in default. Settings beats the
    environment deliberately — the GUI is where the choice is made and shown, so
    a stale env var must not quietly redirect a run somewhere else.

    Settings are read from the provider being resolved and from nowhere else, so
    switching provider can never carry the other one's model name or key across
    — that would fail the request, or worse, run something unintended.
    """
    resolved_provider, provider_source = _pick(
        normalise_provider(provider) or None, configured_provider(),
        "LLM_PROVIDER", DEFAULT_PROVIDER,
    )
    resolved_provider = normalise_provider(resolved_provider) or DEFAULT_PROVIDER
    own = settings_for(resolved_provider)

    if resolved_provider == "anthropic":
        chosen_model, model_source = _pick(model, own.get("model"), "AGENT_MODEL", DEFAULT_MODEL)
        return LLMChoice(
            provider=resolved_provider,
            model=chosen_model,
            api_key=own.get("api_key") or os.getenv("ANTHROPIC_API_KEY"),
            provider_source=provider_source,
            model_source=model_source,
        )

    if resolved_provider == "claude_code":
        chosen_model, model_source = _pick(
            model, own.get("model"), "CLAUDE_CODE_MODEL", DEFAULT_CLAUDE_CODE_MODEL)
        return LLMChoice(
            provider=resolved_provider,
            model=chosen_model,
            # ONLY what the operator stored, with no environment fallback — and
            # that difference is a billing decision, not a detail. The CLI signed
            # in to a Claude subscription draws on that plan; hand it an API key
            # and it bills per token instead. `load_into_env()` puts
            # ANTHROPIC_API_KEY in the environment whenever the anthropic
            # provider is configured, so falling back to it here would silently
            # move an operator off their subscription because of a setting they
            # made for a different provider. None means "use the CLI's own
            # login", and it has to be reachable.
            api_key=own.get("api_key") or None,
            provider_source=provider_source,
            model_source=model_source,
        )

    chosen_model, model_source = _pick(model, own.get("model"), "OMLX_MODEL", DEFAULT_OMLX_MODEL)
    chosen_url, _ = _pick(base_url, own.get("base_url"), "OMLX_BASE_URL", DEFAULT_OMLX_BASE_URL)
    return LLMChoice(
        provider=resolved_provider,
        model=chosen_model,
        base_url=chosen_url,
        api_key=own.get("api_key") or os.getenv("OMLX_API_KEY") or DEFAULT_OMLX_API_KEY,
        provider_source=provider_source,
        model_source=model_source,
    )


def chat_completion(base_url: str, api_key: str, payload: dict, timeout_s: int = 120) -> dict:
    """POST to an OpenAI-compatible /chat/completions. Lives here, with the rest
    of the provider plumbing, so core/ code can reach a local server without
    importing the orchestration layer."""
    url = f"{base_url.rstrip('/')}/chat/completions"
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    req = request.Request(url, data=body, headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=timeout_s) as response:
            return json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
        raise RuntimeError(f"OpenAI-compatible API error {exc.code}: {detail}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"Could not reach OpenAI-compatible API at {url}: {exc}") from exc


def is_configured() -> bool:
    """True if a provider has been made active (needs the master key to check)."""
    return configured_provider() is not None


def store_llm_credential(
    provider: str = DEFAULT_PROVIDER,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    activate: bool = True,
) -> None:
    """Save one provider's settings. `api_key` for anthropic; `base_url`+`model`
    (key optional) for an openai_compatible server; model only for claude_code.

    Saving and choosing are separate acts: `activate=False` records the settings
    without changing which provider runs, so the operator can set up a second one
    while the first keeps working. When nothing is active yet, the first provider
    saved becomes the active one regardless — a stored provider that nothing uses
    would leave the harness with no model for no visible reason.
    """
    provider = normalise_provider(provider) or DEFAULT_PROVIDER
    store = CredentialStore()
    _migrate_pointer(store)
    fields: dict[str, str] = {}
    if api_key:
        fields["api_key"] = api_key
    if base_url:
        fields["base_url"] = base_url
    if model:
        fields["model"] = model
    store.set(_settings_key(provider), **fields)
    if activate or configured_provider() is None:
        store.set(LLM_CREDENTIAL_KEY, provider=provider)


def configured_provider() -> str | None:
    """Which provider is active, or None if none has been chosen."""
    pointer = _pointer()
    if not pointer:
        return None
    return normalise_provider(pointer.get("provider")) or DEFAULT_PROVIDER


def current_config() -> dict | None:
    """The ACTIVE provider's settings with the api_key masked — for display."""
    provider = configured_provider()
    if provider is None:
        return None
    cfg = {"provider": provider, **settings_for(provider)}
    if cfg.get("api_key"):
        cfg["api_key"] = "<stored>"
    return cfg


def stored_api_key(provider: str | None = None) -> str | None:
    """The RAW stored api_key, so a caller can reuse it when the operator didn't
    re-type it. Server-side only — never return this to a client.

    Scoped to one provider, which is what stops an Anthropic key being handed to
    a local OpenAI-compatible server. Omitting `provider` means the active one.
    """
    provider = normalise_provider(provider) if provider else configured_provider()
    if not provider:
        return None
    return settings_for(provider).get("api_key") or None


def is_local_network_host(host: str) -> bool:
    """True when `host` names a machine on the LAN — a private/link-local IP or an
    mDNS `.local` name. Loopback is excluded: it is never gated (see below)."""
    if not host:
        return False
    if host.lower().endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (ip.is_private or ip.is_link_local) and not ip.is_loopback


def blocked_by_macos_local_network(host: str, exc: BaseException) -> bool:
    """Detect macOS denying this process local-network access.

    macOS 15+ gates LAN access per signed binary. A denied process gets
    EHOSTUNREACH even though the host is up and serving — which is why
    Apple-signed /usr/bin/curl reaches it while an ad-hoc-signed Homebrew Python
    (TeamIdentifier=not set) cannot, from the very same shell. Without naming
    this, the failure reads as a dead server and sends you debugging the network.
    """
    if sys.platform != "darwin" or not is_local_network_host(host):
        return False
    reason = getattr(exc, "reason", exc)
    return getattr(reason, "errno", None) == _errno.EHOSTUNREACH


def list_models(base_url: str, api_key: str | None = None) -> list[str]:
    """Ask an OpenAI-compatible server which models it actually has (GET /models),
    so the operator picks a real one instead of guessing. Raises on failure so the
    caller can show why."""
    url = base_url.rstrip("/") + "/models"
    host = urlparse(url).hostname or ""
    # api_key is masked to <present>/<absent> by the logging standard.
    ctx = {"base_url": base_url, "host": host, "api_key": api_key}
    req = request.Request(url, headers={"Authorization": f"Bearer {api_key or 'local'}"})
    try:
        with request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        raise RuntimeError(log.failure(
            operation="list_models",
            code="LLM_SERVER_HTTP_ERROR",
            message=f"{host} answered {exc.code} for {url}.",
            remediation=(
                "The server is reachable, so this is the request, not the network — "
                "check the API key and that the base URL ends in the right path (e.g. /v1)."
            ),
            context={**ctx, "status": exc.code},
            exc=exc,
        )) from exc
    except error.URLError as exc:
        if blocked_by_macos_local_network(host, exc):
            raise RuntimeError(log.failure(
                operation="list_models",
                code="LLM_LOCAL_NETWORK_BLOCKED",
                message=(
                    f"macOS blocked this Python process from reaching {host} on the local network "
                    f"(the server itself may be fine — curl can usually reach it)."
                ),
                remediation=(
                    "Grant Local Network access to this Python binary under System Settings → "
                    "Privacy & Security → Local Network; if it never prompted, run "
                    "'sudo tccutil reset LocalNetwork' and retry. Alternatively point the harness "
                    "at a loopback tunnel (ssh -N -L <port>:localhost:<port> host), which is not gated."
                ),
                context={**ctx, "interpreter": sys.executable, "platform": sys.platform},
                exc=exc,
            )) from exc
        raise RuntimeError(log.failure(
            operation="list_models",
            code="LLM_SERVER_UNREACHABLE",
            message=f"Could not reach {url}: {exc.reason}.",
            remediation="Check the base URL, and that the server is running and reachable from this machine.",
            context=ctx,
            exc=exc,
        )) from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError(log.failure(
            operation="list_models",
            code="LLM_MODELS_UNREADABLE",
            message=f"{host} answered, but the model list wasn't valid JSON.",
            remediation="Confirm the base URL points at an OpenAI-compatible API root (it should end in /v1).",
            context=ctx,
            exc=exc,
        )) from exc
    return [m.get("id") for m in data.get("data", []) if m.get("id")]


def load_into_env() -> bool:
    """Load the ACTIVE provider's settings into the env vars orchestration/agent.py
    reads (LLM_PROVIDER, plus ANTHROPIC_API_KEY/AGENT_MODEL or OMLX_*). Returns
    True if a usable provider was loaded. No-op-safe if nothing is stored.

    Only the active provider's own variables are exported — never another
    provider's. Nothing is unset, so a key that was already in the real
    environment before the harness started stays there; `resolve()` is what
    decides whether it is used, and for claude_code it deliberately does not.
    """
    provider = configured_provider()
    if provider is None:
        return False
    cred = settings_for(provider)
    os.environ["LLM_PROVIDER"] = provider

    if provider == "anthropic":
        api_key = cred.get("api_key")
        if not api_key:
            return False
        os.environ["ANTHROPIC_API_KEY"] = api_key
        if cred.get("model"):
            os.environ["AGENT_MODEL"] = cred["model"]
        return True

    if provider == "claude_code":
        # No key is exported, even when one is stored. The CLI reads
        # ANTHROPIC_API_KEY from its environment, so exporting it here would
        # decide a billing question process-wide; `run_claude_code` passes the
        # stored key (or removes the variable) for its own subprocess only.
        if cred.get("model"):
            os.environ["CLAUDE_CODE_MODEL"] = cred["model"]
        return True

    # openai_compatible / local — a key isn't always required
    if cred.get("base_url"):
        os.environ["OMLX_BASE_URL"] = cred["base_url"]
    if cred.get("model"):
        os.environ["OMLX_MODEL"] = cred["model"]
    os.environ["OMLX_API_KEY"] = cred.get("api_key") or "local"
    return bool(cred.get("base_url"))
