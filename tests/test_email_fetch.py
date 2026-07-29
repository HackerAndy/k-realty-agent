"""Deterministic parts of the email-fetch wiring.

The live Gmail/OAuth calls need real credentials and can only be exercised
end-to-end by the operator; these cover everything that DOESN'T touch the
network: the search-query builder, the email search round-tripping through
services.yaml, the attachment-tree walk, and the guards on fetching.
"""

from pathlib import Path

import pytest

import core.fetch_ingest as fetch_ingest
import core.tools.service_manifest as service_manifest_module
from core.ingest import IngestError
from core.tools.email_fetcher import _attachment_parts, build_gmail_query
from core.tools.service_manifest import EmailSearch, Service, ServiceManifest


def test_build_gmail_query_full():
    cfg = EmailSearch(
        carrier="email",
        from_address="donotreply@managebuilding.com",
        subject_contains="Owner Statement",
        attachment_suffix=".pdf",
        newer_than_days=45,
    )
    q = build_gmail_query(cfg)
    assert "has:attachment" in q
    assert "from:donotreply@managebuilding.com" in q
    assert 'subject:"Owner Statement"' in q
    assert "filename:pdf" in q  # dot stripped
    assert "newer_than:45d" in q


def test_build_gmail_query_minimal():
    cfg = EmailSearch(carrier="email")
    assert build_gmail_query(cfg) == "has:attachment"


def test_the_email_search_round_trips_on_the_source(tmp_path):
    """It hangs off the SOURCE whose document arrives — the inbox entry keeps
    nothing but its provider, because several sources share it."""
    manifest = ServiceManifest(tmp_path / "services.yaml")
    manifest.add(Service(key="email", label="Email", input_type="email_trigger", provider="gmail"))
    manifest.add(Service(key="epic", label="Epic", parser="buildium_owner_statement",
                         status="implemented"))
    manifest.set_email_search("epic", EmailSearch(
        carrier="email", from_address="donotreply@managebuilding.com", attachment_suffix=".pdf",
    ))

    reloaded = ServiceManifest(tmp_path / "services.yaml")
    epic = reloaded.get("epic")
    assert epic.email_search is not None
    assert epic.email_search.carrier == "email"
    assert epic.email_search.from_address == "donotreply@managebuilding.com"
    assert epic.email_search.attachment_suffix == ".pdf"
    assert reloaded.get("email").email_search is None, "an inbox doesn't arrive by email"


def test_clearing_the_search_leaves_the_source_alone(tmp_path):
    manifest = ServiceManifest(tmp_path / "services.yaml")
    manifest.add(Service(key="email", label="Email", input_type="email_trigger"))
    manifest.add(Service(key="epic", label="Epic", parser="p", status="implemented"))
    manifest.set_email_search("epic", EmailSearch(carrier="email"))

    manifest.clear_email_search("epic")

    epic = ServiceManifest(tmp_path / "services.yaml").get("epic")
    assert epic.email_search is None
    assert epic.parser == "p", "dropping a route must not touch the code that reads it"


def test_attachment_parts_walks_nested_payload():
    payload = {
        "parts": [
            {"filename": "", "body": {}},  # text body, no attachment
            {"filename": "statement.pdf", "body": {"attachmentId": "att-1"}},
            {"parts": [{"filename": "nested.pdf", "body": {"attachmentId": "att-2"}}]},
        ]
    }
    assert _attachment_parts(payload) == [("statement.pdf", "att-1"), ("nested.pdf", "att-2")]


def test_fetching_a_source_that_does_not_arrive_by_email_is_refused(tmp_path):
    manifest = ServiceManifest(tmp_path / "services.yaml")
    manifest.add(Service(key="epic", label="Epic", parser="p", status="implemented"))
    with pytest.raises(IngestError, match="doesn't arrive by email"):
        fetch_ingest.fetch_and_ingest("epic", manifest=manifest)


def test_fetching_into_a_source_with_no_parser_is_refused(tmp_path):
    """The fetch would otherwise succeed and the document land nowhere."""
    manifest = ServiceManifest(tmp_path / "services.yaml")
    manifest.add(Service(key="email", label="Email", input_type="email_trigger"))
    manifest.add(Service(key="epic", label="Epic", status="planned"))  # no active parser
    manifest.set_email_search("epic", EmailSearch(carrier="email"))
    with pytest.raises(IngestError, match="no active parser"):
        fetch_ingest.fetch_and_ingest("epic", manifest=manifest)


def test_manifest_load_merges_multiple_yaml_documents(tmp_path):
    path = tmp_path / "services.yaml"
    path.write_text(
        "services:\n"
        "- key: email\n"
        "  label: Email\n"
        "---\n"
        "services:\n"
        "- key: epic\n"
        "  label: Epic\n"
    )

    loaded = ServiceManifest(path).load()
    assert [s.key for s in loaded] == ["email", "epic"]


def test_manifest_load_falls_back_when_single_load_crashes(tmp_path, monkeypatch):
    path = tmp_path / "services.yaml"
    path.write_text(
        "services:\n"
        "- key: email\n"
        "  label: Email\n"
        "---\n"
        "services:\n"
        "- key: epic\n"
        "  label: Epic\n"
    )

    original_load = service_manifest_module._yaml.load

    def boom(_: str):
        raise AttributeError("simulated ruamel crash")

    monkeypatch.setattr(service_manifest_module._yaml, "load", boom)
    try:
        loaded = ServiceManifest(path).load()
    finally:
        monkeypatch.setattr(service_manifest_module._yaml, "load", original_load)

    assert [s.key for s in loaded] == ["email", "epic"]
