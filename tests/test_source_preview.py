"""What the harness reports back before a new source is named.

The add-a-source flow rests on one claim: the agent looks at the source first,
and the operator names it from what it saw. That only works if "what it saw" is
honest — the source's OWN column headings, a row count that doesn't pretend to be
a parse, and a plain admission when it couldn't make anything out.

The other property here is that a preview is a convenience, never a gate. No
model, an unreachable model, or a model that replies with prose must all leave
the operator able to carry on naming and saving their source.
"""

import json
from datetime import datetime

import pytest

from core import preview
from core.models import Transaction
from interfaces import mcp_tools
from orchestration import naming


def _txn(day, **fields):
    return Transaction(source_key="x", date=datetime(2026, 7, day), amount=-1.0,
                       description="d", fields=fields)


# ── Summarising what was read ────────────────────────────────────────────────

def test_columns_are_the_sources_own_headings_in_order():
    """Faithful data: report what the source calls things, not what we do."""
    out = preview.summarise_transactions([
        _txn(1, Date="7/1", Payee="Acme", Amount="-10"),
        _txn(2, Date="7/2", Payee="Acme", Memo="late"),
    ])
    assert out["columns"] == ["Date", "Payee", "Amount", "Memo"]
    assert out["rows"] == 2


def test_the_span_reads_as_a_date_range():
    out = preview.summarise_transactions([_txn(1), _txn(15)])
    assert out["span"] == "1 Jul – 15 Jul 2026"


def test_a_single_day_is_not_reported_as_a_range():
    assert preview.summarise_transactions([_txn(4)])["span"] == "4 Jul 2026"


def test_nothing_read_summarises_to_nothing_rather_than_erroring():
    assert preview.summarise_transactions([]) == {"rows": 0, "span": "", "columns": []}


# ── Summarising a demonstration ──────────────────────────────────────────────

DEMO = {
    "final_url": "https://portal.example.com/reports?run=1",
    "title": "Activity",
    "recorded_actions": [{"kind": "click"}, {"kind": "change"}],
    "candidate_requests": [
        {"method": "POST", "url": "https://portal.example.com/api/transactions?x=1"},
        {"method": "GET", "url": "https://portal.example.com/api/lookups"},
    ],
    "final_page": {"title": "Activity", "tables": [
        {"th_headers": [], "header_row": ["A"], "row_count": 2},
        {"th_headers": ["Date", "Description", "Amount"], "row_count": 19},
    ]},
}


def test_the_biggest_table_on_the_page_is_the_one_they_came_for():
    out = preview.summarise_demo(DEMO)
    assert out["columns"] == ["Date", "Description", "Amount"]


def test_the_header_row_is_not_counted_as_a_transaction():
    assert preview.summarise_demo(DEMO)["rows"] == 18


def test_it_reports_the_request_it_would_call_next_time():
    out = preview.summarise_demo(DEMO)
    assert "POST" in out["how"] and "portal.example.com/api/transactions" in out["how"]
    assert "?" not in out["how"], "the query string is noise at this size"


def test_a_captured_request_beats_a_rendered_table():
    """A data endpoint is worth more than scraped HTML, so it's what gets
    reported even when the final page had no table on it."""
    out = preview.summarise_demo({**DEMO, "final_page": {"tables": []}})
    assert out["columns"] == [] and out["rows"] == 0
    assert "Found the request" in out["how"]


def test_a_demo_that_captured_nothing_says_so_instead_of_inventing_it():
    out = preview.summarise_demo({**DEMO, "final_page": {"tables": []}, "candidate_requests": []})
    assert "no data table" in out["how"]


def test_a_demo_with_clicks_but_no_endpoint_falls_back_to_replaying_them():
    out = preview.summarise_demo({**DEMO, "candidate_requests": []})
    assert "replay the clicks" in out["how"]


def test_an_empty_demonstration_does_not_blow_up():
    out = preview.summarise_demo({})
    assert out["rows"] == 0 and out["where"] == ""


# ── The one quick look at a document ─────────────────────────────────────────

def test_a_json_reply_becomes_a_description(monkeypatch):
    monkeypatch.setattr(naming, "_complete", lambda s, u: json.dumps(
        {"name": "Chase Business Checking", "columns": ["Date", "Description", "Amount"],
         "rows": 18, "earliest": "16 Jun", "latest": "15 Jul"}))

    out = naming.describe_document("…", "export.pdf")
    assert out["label"] == "Chase Business Checking" and out["label_source"] == "model"
    assert out["columns"] == ["Date", "Description", "Amount"]
    assert out["rows"] == 18 and out["span"] == "16 Jun – 15 Jul"


def test_a_model_that_chats_before_the_json_is_still_understood(monkeypatch):
    """Local models rarely answer with bare JSON, and re-prompting costs a round
    trip for something this cheap."""
    monkeypatch.setattr(naming, "_complete", lambda s, u:
                        'Sure! Here you go:\n```json\n{"name": "DFCU", "columns": ["Date"]}\n```')

    out = naming.describe_document("…", "x.pdf")
    assert out["label"] == "DFCU" and out["columns"] == ["Date"]


