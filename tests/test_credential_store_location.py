"""Where the credential store lives, and what it says when it isn't there.

Field failure this pins: a run reported "No credentials stored for
'dfcu_financial_bank'" while that credential was present and correct. The store
path was relative (`.secrets/credentials.enc`), so any process started outside
the repo root looked in the wrong directory, found nothing, and reported it as
an empty vault — sending the operator to Settings to re-enter a password that
was never the problem.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from core.tools import credential_store as cs
from core.tools.credential_store import (
    CredentialNotFound,
    CredentialStore,
    CredentialStoreError,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


# --- the location ------------------------------------------------------------
#
# These run in a SUBPROCESS on purpose. conftest isolates the store per test (it
# has to — the real one holds the operator's actual passwords), which also means
# the in-process constants are the fixture's, not the module's. A fresh
# interpreter with a different cwd is both the honest way to read them and an
# exact reproduction of how the bug reached the operator.

def _paths_from(cwd: Path) -> tuple[str, str]:
    script = (
        "import sys; sys.path.insert(0, %r)\n"
        "from core.tools import credential_store as cs\n"
        "print(cs.CredentialStore().store_path)\n"
        "print(cs.MASTER_KEY_PATH)\n"
    ) % str(REPO_ROOT)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=cwd, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    store, key = result.stdout.strip().splitlines()
    return store, key


def test_the_store_is_found_from_a_different_working_directory(tmp_path):
    """The regression itself. Run from anywhere else, the store must still be
    the repo's — not a path that resolves against whatever cwd it inherited."""
    assert _paths_from(tmp_path) == _paths_from(REPO_ROOT)


def test_the_store_and_its_key_are_absolute_and_sit_together(tmp_path):
    store, key = (Path(p) for p in _paths_from(tmp_path))
    assert store.is_absolute() and key.is_absolute()
    assert store.parent == key.parent == REPO_ROOT / ".secrets"


# --- what a miss says --------------------------------------------------------

@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv(cs.SECRET_KEY_ENV_VAR, cs.generate_key())
    return CredentialStore(store_path=tmp_path / "credentials.enc")


def test_a_missing_credential_raises_its_own_type(store):
    """Distinct from a store that won't open — different problem, different fix."""
    with pytest.raises(CredentialNotFound):
        store.get("nobody")


def test_credential_not_found_is_still_a_store_error(store):
    """Existing `except CredentialStoreError` handlers must keep working."""
    with pytest.raises(CredentialStoreError):
        store.get("nobody")


def test_the_message_names_the_store_it_looked_in(store):
    """So "wrong file" and "missing entry" can be told apart at a glance."""
    store.set("present", username="u", password="p")
    with pytest.raises(CredentialNotFound) as exc:
        store.get("absent")
    text = str(exc.value)
    assert str(store.store_path) in text
    assert "present" in text        # what IS in the store
    assert "absent" in text


def test_try_get_returns_none_instead_of_raising(store):
    """The shape callers were already written for: `if not store.try_get(key)`."""
    assert store.try_get("nobody") is None


def test_try_get_returns_the_credential_when_there_is_one(store):
    store.set("dfcu", username="u", password="p")
    assert store.try_get("dfcu") == {"username": "u", "password": "p"}


def test_try_get_still_raises_when_the_STORE_cannot_be_read(store, monkeypatch):
    """An unreadable vault is never an ordinary 'you haven't set that up yet'.
    Swallowing it would report a wrong master key as a missing password."""
    store.set("dfcu", username="u", password="p")
    monkeypatch.setenv(cs.SECRET_KEY_ENV_VAR, cs.generate_key())  # different key
    with pytest.raises(CredentialStoreError):
        store.try_get("dfcu")


def test_a_real_credential_round_trips(store):
    store.set("dfcu", username="u", password="p p ")
    assert store.get("dfcu")["password"] == "p p "   # spaces are part of a password
