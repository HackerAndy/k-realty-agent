"""Exercise the ingest pipeline the TUI actually calls.

Regression guard: the parsers were unit-tested directly, but ingest_source()
— the function the TUI invokes — wasn't, so a stale `transaction_date`
reference in _persist() shipped and crashed on every real ingest. These tests
walk the full path (parse → persist → reload) for a real source.
"""

import json
from datetime import datetime
from pathlib import Path

import core.ingest as ingest
from core.models import Transaction
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


# --- one run per ROUTE ------------------------------------------------------
#
# A source has several ways in, and each is a separate claim about the same
# month. Storing them under one source-month filename meant the last route to
# run destroyed what the others had produced — so the graph could never show a
# route its own count, only "whatever ran last" or nothing.

def _run(monkeypatch_free_key="epic_property_management", *, transport, month="2026-06", n=1):
    """Persist n synthetic rows for a route, dated inside `month`."""
    txns = [
        Transaction(source_key=monkeypatch_free_key, date=datetime.fromisoformat(f"{month}-15"),
                    amount=float(i + 1), description=f"row {i}")
        for i in range(n)
    ]
    return ingest._persist(monkeypatch_free_key, txns, Path("in.pdf"), "deterministic_parser",
                           "some_parser", transport=transport)


def test_two_routes_in_the_same_month_both_survive(tmp_path, monkeypatch):
    """The actual bug: a scrape used to overwrite an upload of the same month."""
    monkeypatch.setattr(ingest, "DATA_DIR", tmp_path / "parsed")

    up = _run(transport="upload", n=23)
    sc = _run(transport="scrape", n=33)

    assert up["run_path"] != sc["run_path"]
    assert Path(up["run_path"]).exists(), "the upload's rows are still there"
    assert ingest.load_latest_parsed_for("epic_property_management", "upload")[
        "transaction_count"] == 23
    assert ingest.load_latest_parsed_for("epic_property_management", "scrape")[
        "transaction_count"] == 33


def test_a_route_rerun_replaces_only_its_own(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "DATA_DIR", tmp_path / "parsed")
    _run(transport="upload", n=23)
    _run(transport="scrape", n=33)

    _run(transport="scrape", n=41)

    by_route = ingest.runs_by_transport("epic_property_management")
    assert by_route["scrape"]["count"] == 41
    assert by_route["upload"]["count"] == 23, "the other route is untouched"


def test_a_route_that_never_ran_is_absent_rather_than_borrowing(tmp_path, monkeypatch):
    """What "Not run · by this route" is drawn from. A missing route must not
    fall back to another route's number — that would be a lie on screen."""
    monkeypatch.setattr(ingest, "DATA_DIR", tmp_path / "parsed")
    _run(transport="scrape", n=33)

    by_route = ingest.runs_by_transport("epic_property_management")

    assert set(by_route) == {"scrape"}
    assert ingest.load_latest_parsed_for("epic_property_management", "email") is None


def test_without_a_route_you_still_get_whatever_ran_last(tmp_path, monkeypatch):
    """The unchanged behaviour the rest of the app relies on."""
    monkeypatch.setattr(ingest, "DATA_DIR", tmp_path / "parsed")
    _run(transport="upload", n=23)
    _run(transport="scrape", n=33)

    assert ingest.load_latest_parsed_for("epic_property_management")["transaction_count"] == 33


def test_runs_by_transport_carries_how_it_was_read(tmp_path, monkeypatch):
    """The reader node's label comes from here: a parser name, or a model's."""
    monkeypatch.setattr(ingest, "DATA_DIR", tmp_path / "parsed")
    txn = Transaction(source_key="k", date=datetime.fromisoformat("2026-06-15"), amount=1.0,
                      description="row")
    ingest._persist("k", [txn], Path("in.pdf"), "llm_extract", None,
                    transport="upload", model="qwen3-coder-30b")

    entry = ingest.runs_by_transport("k")["upload"]

    assert entry["extraction_method"] == "llm_extract"
    assert entry["model"] == "qwen3-coder-30b"
    assert entry["month"] == "2026-06"


def test_runs_written_before_routes_existed_still_place_themselves(tmp_path, monkeypatch):
    """Files already in data/parsed have no `transport` field. Rather than
    rewriting the operator's financial data, infer it: only the portal scrape
    had its own extraction method, everything else arrived as a file."""
    monkeypatch.setattr(ingest, "DATA_DIR", tmp_path / "parsed")
    data_dir = tmp_path / "parsed"
    data_dir.mkdir(parents=True)
    (data_dir / "legacy-2026-06.json").write_text(json.dumps({
        "source_key": "legacy", "month": "2026-06", "transaction_count": 35,
        "extraction_method": "portal_scrape", "transactions": [],
    }))

    assert ingest.runs_by_transport("legacy")["scrape"]["count"] == 35


def test_a_new_run_records_its_route_even_when_not_told(tmp_path, monkeypatch):
    """persist_scraped and friends pass it explicitly, but nothing should be able
    to write a run whose route is None again."""
    monkeypatch.setattr(ingest, "DATA_DIR", tmp_path / "parsed")
    txn = Transaction(source_key="k", date=datetime.fromisoformat("2026-06-15"), amount=1.0,
                      description="row")

    run = ingest._persist("k", [txn], Path("in.pdf"), "portal_scrape", None)

    assert run["transport"] == "scrape"
    assert "-scrape-" in run["run_path"]


def test_unreadable_run_files_are_skipped_not_fatal(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "DATA_DIR", tmp_path / "parsed")
    _run(transport="upload", n=23)
    (tmp_path / "parsed" / "broken.json").write_text("{not json")

    assert ingest.runs_by_transport("epic_property_management")["upload"]["count"] == 23


def test_the_later_RUN_wins_even_when_the_file_is_older_on_disk(tmp_path, monkeypatch):
    """Two scrapes half a minute apart land in different month files, so "which
    ran last" is a fact about the run. mtime is about when a file was touched —
    a restore or a copy would reorder them."""
    monkeypatch.setattr(ingest, "DATA_DIR", tmp_path / "parsed")
    data_dir = tmp_path / "parsed"
    data_dir.mkdir(parents=True)
    for name, when, n in [("k-scrape-2026-07.json", "2026-07-30T17:31:09+00:00", 13),
                          ("k-scrape-2026-06.json", "2026-07-30T17:31:40+00:00", 33)]:
        (data_dir / name).write_text(json.dumps({
            "source_key": "k", "transport": "scrape", "parsed_at": when,
            "transaction_count": n, "transactions": [],
        }))
    # touch the EARLIER run last, so mtime disagrees with reality
    (data_dir / "k-scrape-2026-07.json").touch()

    assert ingest.runs_by_transport("k")["scrape"]["count"] == 33
