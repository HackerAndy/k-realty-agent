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

import json
import os
from urllib import error, request

from core.tools.credential_store import CredentialStore, CredentialStoreError

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


def list_models(base_url: str, api_key: str | None = None) -> list[str]:
    """Ask an OpenAI-compatible server which models it actually has (GET /models),
    so the operator picks a real one instead of guessing. Raises on failure so the
    caller can show why."""
    url = base_url.rstrip("/") + "/models"
    req = request.Request(url, headers={"Authorization": f"Bearer {api_key or 'local'}"})
    try:
        with request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        raise RuntimeError(f"server returned {exc.code} for {url}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"could not reach {url}: {exc.reason}") from exc
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
