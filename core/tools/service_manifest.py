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

_FIELD_ORDER = ["key", "label", "login_url", "notes", "input_type", "access", "provider",
                "parser", "status", "default_transport", "email_search"]


class EmailSearch(BaseModel):
    """How to find THIS source's document in an inbox.

    Deliberately stored on the SOURCE, not on the inbox. Getting into an inbox
    (Google consent, a token) is an access question and belongs with the other
    sign-ins; "the sender is mail@managebuilding.com and the subject says Owner's
    Statement" is ingestion configuration for one particular body of data. One
    inbox carries statements from many senders, so the search terms cannot live
    on the inbox — that limited a connected account to a single source.

    Non-secret: lives visibly in services.yaml. The secret half (the OAuth token)
    is stored encrypted per inbox (core/tools/email_oauth.py).
    """

    carrier: str  # key of the inbox service to search (its token is the access)
    # Search criteria — narrow to the one message that carries the document:
    from_address: str | None = None
    subject_contains: str | None = None
    attachment_suffix: str | None = None  # e.g. ".pdf" — only pull matching attachments
    newer_than_days: int | None = None  # bound the search window (Gmail newer_than:)


def _as_map(model: BaseModel) -> CommentedMap:
    """A pydantic model as a YAML map, so nested config round-trips with comments."""
    out = CommentedMap()
    for field, value in model.model_dump(exclude_none=True).items():
        out[field] = value
    return out


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

    # Inboxes only: how the harness gets into this one (gmail today; imap later).
    # An inbox is a way IN, not a body of data — it has no parser of its own.
    provider: str | None = None

    # Which transport "Get latest" runs for this source (upload | scrape | email).
    # See core/transports.py — a source can arrive by several routes; this pins
    # the preferred one. Automation, when enabled, runs this same route.
    default_transport: str | None = None

    # Set when this source's document ARRIVES BY EMAIL — which inbox to search
    # and what to look for. See EmailSearch: the inbox itself only holds access.
    email_search: EmailSearch | None = None


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
        raw = self.manifest_path.read_text()
        try:
            doc = _yaml.load(raw) or CommentedMap()
        except Exception as exc:
            # If an accidental YAML document separator (---) slips into the file,
            # merge all documents so the app stays operable and logs a warning.
            parser = YAML()
            parser.preserve_quotes = True
            parser.width = 4096
            try:
                docs = list(parser.load_all(raw))
            except Exception as parse_exc:  # pragma: no cover - defensive path
                raise ServiceManifestError(log.failure(
                    operation="load_manifest",
                    code="MANIFEST_INVALID_YAML",
                    message=f"Could not parse {self.manifest_path} as YAML.",
                    remediation="Fix YAML syntax in core/policies/services.yaml and retry.",
                    context={"manifest": str(self.manifest_path)},
                    exc=parse_exc,
                )) from parse_exc

            merged = CommentedMap()
            merged["services"] = []
            for i, item in enumerate(docs):
                if item is None:
                    continue
                if not isinstance(item, dict):
                    raise ServiceManifestError(log.failure(
                        operation="load_manifest",
                        code="MANIFEST_INVALID_YAML",
                        message=f"YAML document #{i + 1} in {self.manifest_path} is not a mapping.",
                        remediation="Each YAML document must be a mapping with a 'services' list.",
                        context={"manifest": str(self.manifest_path), "document_index": i + 1},
                        exc=exc,
                    )) from exc
                services = item.get("services")
                if services:
                    merged["services"].extend(services)

            log.event(
                operation="load_manifest",
                code="MANIFEST_MULTI_DOC_MERGED",
                message=(
                    f"Loaded {self.manifest_path} as {len(docs)} YAML documents and merged 'services'. "
                    "Remove accidental '---' separators to keep a single-document manifest."
                ),
                context={"manifest": str(self.manifest_path), "documents": len(docs)},
                level="warning",
            )
            doc = merged
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
            if field == "email_search":  # nested model → nested map
                entry[field] = _as_map(value)
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

    def set_email_search(self, key: str, search: EmailSearch) -> None:
        """Attach/replace how this source's document is found in an inbox, IN
        PLACE (comments preserved). Search terms are configuration and live here
        visibly; the inbox's OAuth token is a separate encrypted secret
        (core/tools/email_oauth.py)."""
        doc = self._load_doc()
        for entry in doc["services"]:
            if entry.get("key") == key:
                entry["email_search"] = _as_map(search)
                self._dump_doc(doc)
                return
        raise self._not_found(key)

    def clear_email_search(self, key: str) -> None:
        """Drop the email route from a source. Its inbox stays connected — other
        sources may arrive through the same one."""
        doc = self._load_doc()
        for entry in doc["services"]:
            if entry.get("key") == key:
                entry.pop("email_search", None)
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
