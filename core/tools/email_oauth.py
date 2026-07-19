# Template candidate: generic (tier 1) — no client-specific logic.
# See agent-harness-template/docs/promotion-log.md.
"""Gmail OAuth for the email fetcher — the harness's own inbox access, stored
as a secret like every other credential.

Why OAuth (not IMAP + password): Google Workspace permanently disabled basic
authentication for IMAP/POP/SMTP in May 2025, so a password/app-password can no
longer read a Workspace mailbox. OAuth is the only path Google leaves open.

The flow, all inside the harness:
1. The operator creates an OAuth *client* in Google Cloud (Desktop app,
   gmail.readonly scope) and downloads its client-secret JSON. That file holds
   client_id/client_secret — not the mailbox itself.
2. run_consent() runs Google's installed-app flow: a browser opens, the operator
   clicks "Allow" once, and Google returns a refresh token.
3. We store the refresh token (+ the client id/secret needed to refresh it)
   encrypted in the credential store. From then on load_credentials() mints
   short-lived access tokens headlessly — no browser, works on a Raspberry Pi.

Read-only scope: the harness can find and download the statement attachment but
can never send, delete, or modify mail. Least privilege.

This module must stay framework-free (no langgraph/langchain imports).
"""

from __future__ import annotations

import json
from pathlib import Path

from core.observability import get_logger
from core.tools.credential_store import CredentialStore, CredentialStoreError

# Read-only: list/read messages and download attachments; nothing else.
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TOKEN_URI = "https://oauth2.googleapis.com/token"

log = get_logger("core.tools.email_oauth")


def _credential_key(source_key: str) -> str:
    """One encrypted entry per fetched source (a harness could have several inboxes)."""
    return f"email_oauth::{source_key}"


def is_configured(source_key: str) -> bool:
    """True if a usable OAuth token is stored for this source."""
    try:
        cred = CredentialStore().get(_credential_key(source_key))
    except CredentialStoreError:
        return False
    return bool(cred.get("refresh_token"))


def account_email(source_key: str) -> str | None:
    """The email address that was authorized (recorded at consent, for display)."""
    try:
        return CredentialStore().get(_credential_key(source_key)).get("account_email")
    except CredentialStoreError:
        return None


def _load_client_config(client_secret_path: Path) -> dict:
    """Read the Desktop-app client-secret JSON downloaded from Google Cloud.
    Google wraps it under an "installed" (or "web") top-level key."""
    raw = json.loads(Path(client_secret_path).expanduser().read_text())
    inner = raw.get("installed") or raw.get("web")
    if not inner or "client_id" not in inner:
        raise ValueError(log.failure(
            operation="load_client_config",
            code="BAD_CLIENT_JSON",
            message="That file isn't an OAuth client-secret JSON (no installed/web client_id).",
            remediation="Download it from Google Cloud Console → APIs & Services → Credentials → "
                        "your OAuth client (Desktop app) → Download JSON.",
            context={"client_secret_path": str(client_secret_path)},
        ))
    return inner


def run_consent(source_key: str, client_secret_path: Path) -> str:
    """Run Google's installed-app consent flow (opens a browser, operator clicks
    Allow once), then store the resulting refresh token encrypted. Returns the
    authorized account's email address. Requires a browser on this machine — do
    the initial consent on a workstation, then the stored token runs headless."""
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    inner = _load_client_config(client_secret_path)
    client_config = {"installed": inner}
    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")

    if not creds.refresh_token:
        raise RuntimeError(log.failure(
            operation="run_consent",
            code="OAUTH_NO_REFRESH_TOKEN",
            message="Google didn't return a refresh token during consent.",
            remediation="Revoke the app at myaccount.google.com/permissions and run setup again "
                        "so Google re-prompts for consent.",
            context={"source_key": source_key},
        ))

    # Confirm the token works and learn which account was authorized.
    if not creds.valid:
        creds.refresh(Request())
    service = build("gmail", "v1", credentials=creds)
    profile = service.users().getProfile(userId="me").execute()
    email = profile.get("emailAddress", "")

    CredentialStore().set(
        _credential_key(source_key),
        provider="gmail",
        account_email=email,
        client_id=inner["client_id"],
        client_secret=inner["client_secret"],
        refresh_token=creds.refresh_token,
        scopes=" ".join(SCOPES),
    )
    return email


def load_credentials(source_key: str):
    """Rebuild live Google credentials from the stored refresh token (mints a
    fresh access token; no browser). Returns a google.oauth2.credentials
    object ready for the Gmail API."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    try:
        cred = CredentialStore().get(_credential_key(source_key))
    except CredentialStoreError as exc:
        raise CredentialStoreError(log.failure(
            operation="load_credentials",
            code="OAUTH_NOT_CONFIGURED",
            message=f"Email fetch isn't set up for '{source_key}' yet.",
            remediation="Run the email fetch setup walkthrough in the TUI first.",
            context={"source_key": source_key},
            exc=exc,
        )) from exc

    creds = Credentials(
        token=None,
        refresh_token=cred["refresh_token"],
        client_id=cred["client_id"],
        client_secret=cred["client_secret"],
        token_uri=TOKEN_URI,
        scopes=cred.get("scopes", " ".join(SCOPES)).split(),
    )
    creds.refresh(Request())
    return creds
