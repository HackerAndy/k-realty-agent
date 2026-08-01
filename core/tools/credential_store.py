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

from core.observability import get_logger

# Anchored to the repo, NOT to the working directory. These were relative, so a
# process started anywhere else — a scheduled run, a subprocess, a worker with a
# different cwd — silently found no store and reported "No credentials stored for
# X". That is the same sentence the operator gets when they genuinely never
# entered a password, so it sent them to Settings to re-type a credential that
# was already there and already correct. A store you cannot find is not a store
# that is empty, and the two must never say the same thing.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SECRETS_DIR = REPO_ROOT / ".secrets"
DEFAULT_STORE_PATH = SECRETS_DIR / "credentials.enc"
MASTER_KEY_PATH = SECRETS_DIR / "master.key"
SECRET_KEY_ENV_VAR = "AGENT_SECRET_KEY"

log = get_logger("core.tools.credential_store")


class CredentialStoreError(RuntimeError):
    pass


class CredentialNotFound(CredentialStoreError):
    """This service has no stored credential — the operator's to fix, in Settings.

    Its own type because callers must be able to tell it apart from the store
    being unreadable. "Add your password" and "the vault didn't open" are
    different problems with different fixes, and only one of them is the
    operator's."""


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
            raise CredentialStoreError(log.failure(
                operation="load_store",
                code="DECRYPT_FAILED",
                message=f"Could not decrypt {self.store_path} — the master key doesn't match, "
                        "or the file is corrupted.",
                remediation=f"Confirm {SECRET_KEY_ENV_VAR} or {MASTER_KEY_PATH} is the key that "
                            "encrypted this store; restore it or re-enter credentials.",
                context={"store_path": str(self.store_path), "master_key_path": str(MASTER_KEY_PATH)},
                exc=exc,
            )) from exc
        return json.loads(decrypted.decode("utf-8"))

    def _save(self, data: dict[str, dict[str, str]]) -> None:
        fernet = _get_fernet()
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        encrypted = fernet.encrypt(json.dumps(data).encode("utf-8"))
        self.store_path.write_bytes(encrypted)
        self.store_path.chmod(0o600)

    def get(self, service_key: str) -> dict[str, str]:
        """The stored credential. Raises `CredentialNotFound` if there isn't one.

        Note it RAISES rather than returning None — callers written as
        `if not store.get(key)` never reach their own error message, because the
        exception escapes first. `try_get` is there for that shape.
        """
        data = self._load()
        if service_key not in data:
            stored = sorted(data)
            raise CredentialNotFound(log.failure(
                operation="get_credential",
                code="CREDENTIAL_NOT_FOUND",
                # The store and its contents go in the MESSAGE, not only the
                # structured context, because the message is what reaches the
                # operator. "No credentials stored for X" on its own is equally
                # true of a store that is empty and a store that was looked for in
                # the wrong directory, and only one of those is fixed in Settings.
                message=(
                    f"No credentials stored for '{service_key}' in {self.store_path} "
                    f"(exists: {self.store_path.exists()}; it holds: "
                    f"{', '.join(stored) if stored else 'nothing'})."
                ),
                remediation="Add a username and password for this source under "
                            "Settings → Sign-ins. If the store above is not the one "
                            "you expected, the process is running against a different "
                            "repository or key, and no password will fix that.",
                # The store this looked in, and what IS in it. When the answer is
                # "wrong file" rather than "missing entry", these two lines say so
                # immediately instead of sending the operator to re-type a
                # password that was never the problem.
                context={"service_key": service_key,
                         "store_path": str(self.store_path),
                         "store_exists": self.store_path.exists(),
                         "services_in_store": stored},
            ))
        return data[service_key]

    def try_get(self, service_key: str) -> dict[str, str] | None:
        """The stored credential, or None if this service has none.

        A missing password is an ordinary state — the operator hasn't got to it
        yet — and a caller that wants to say so in its own words shouldn't have to
        catch an exception to find out. A store that won't OPEN still raises:
        that one is never ordinary.
        """
        try:
            return self.get(service_key)
        except CredentialNotFound:
            return None

    def set(self, service_key: str, **fields: str) -> None:
        data = self._load()
        data[service_key] = fields
        self._save(data)

    def delete(self, service_key: str) -> bool:
        """Forget one stored credential. True if there was one to forget."""
        data = self._load()
        if service_key not in data:
            return False
        data.pop(service_key)
        self._save(data)
        return True

    def list_services(self) -> list[str]:
        return sorted(self._load().keys())
