# Template candidate: generic (tier 1) — no client-specific logic.
# See agent-harness-template/docs/promotion-log.md.
"""OS-independent encrypted local credential store.

Credentials live encrypted on disk (`.secrets/credentials.enc`, gitignored) so
the store works identically on macOS, Linux (e.g. a Raspberry Pi), or anywhere
else the agent runs — no dependency on an OS keychain API or a third-party
secrets SaaS. The encryption key itself is never written to disk as part of
this repo; it must be supplied via the AGENT_SECRET_KEY environment variable.

This module must stay framework-free (no langgraph/langchain imports).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

DEFAULT_STORE_PATH = Path(".secrets/credentials.enc")
SECRET_KEY_ENV_VAR = "AGENT_SECRET_KEY"


class CredentialStoreError(RuntimeError):
    pass


def generate_key() -> str:
    """Generate a new Fernet key, to be stored by the operator (e.g. in a
    password manager or the OS's own env-var mechanism) — never in the repo."""
    return Fernet.generate_key().decode("utf-8")


def _get_fernet() -> Fernet:
    key = os.environ.get(SECRET_KEY_ENV_VAR)
    if not key:
        raise CredentialStoreError(
            f"{SECRET_KEY_ENV_VAR} is not set. Generate one with "
            "credential_store.generate_key() and export it before running the agent."
        )
    return Fernet(key.encode("utf-8"))


class CredentialStore:
    def __init__(self, store_path: Path = DEFAULT_STORE_PATH):
        self.store_path = store_path

    def _load(self) -> dict[str, dict[str, str]]:
        if not self.store_path.exists():
            return {}
        fernet = _get_fernet()
        encrypted = self.store_path.read_bytes()
        try:
            decrypted = fernet.decrypt(encrypted)
        except InvalidToken as exc:
            raise CredentialStoreError(
                f"Could not decrypt {self.store_path} — wrong {SECRET_KEY_ENV_VAR} "
                "or corrupted file."
            ) from exc
        return json.loads(decrypted.decode("utf-8"))

    def _save(self, data: dict[str, dict[str, str]]) -> None:
        fernet = _get_fernet()
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        encrypted = fernet.encrypt(json.dumps(data).encode("utf-8"))
        self.store_path.write_bytes(encrypted)
        self.store_path.chmod(0o600)

    def get(self, service_key: str) -> dict[str, str]:
        data = self._load()
        if service_key not in data:
            raise CredentialStoreError(f"No credentials stored for '{service_key}'.")
        return data[service_key]

    def set(self, service_key: str, **fields: str) -> None:
        data = self._load()
        data[service_key] = fields
        self._save(data)

    def list_services(self) -> list[str]:
        return sorted(self._load().keys())
