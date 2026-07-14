# Template candidate: generic (tier 1) — no client-specific logic.
# See agent-harness-template/docs/promotion-log.md.
"""The service manifest: a small, hand-maintainable list of external
services this agent needs credentials for (key, label, login URL, notes).

This is the "gold reference" services.yaml lives at, kept separate from
clients/<slug>/intake.yaml in the template repo — the intake is a point-in-
time interview record; this manifest is a living list an operator adds to,
edits, or removes from as the agent's actual integrations change over time.
It can be bootstrapped from an intake once, but isn't tied to it afterward.

This module must stay framework-free (no langgraph/langchain imports).
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel

DEFAULT_MANIFEST_PATH = Path("core/policies/services.yaml")


class Service(BaseModel):
    key: str
    label: str
    login_url: str | None = None
    notes: str | None = None

    # How this source's data is handled (seeded from intake, refined per source):
    #   input_type: pdf_statement | csv_export | html_scrape | email_trigger | unknown
    #   access:     email_attachment | portal_login | download | api
    #   parser:     name registered in core/parsers/REGISTRY, or null if not built
    #   status:     implemented | needs_parser | planned
    input_type: str | None = None
    access: str | None = None
    parser: str | None = None
    status: str = "planned"


class ServiceManifestError(RuntimeError):
    pass


class ServiceManifest:
    def __init__(self, manifest_path: Path = DEFAULT_MANIFEST_PATH):
        self.manifest_path = manifest_path

    def load(self) -> list[Service]:
        if not self.manifest_path.exists():
            return []
        data = yaml.safe_load(self.manifest_path.read_text()) or {}
        return [Service.model_validate(entry) for entry in data.get("services", [])]

    def save(self, services: list[Service]) -> None:
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"services": [s.model_dump(exclude_none=True) for s in services]}
        self.manifest_path.write_text(yaml.safe_dump(data, sort_keys=False))

    def add(self, service: Service) -> None:
        services = self.load()
        if any(s.key == service.key for s in services):
            raise ServiceManifestError(f"Service '{service.key}' already exists.")
        services.append(service)
        self.save(services)

    def update(self, key: str, **fields: str | None) -> None:
        services = self.load()
        for i, service in enumerate(services):
            if service.key == key:
                services[i] = service.model_copy(update=fields)
                self.save(services)
                return
        raise ServiceManifestError(f"Service '{key}' not found.")

    def remove(self, key: str) -> None:
        services = self.load()
        remaining = [s for s in services if s.key != key]
        if len(remaining) == len(services):
            raise ServiceManifestError(f"Service '{key}' not found.")
        self.save(remaining)

    def get(self, key: str) -> Service:
        for service in self.load():
            if service.key == key:
                return service
        raise ServiceManifestError(f"Service '{key}' not found.")