def test_a_model_that_answers_with_prose_falls_back_to_the_file_name(monkeypatch):
    monkeypatch.setattr(naming, "_complete", lambda s, u: "I think this is a bank statement.")

    out = naming.describe_document("…", "2026-07_dfcu-checking.pdf")
    assert out["label_source"] == "filename" and "Dfcu" in out["label"]
    assert out["columns"] == []


def test_an_unreachable_model_is_not_an_error(monkeypatch):
    def boom(s, u):
        raise RuntimeError("507 memory ceiling")

    monkeypatch.setattr(naming, "_complete", boom)
    assert naming.describe_document("…", "statement.pdf")["label"] == "Statement"


def test_a_junk_row_count_is_dropped_not_propagated(monkeypatch):
    monkeypatch.setattr(naming, "_complete", lambda s, u:
                        '{"name": "X", "columns": [], "rows": "lots"}')
    assert naming.describe_document("…", "x.pdf")["rows"] == 0


def test_invented_column_names_are_capped(monkeypatch):
    monkeypatch.setattr(naming, "_complete", lambda s, u:
                        json.dumps({"name": "X", "columns": [f"c{i}" for i in range(40)]}))
    assert len(naming.describe_document("…", "x.pdf")["columns"]) == 12


# ── The tools the wizard actually calls ──────────────────────────────────────

@pytest.fixture
def doc(tmp_path):
    path = tmp_path / "july.pdf"
    path.write_bytes(b"%PDF-1.4")
    return path


def test_a_document_preview_says_its_row_count_is_an_estimate(doc, monkeypatch):
    """It is one quick look, not a parse. Calling it a parse would be a lie the
    operator can't check."""
    monkeypatch.setattr(mcp_tools.llm_provider, "is_configured", lambda: True)
    monkeypatch.setattr("core.tools.llm_extractor.read_document_text", lambda p: "text")
    monkeypatch.setattr("orchestration.naming.describe_document", lambda t, f: {
        "label": "Shellpoint Mortgage", "label_source": "model",
        "columns": ["Date", "Amount"], "rows": 18, "span": "16 Jun – 15 Jul"})

    out = mcp_tools.preview_document(str(doc), "july.pdf")

    assert out["rows_estimated"] is True
    assert out["columns"] == ["Date", "Amount"]
    assert out["suggested_name"] == "Shellpoint Mortgage"
    assert out["unattended"] is False, "a document means picking a file every month"
    assert out["trouble"] is None


def test_a_document_preview_with_no_model_still_suggests_a_name(doc, monkeypatch):
    monkeypatch.setattr(mcp_tools.llm_provider, "is_configured", lambda: False)

    out = mcp_tools.preview_document(str(doc), "2026-07_shellpoint.pdf")

    assert out["name_source"] == "filename" and out["suggested_name"]
    assert "No LLM provider" in out["trouble"]


def test_a_document_it_could_not_make_out_says_so_plainly(doc, monkeypatch):
    monkeypatch.setattr(mcp_tools.llm_provider, "is_configured", lambda: True)
    monkeypatch.setattr("core.tools.llm_extractor.read_document_text", lambda p: "text")
    monkeypatch.setattr("orchestration.naming.describe_document", lambda t, f: {
        "label": "July", "label_source": "filename", "columns": [], "rows": 0, "span": ""})

    assert "Couldn't make out" in mcp_tools.preview_document(str(doc), "july.pdf")["trouble"]


def test_a_missing_document_is_refused(tmp_path):
    with pytest.raises(mcp_tools.ToolError, match="No file at"):
        mcp_tools.preview_document(str(tmp_path / "gone.pdf"))


def test_a_demo_preview_needs_no_model_at_all(tmp_path, monkeypatch):
    """The operator left their data on screen — the page IS the answer."""
    path = tmp_path / "demo.json"
    path.write_text(json.dumps(DEMO))
    monkeypatch.setattr("orchestration.naming.suggest_label",
                        lambda text, filename="": {"label": "Activity", "source": "filename"})

    out = mcp_tools.preview_demo(str(path))

    assert out["columns"] == ["Date", "Description", "Amount"] and out["rows"] == 18
    assert out["unattended"] is True
    assert out["where"] == "https://portal.example.com/reports?run=1"


def test_a_missing_demonstration_is_refused(tmp_path):
    with pytest.raises(mcp_tools.ToolError, match="No demonstration at"):
        mcp_tools.preview_demo(str(tmp_path / "gone.json"))


def test_an_unreadable_demonstration_says_which_file(tmp_path):
    path = tmp_path / "demo.json"
    path.write_text("{not json")
    with pytest.raises(mcp_tools.ToolError, match="unreadable"):
        mcp_tools.preview_demo(str(path))
