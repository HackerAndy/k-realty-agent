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
LLM_CREDENTIAL_KEY = "llm_provider"

DEFAULT_PROVIDER = "anthropic"

# The providers the harness (orchestration/agent.py) can drive:
#   anthropic          — the Claude API (needs an api_key)
#   openai_compatible  — any local/hosted OpenAI-compatible server (OMLX, Ollama,
#                        LM Studio, vLLM …); needs base_url + model, key optional.
PROVIDERS = ("anthropic", "openai_compatible")


def is_configured() -> bool:
    """True if an LLM credential is stored (needs the master key to check)."""
    try:
        CredentialStore().get(LLM_CREDENTIAL_KEY)
        return True
    except CredentialStoreError:
        return False


def store_llm_credential(
    provider: str = DEFAULT_PROVIDER,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
) -> None:
    """Store the harness's LLM choice as a secret. `api_key` for anthropic;
    `base_url`+`model` (key optional) for an openai_compatible server."""
    fields: dict[str, str] = {"provider": provider}
    if api_key:
        fields["api_key"] = api_key
    if base_url:
        fields["base_url"] = base_url
    if model:
        fields["model"] = model
    CredentialStore().set(LLM_CREDENTIAL_KEY, **fields)


def configured_provider() -> str | None:
    try:
        return CredentialStore().get(LLM_CREDENTIAL_KEY).get("provider", DEFAULT_PROVIDER)
    except CredentialStoreError:
        return None


def current_config() -> dict | None:
    """The stored provider config with the api_key masked — for display."""
    try:
        cred = dict(CredentialStore().get(LLM_CREDENTIAL_KEY))
    except CredentialStoreError:
        return None
    if cred.get("api_key"):
        cred["api_key"] = "<stored>"
    return cred


def stored_api_key(provider: str | None = None) -> str | None:
    """The RAW stored api_key, so a caller can reuse it when the operator didn't
    re-type it. Server-side only — never return this to a client.

    `provider` guards against handing an Anthropic key to an OpenAI-compatible
    server (or vice versa): the key is only returned if the stored credential is
    for that same provider.
    """
    try:
        cred = CredentialStore().get(LLM_CREDENTIAL_KEY)
    except CredentialStoreError:
        return None
    if provider is not None and cred.get("provider", DEFAULT_PROVIDER) != provider:
        return None
    return cred.get("api_key") or None


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
    """Load the stored provider config into the env vars orchestration/agent.py
    reads (LLM_PROVIDER, plus ANTHROPIC_API_KEY/AGENT_MODEL or OMLX_*). Returns
    True if a usable provider was loaded. No-op-safe if nothing is stored."""
    try:
        cred = CredentialStore().get(LLM_CREDENTIAL_KEY)
    except CredentialStoreError:
        return False
    provider = cred.get("provider", DEFAULT_PROVIDER)
    os.environ["LLM_PROVIDER"] = provider

    if provider in ("anthropic", "claude"):
        api_key = cred.get("api_key")
        if not api_key:
            return False
        os.environ["ANTHROPIC_API_KEY"] = api_key
        if cred.get("model"):
            os.environ["AGENT_MODEL"] = cred["model"]
        return True

    # openai_compatible / local — a key isn't always required
    if cred.get("base_url"):
        os.environ["OMLX_BASE_URL"] = cred["base_url"]
    if cred.get("model"):
        os.environ["OMLX_MODEL"] = cred["model"]
    os.environ["OMLX_API_KEY"] = cred.get("api_key") or "local"
    return bool(cred.get("base_url"))
