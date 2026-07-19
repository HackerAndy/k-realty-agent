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

from core.observability import get_logger

DEFAULT_MANIFEST_PATH = Path("core/policies/services.yaml")

log = get_logger("core.tools.service_manifest")

# Round-trip YAML preserves comments (the header roadmap, any inline notes) and
# formatting across edits — activating a parser no longer wipes the docs.
_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.width = 4096  # don't hard-wrap long note lines

_FIELD_ORDER = ["key", "label", "login_url", "notes", "input_type", "access", "parser", "status", "fetch"]


class FetchConfig(BaseModel):
    """How a *fetched* source (e.g. an inbox) pulls its document and where that
    document goes. Non-secret — lives visibly in services.yaml. The secret half
    (OAuth token) is stored encrypted separately (core/tools/email_oauth.py).

    A fetched source doesn't parse anything itself: it retrieves a document and
    routes it to `delivers_to`, whose committed parser does the parsing. So the
    email inbox that carries the Epic PDF has delivers_to=epic_property_management
    and reuses that source's already-built parser.
    """

    provider: str = "gmail"  # gmail (OAuth) today; imap (app password) for other providers later
    delivers_to: str  # source_key whose parser handles the fetched document
    # Search criteria — narrow to the one message that carries the document:
    from_address: str | None = None
    subject_contains: str | None = None
    attachment_suffix: str | None = None  # e.g. ".pdf" — only pull matching attachments
    newer_than_days: int | None = None  # bound the search window (Gmail newer_than:)


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

    # Present only on fetched sources (inboxes, etc.) — see FetchConfig.
    fetch: FetchConfig | None = None


class ServiceManifestError(RuntimeError):
    pass


class ServiceManifest:
    def __init__(self, manifest_path: Path = DEFAULT_MANIFEST_PATH):
        self.manifest_path = manifest_path

    def _not_found(self, key: str) -> ServiceManifestError:
        return ServiceManifestError(log.failure(
            operation="service_lookup",
            code="SERVICE_NOT_FOUND",
            message=f"Service '{key}' not found.",
            remediation="Check the key against core/policies/services.yaml.",
            context={"service_key": key, "manifest": str(self.manifest_path)},
        ))

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
            if value is None:
                continue
            if field == "fetch":  # nested model → nested map
                fetch_map = CommentedMap()
                for k, v in value.model_dump(exclude_none=True).items():
                    fetch_map[k] = v
                entry[field] = fetch_map
            else:
                entry[field] = value
        return entry

    def load(self) -> list[Service]:
        return [Service.model_validate(dict(e)) for e in self._load_doc()["services"]]

    def add(self, service: Service) -> None:
        doc = self._load_doc()
        if any(e.get("key") == service.key for e in doc["services"]):
            raise ServiceManifestError(log.failure(
                operation="add_service",
                code="SERVICE_EXISTS",
                message=f"Service '{service.key}' already exists.",
                remediation="Use a different key, or edit/remove the existing service.",
                context={"service_key": service.key},
            ))
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
        raise self._not_found(key)

    def set_fetch(self, key: str, config: FetchConfig) -> None:
        """Attach/replace a source's fetch config IN PLACE (comments preserved).
        The routing + search criteria live here visibly; the OAuth token is a
        separate encrypted secret (core/tools/email_oauth.py)."""
        doc = self._load_doc()
        for entry in doc["services"]:
            if entry.get("key") == key:
                fetch_map = CommentedMap()
                for k, v in config.model_dump(exclude_none=True).items():
                    fetch_map[k] = v
                entry["fetch"] = fetch_map
                self._dump_doc(doc)
                return
        raise self._not_found(key)

    def remove(self, key: str) -> None:
        doc = self._load_doc()
        for i, entry in enumerate(doc["services"]):
            if entry.get("key") == key:
                del doc["services"][i]
                self._dump_doc(doc)
                return
        raise self._not_found(key)

    def get(self, key: str) -> Service:
        for service in self.load():
            if service.key == key:
                return service
        raise self._not_found(key)
