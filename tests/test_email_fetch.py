"""Deterministic parts of the email-fetch wiring.

The live Gmail/OAuth calls need real credentials and can only be exercised
end-to-end by the operator; these cover everything that DOESN'T touch the
network: the search-query builder, the fetch config round-tripping through
services.yaml, the attachment-tree walk, and the routing guards.
"""

from pathlib import Path

import pytest

import core.fetch_ingest as fetch_ingest
from core.ingest import IngestError
from core.tools.email_fetcher import _attachment_parts, build_gmail_query
from core.tools.service_manifest import FetchConfig, Service, ServiceManifest


def test_build_gmail_query_full():
    cfg = FetchConfig(
        provider="gmail",
        delivers_to="epic_property_management",
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
    cfg = FetchConfig(provider="gmail", delivers_to="epic_property_management")
    assert build_gmail_query(cfg) == "has:attachment"


def test_fetch_config_round_trips_through_manifest(tmp_path):
    manifest = ServiceManifest(tmp_path / "services.yaml")
    manifest.add(Service(key="email", label="Email", input_type="email_trigger"))
    manifest.add(Service(key="epic", label="Epic", parser="buildium_owner_statement",
                         status="implemented"))
    manifest.set_fetch("email", FetchConfig(
        provider="gmail", delivers_to="epic",
        from_address="donotreply@managebuilding.com", attachment_suffix=".pdf",
    ))

    reloaded = ServiceManifest(tmp_path / "services.yaml").get("email")
    assert reloaded.fetch is not None
    assert reloaded.fetch.delivers_to == "epic"
    assert reloaded.fetch.from_address == "donotreply@managebuilding.com"
    assert reloaded.fetch.attachment_suffix == ".pdf"
    # untouched sources still parse
    assert ServiceManifest(tmp_path / "services.yaml").get("epic").fetch is None


def test_attachment_parts_walks_nested_payload():
    payload = {
        "parts": [
            {"filename": "", "body": {}},  # text body, no attachment
            {"filename": "statement.pdf", "body": {"attachmentId": "att-1"}},
            {"parts": [{"filename": "nested.pdf", "body": {"attachmentId": "att-2"}}]},
        ]
    }
    assert _attachment_parts(payload) == [("statement.pdf", "att-1"), ("nested.pdf", "att-2")]


def test_fetch_and_ingest_requires_fetch_config(tmp_path):
    manifest = ServiceManifest(tmp_path / "services.yaml")
    manifest.add(Service(key="email", label="Email", input_type="email_trigger"))
    with pytest.raises(IngestError, match="no fetch config"):
        fetch_ingest.fetch_and_ingest("email", manifest=manifest)


def test_fetch_and_ingest_refuses_delivery_to_source_without_parser(tmp_path):
    manifest = ServiceManifest(tmp_path / "services.yaml")
    manifest.add(Service(key="email", label="Email", input_type="email_trigger"))
    manifest.add(Service(key="epic", label="Epic", status="planned"))  # no active parser
    manifest.set_fetch("email", FetchConfig(provider="gmail", delivers_to="epic"))
    with pytest.raises(IngestError, match="no active parser"):
        fetch_ingest.fetch_and_ingest("email", manifest=manifest)
