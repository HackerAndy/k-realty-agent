"""Exercise the ingest pipeline the TUI actually calls.

Regression guard: the parsers were unit-tested directly, but ingest_source()
— the function the TUI invokes — wasn't, so a stale `transaction_date`
reference in _persist() shipped and crashed on every real ingest. These tests
walk the full path (parse → persist → reload) for a real source.
"""

from pathlib import Path

import core.ingest as ingest
from core.tools.service_manifest import Service, ServiceManifest

FIXTURE = Path(__file__).parent / "fixtures" / "sample_owner_statement.pdf"


def _manifest_with_epic(tmp_path: Path) -> ServiceManifest:
    manifest = ServiceManifest(tmp_path / "services.yaml")
    manifest.add(
        Service(
            key="epic_property_management",
            label="Epic Property Management",
            parser="buildium_owner_statement",
            status="implemented",
        )
    )
    return manifest


def test_ingest_source_parses_persists_and_reloads(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "DATA_DIR", tmp_path / "parsed")
    manifest = _manifest_with_epic(tmp_path)

    run = ingest.ingest_source("epic_property_management", FIXTURE, manifest=manifest)

    assert run["transaction_count"] == 6
    assert run["extraction_method"] == "deterministic_parser"
    assert run["parser"] == "buildium_owner_statement"
    # month_key is derived from Transaction.date — the field whose rename broke this
    assert run["month"] == "2026-05"
    assert Path(run["run_path"]).exists()

    # persisted transactions round-trip back through the model, faithful columns intact
    txns = ingest.transactions_from_run(run)
    assert len(txns) == 6
    assert set(txns[0].fields) >= {"Date", "Property", "Unit", "Account", "Amount"}
    assert txns[0].source_key == "epic_property_management_statement"


def test_ingest_source_refuses_a_source_with_no_parser(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "DATA_DIR", tmp_path / "parsed")
    manifest = ServiceManifest(tmp_path / "services.yaml")
    manifest.add(Service(key="some_bank", label="Some Bank", status="needs_parser"))

    try:
        ingest.ingest_source("some_bank", FIXTURE, manifest=manifest)
    except ingest.IngestError as exc:
        assert "no parser" in str(exc).lower()
    else:
        raise AssertionError("expected IngestError for a source with no parser")


def test_load_latest_parsed_returns_none_when_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "DATA_DIR", tmp_path / "parsed")
    assert ingest.load_latest_parsed() is None
