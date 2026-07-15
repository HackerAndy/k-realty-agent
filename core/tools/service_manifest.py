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

from pydantic import BaseModel
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

DEFAULT_MANIFEST_PATH = Path("core/policies/services.yaml")

# Round-trip YAML preserves comments (the header roadmap, any inline notes) and
# formatting across edits — activating a parser no longer wipes the docs.
_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.width = 4096  # don't hard-wrap long note lines

_FIELD_ORDER = ["key", "label", "login_url", "notes", "input_type", "access", "parser", "status"]


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

    def _load_doc(self) -> CommentedMap:
        """Load the raw round-trip document (comments intact)."""
        if not self.manifest_path.exists():
            doc = CommentedMap()
            doc["services"] = []
            return doc
        with self.manifest_path.open() as f:
            doc = _yaml.load(f) or CommentedMap()
        if not doc.get("services"):
            doc["services"] = []
        return doc

    def _dump_doc(self, doc: CommentedMap) -> None:
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        with self.manifest_path.open("w") as f:
            _yaml.dump(doc, f)

    def _entry(self, service: Service) -> CommentedMap:
        entry = CommentedMap()
        for field in _FIELD_ORDER:
            value = getattr(service, field)
            if value is not None:
                entry[field] = value
        return entry

    def load(self) -> list[Service]:
        return [Service.model_validate(dict(e)) for e in self._load_doc()["services"]]

    def add(self, service: Service) -> None:
        doc = self._load_doc()
        if any(e.get("key") == service.key for e in doc["services"]):
            raise ServiceManifestError(f"Service '{service.key}' already exists.")
        doc["services"].append(self._entry(service))
        self._dump_doc(doc)

    def update(self, key: str, **fields: str | None) -> None:
        """Change fields on one service IN PLACE — only the touched keys move,
        so that entry's (and the file's) comments are preserved."""
        doc = self._load_doc()
        for entry in doc["services"]:
            if entry.get("key") == key:
                for field, value in fields.items():
                    if value is None:
                        entry.pop(field, None)
                    else:
                        entry[field] = value
                self._dump_doc(doc)
                return
        raise ServiceManifestError(f"Service '{key}' not found.")

    def remove(self, key: str) -> None:
        doc = self._load_doc()
        for i, entry in enumerate(doc["services"]):
            if entry.get("key") == key:
                del doc["services"][i]
                self._dump_doc(doc)
                return
        raise ServiceManifestError(f"Service '{key}' not found.")

    def get(self, key: str) -> Service:
        for service in self.load():
            if service.key == key:
                return service
        raise ServiceManifestError(f"Service '{key}' not found.")
