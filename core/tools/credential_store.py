# Template candidate: generic (tier 1) — no client-specific logic.
# See agent-harness-template/docs/promotion-log.md.
"""OS-independent encrypted local credential store.

Credentials live encrypted on disk (`.secrets/credentials.enc`, gitignored) so
the store works identically on macOS, Linux (e.g. a Raspberry Pi), or anywhere
else the agent runs — no dependency on an OS keychain API or a third-party
secrets SaaS.

The encryption (master) key is resolved in order: the AGENT_SECRET_KEY
environment variable (most secure — never touches disk; use this for
CI/hardened setups), else a local `.secrets/master.key` file that the harness
auto-generates on first run (so the operator never has to manage a key by
hand). The file lives beside the ciphertext and is gitignored + chmod 600 —
a usability/security tradeoff appropriate for a single-operator local harness;
set AGENT_SECRET_KEY in the environment instead if you want the key off disk.

This module must stay framework-free (no langgraph/langchain imports).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

DEFAULT_STORE_PATH = Path(".secrets/credentials.enc")
MASTER_KEY_PATH = DEFAULT_STORE_PATH.parent / "master.key"
SECRET_KEY_ENV_VAR = "AGENT_SECRET_KEY"


class CredentialStoreError(RuntimeError):
    pass


def generate_key() -> str:
    """Generate a new Fernet key."""
    return Fernet.generate_key().decode("utf-8")


def _read_key() -> str | None:
    key = os.environ.get(SECRET_KEY_ENV_VAR)
    if key:
        return key
    if MASTER_KEY_PATH.exists():
        return MASTER_KEY_PATH.read_text().strip()
    return None


def ensure_master_key() -> str:
    """Make sure a master key exists so the store is usable without the operator
    managing one by hand. Returns one of 'env' / 'file:existing' / 'file:created'.
    Generates + persists a local key file only when neither an env var nor a
    file is present."""
    if os.environ.get(SECRET_KEY_ENV_VAR):
        return "env"
    if MASTER_KEY_PATH.exists():
        return "file:existing"
    MASTER_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    MASTER_KEY_PATH.write_text(generate_key())
    MASTER_KEY_PATH.chmod(0o600)
    return "file:created"


def _get_fernet() -> Fernet:
    key = _read_key()
    if not key:
        raise CredentialStoreError(
            f"No master key found. Set {SECRET_KEY_ENV_VAR}, or call "
            "credential_store.ensure_master_key() to create a local one."
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
                f"Could not decrypt {self.store_path} — the master key "
                f"({SECRET_KEY_ENV_VAR} or {MASTER_KEY_PATH}) doesn't match, or the "
                "file is corrupted."
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
