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

import os

from core.tools.credential_store import CredentialStore, CredentialStoreError

# Reserved key under which the LLM credential lives in the credential store —
# stored "alongside the rest" of the secrets, but not a data source.
LLM_CREDENTIAL_KEY = "llm_provider"

DEFAULT_PROVIDER = "anthropic"
# provider -> the env var that provider's SDK reads for its API key.
PROVIDER_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    # future: "openai": "OPENAI_API_KEY", "google": "GOOGLE_API_KEY", ...
}


def is_configured() -> bool:
    """True if an LLM credential is stored (needs the master key to check)."""
    try:
        CredentialStore().get(LLM_CREDENTIAL_KEY)
        return True
    except CredentialStoreError:
        return False


def store_llm_credential(api_key: str, provider: str = DEFAULT_PROVIDER) -> None:
    CredentialStore().set(LLM_CREDENTIAL_KEY, provider=provider, api_key=api_key)


def configured_provider() -> str | None:
    try:
        return CredentialStore().get(LLM_CREDENTIAL_KEY).get("provider", DEFAULT_PROVIDER)
    except CredentialStoreError:
        return None


def load_into_env() -> bool:
    """Load the stored LLM key into the env var its SDK expects. Returns True if
    a credential was loaded. No-op-safe if nothing is stored."""
    try:
        cred = CredentialStore().get(LLM_CREDENTIAL_KEY)
    except CredentialStoreError:
        return False
    provider = cred.get("provider", DEFAULT_PROVIDER)
    env_var = PROVIDER_ENV.get(provider)
    api_key = cred.get("api_key")
    if env_var and api_key:
        os.environ[env_var] = api_key
        return True
    return False
