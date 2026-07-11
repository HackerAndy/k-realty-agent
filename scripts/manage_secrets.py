#!/usr/bin/env python3
"""Interactive CLI for setting credentials in the local encrypted store.

Input is read with getpass, so values are never echoed to the terminal,
never printed, and never logged. Run this yourself to enter real
credentials — nothing here transmits or displays secret values.

Usage:
    poetry run python scripts/manage_secrets.py generate-key
    poetry run python scripts/manage_secrets.py set <service_key>
    poetry run python scripts/manage_secrets.py list
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.tools.credential_store import (  # noqa: E402
    SECRET_KEY_ENV_VAR,
    CredentialStore,
    CredentialStoreError,
    generate_key,
)


def cmd_generate_key(_args: argparse.Namespace) -> int:
    key = generate_key()
    print(f"Generated key. Store it somewhere safe (e.g. your password manager),")
    print(f"then export it before running the agent or this CLI again:\n")
    print(f"  export {SECRET_KEY_ENV_VAR}='{key}'\n")
    print("This key is not saved anywhere by this tool.")
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    store = CredentialStore()
    print(f"Setting credentials for '{args.service_key}'. Input is hidden.")
    username = getpass.getpass("username/email: ")
    password = getpass.getpass("password: ")
    store.set(args.service_key, username=username, password=password)
    print(f"Saved encrypted credentials for '{args.service_key}'.")
    return 0


def cmd_list(_args: argparse.Namespace) -> int:
    store = CredentialStore()
    services = store.list_services()
    if not services:
        print("No credentials stored yet.")
        return 0
    print("Services with stored credentials:")
    for service in services:
        print(f"  - {service}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("generate-key", help="Generate a new encryption key").set_defaults(
        func=cmd_generate_key
    )

    set_parser = subparsers.add_parser("set", help="Set credentials for a service")
    set_parser.add_argument("service_key", help="e.g. epic_property_management, dfcu_bank")
    set_parser.set_defaults(func=cmd_set)

    subparsers.add_parser("list", help="List services with stored credentials").set_defaults(
        func=cmd_list
    )

    args = parser.parse_args()
    try:
        return args.func(args)
    except CredentialStoreError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
