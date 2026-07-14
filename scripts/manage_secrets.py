#!/usr/bin/env python3
"""Interactive TUI for managing the service manifest and credentials.

Two things this manages, kept deliberately separate:

- core/policies/services.yaml — the "gold reference" list of external
  services this agent needs credentials for. Can be bootstrapped from a
  client intake.yaml once (`init --intake <path>`), then maintained by hand
  going forward (`services add/edit/remove/list`) — it's not re-derived
  from the intake automatically after that first bootstrap.
- the encrypted credential store — actual username/password values. Only the
  password is ever masked; username/email is always shown as you type it, so
  you can see and verify exactly what's being entered — never printed to a
  log or transmitted anywhere else either way.

Usage:
    poetry run python scripts/manage_secrets.py generate-key
    poetry run python scripts/manage_secrets.py init --intake <path/to/intake.yaml>
    poetry run python scripts/manage_secrets.py services list
    poetry run python scripts/manage_secrets.py services add
    poetry run python scripts/manage_secrets.py services edit <key>
    poetry run python scripts/manage_secrets.py services remove <key>
    poetry run python scripts/manage_secrets.py setup
    poetry run python scripts/manage_secrets.py set <service_key>
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import questionary
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.tools.credential_store import (  # noqa: E402
    SECRET_KEY_ENV_VAR,
    CredentialStore,
    CredentialStoreError,
    generate_key,
)
from core.tools.service_manifest import Service, ServiceManifest, ServiceManifestError  # noqa: E402


def cmd_generate_key(_args: argparse.Namespace) -> int:
    key = generate_key()
    print("Generated key. Store it somewhere safe (e.g. your password manager),")
    print("then export it before running the agent or this CLI again:\n")
    print(f"  export {SECRET_KEY_ENV_VAR}='{key}'\n")
    print("This key is not saved anywhere by this tool.")
    return 0


def _parse_systems_touched(text: str) -> list[Service]:
    """Best-effort parse of a free-text 'Label (detail), Label (detail), ...'
    answer into candidate Service entries. Deliberately heuristic — every
    result is shown to the operator for accept/edit/skip, never auto-applied.
    """
    segments = re.split(r",(?![^(]*\))", text)
    candidates = []
    for segment in segments:
        match = re.match(r"^\s*([^(]+?)\s*\(([^)]+)\)", segment)
        if not match:
            continue
        label = match.group(1).strip()
        detail = match.group(2).strip()
        key = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
        if not key:
            continue
        if "@" in detail:
            candidates.append(Service(key=key, label=label, notes=f"Account: {detail}"))
        else:
            url = detail if detail.startswith("http") else f"https://{detail}"
            candidates.append(Service(key=key, label=label, login_url=url))
    return candidates


def cmd_init(args: argparse.Namespace) -> int:
    intake_path = Path(args.intake)
    if not intake_path.exists():
        print(f"Error: intake file not found at {intake_path}", file=sys.stderr)
        return 1

    intake_data = yaml.safe_load(intake_path.read_text())
    systems_text = intake_data.get("answers", {}).get("systems_touched", "")
    if not systems_text:
        print("No 'systems_touched' answer found in intake — nothing to bootstrap from.")
        return 1

    candidates = _parse_systems_touched(systems_text)
    if not candidates:
        print("Could not parse any services from the intake text — add them manually with 'services add'.")
        return 1

    manifest = ServiceManifest()
    existing = {s.key for s in manifest.load()}
    print(f"Parsed {len(candidates)} candidate service(s) from intake. Review each:\n")

    accepted = 0
    for candidate in candidates:
        if candidate.key in existing:
            print(f"Skipping '{candidate.key}' — already in the manifest.")
            continue
        print(f"\n  Label:     {candidate.label}")
        print(f"  Key:       {candidate.key}")
        print(f"  Login URL: {candidate.login_url or '(none — see notes)'}")
        if candidate.notes:
            print(f"  Notes:     {candidate.notes}")
        action = questionary.select(
            "Add this service to the manifest?",
            choices=["Accept as-is", "Edit before adding", "Skip"],
        ).ask()
        if action in (None, "Skip"):
            continue
        if action == "Edit before adding":
            candidate = _prompt_service(candidate)
        manifest.add(candidate)
        accepted += 1

    print(f"\nAdded {accepted} service(s) to {manifest.manifest_path}.")
    return 0


def _prompt_service(service: Service | None = None) -> Service:
    key = questionary.text("Key (short id, e.g. dfcu_bank):", default=service.key if service else "").ask()
    label = questionary.text("Label:", default=service.label if service else "").ask()
    login_url = questionary.text(
        "Login URL (optional):", default=(service.login_url or "") if service else ""
    ).ask()
    notes = questionary.text("Notes (optional):", default=(service.notes or "") if service else "").ask()
    return Service(key=key, label=label, login_url=login_url or None, notes=notes or None)


def _prompt_credentials(label: str) -> tuple[str, str] | None:
    """Prompt for a username/email and password. Only the password is ever
    masked — username/email is shown as you type it, since it isn't a secret
    and you need to be able to see and verify it (typos here silently break
    login later). Returns None if the user cancels (e.g. Ctrl+C)."""
    print(f"  Entering credentials for {label}:")
    print("  - username/email is shown as you type (not a secret)")
    print("  - password is hidden as you type (this one is a secret)")
    username = questionary.text("  username/email:").ask()
    if username is None:
        return None
    password = questionary.password("  password:").ask()
    if password is None:
        return None
    return username, password


def cmd_services_list(_args: argparse.Namespace) -> int:
    services = ServiceManifest().load()
    if not services:
        print("No services in the manifest yet. Run 'init --intake <path>' or 'services add'.")
        return 0
    stored = set()
    if os.environ.get(SECRET_KEY_ENV_VAR):
        stored = set(CredentialStore().list_services())
    for service in services:
        status = "credentials set" if service.key in stored else "no credentials "
        print(f"  [{status}] {service.key:30} {service.label}")
    return 0


def cmd_services_add(_args: argparse.Namespace) -> int:
    service = _prompt_service()
    try:
        ServiceManifest().add(service)
    except ServiceManifestError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Added '{service.key}' to the manifest.")
    return 0


def cmd_services_edit(args: argparse.Namespace) -> int:
    manifest = ServiceManifest()
    try:
        existing = manifest.get(args.key)
    except ServiceManifestError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    updated = _prompt_service(existing)
    manifest.update(existing.key, label=updated.label, login_url=updated.login_url, notes=updated.notes)
    print(f"Updated '{existing.key}'.")
    return 0


def cmd_services_remove(args: argparse.Namespace) -> int:
    manifest = ServiceManifest()
    confirmed = questionary.confirm(f"Remove '{args.key}' from the manifest?", default=False).ask()
    if not confirmed:
        print("Cancelled.")
        return 0
    try:
        manifest.remove(args.key)
    except ServiceManifestError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Removed '{args.key}' from the manifest (any stored credentials are untouched).")
    return 0


def cmd_setup(_args: argparse.Namespace) -> int:
    services = ServiceManifest().load()
    if not services:
        print("No services in the manifest. Run 'init --intake <path>' or 'services add' first.")
        return 1

    store = CredentialStore()
    print(f"About to walk through {len(services)} service(s) from {ServiceManifest().manifest_path}.")
    print(f"Credentials are encrypted and saved to {store.store_path},")
    print(f"readable only with the {SECRET_KEY_ENV_VAR} you have set in this shell.")
    print("For each service: username/email is shown as you type it (not a")
    print("secret); only the password is hidden.")

    stored = set(store.list_services()) if os.environ.get(SECRET_KEY_ENV_VAR) else set()
    total = len(services)
    for i, service in enumerate(services, start=1):
        print(f"\n[{i}/{total}] {service.label} ({service.key})")
        if service.login_url:
            print(f"  {service.login_url}")
        if service.key in stored:
            action = questionary.select(
                "Credentials already set for this service.", choices=["Skip", "Update"]
            ).ask()
            if action != "Update":
                continue
        credentials = _prompt_credentials(service.label)
        if credentials is None:
            print("  Cancelled — leaving this service as-is.")
            continue
        username, password = credentials
        store.set(service.key, username=username, password=password)
        print("  Saved.")
    print(f"\nDone. {total} service(s) reviewed.")
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    credentials = _prompt_credentials(args.service_key)
    if credentials is None:
        print("Cancelled.")
        return 0
    username, password = credentials
    CredentialStore().set(args.service_key, username=username, password=password)
    print(f"Saved encrypted credentials for '{args.service_key}'.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("generate-key", help="Generate a new encryption key").set_defaults(
        func=cmd_generate_key
    )

    init_parser = subparsers.add_parser("init", help="Bootstrap the service manifest from an intake.yaml")
    init_parser.add_argument("--intake", required=True, help="Path to clients/<slug>/intake.yaml")
    init_parser.set_defaults(func=cmd_init)

    subparsers.add_parser("setup", help="Walk through every service in the manifest, setting credentials").set_defaults(
        func=cmd_setup
    )

    set_parser = subparsers.add_parser("set", help="Set credentials for one service directly")
    set_parser.add_argument("service_key")
    set_parser.set_defaults(func=cmd_set)

    services_parser = subparsers.add_parser("services", help="Manage the service manifest")
    services_sub = services_parser.add_subparsers(dest="services_command", required=True)
    services_sub.add_parser("list", help="List services and credential status").set_defaults(
        func=cmd_services_list
    )
    services_sub.add_parser("add", help="Add a service to the manifest").set_defaults(func=cmd_services_add)
    edit_parser = services_sub.add_parser("edit", help="Edit a service in the manifest")
    edit_parser.add_argument("key")
    edit_parser.set_defaults(func=cmd_services_edit)
    remove_parser = services_sub.add_parser("remove", help="Remove a service from the manifest")
    remove_parser.add_argument("key")
    remove_parser.set_defaults(func=cmd_services_remove)

    args = parser.parse_args()
    try:
        return args.func(args)
    except CredentialStoreError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
