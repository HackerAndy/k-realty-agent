"""The bench's answer key has to be right, or every number it reports is wrong.

Two things are checked here, both without calling a model:

1. The expectations in `cases.py` match the documents in `tests/fixtures/`.
   Computed independently from the raw files, so regenerating a fixture without
   updating the answer key fails here rather than silently moving the baseline.
2. `score()` separates "the gate approved it" from "it read the document", which
   is the whole reason the bench exists.
"""

from __future__ import annotations

import csv
from decimal import Decimal
import subprocess

import pytest

from orchestration.bench import __main__ as bench_main
from orchestration.bench.cases import BY_KEY, CASES, score

REPO_ROOT = bench_main.REPO_ROOT


def _money(text: str) -> Decimal:
    """A cell as a signed Decimal. Parentheses are the accounting minus sign."""
    text = text.strip().replace(",", "").replace("$", "")
    if text.startswith("(") and text.endswith(")"):
        return -Decimal(text[1:-1])
    return Decimal(text)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.key)
def test_document_exists(case):
    assert case.path.is_file(), f"missing bench document {case.path}"


def test_riverbend_answer_key_matches_the_file():
    case = BY_KEY["bench_riverbend_credit_union"]
    lines = case.path.read_text(encoding="utf-8").splitlines()
    header = next(i for i, line in enumerate(lines) if line.startswith("Account,Posted"))
    rows = [r for r in csv.DictReader(lines[header:]) if r["Posted"]]

    amounts = [_money(r["Amount"]) for r in rows]
    assert len(rows) == case.count
    assert sum(amounts) == Decimal(str(case.total))
    assert sum(1 for a in amounts if a > 0) == case.positives
    assert sum(1 for a in amounts if a < 0) == case.negatives
    assert set(case.required_columns) <= set(csv.DictReader(lines[header:]).fieldnames)

    # The Totals row is present and is NOT one of the transactions — the parser
    # has something to wrongly include, which is the point of the case.
    assert lines[-1].startswith(",,Totals,")


def test_summit_answer_key_inverts_the_source_sign():
    case = BY_KEY["bench_summit_card_services"]
    rows = [r for r in csv.DictReader(case.path.read_text(encoding="utf-8").splitlines())
            if r["Transaction Date"]]

    # The file states a charge as POSITIVE; our convention is money out negative.
    amounts = [-_money(r["Amount"]) for r in rows]
    assert len(rows) == case.count
    assert sum(amounts) == Decimal(str(case.total))
    assert sum(1 for a in amounts if a > 0) == case.positives
    assert sum(1 for a in amounts if a < 0) == case.negatives

    # Passing the column straight through would give the opposite total, so a
    # parser that ignores the convention cannot accidentally score correct.
    assert sum(_money(r["Amount"]) for r in rows) == -Decimal(str(case.total))


def test_harbor_answer_key_matches_the_pdf():
    pdfplumber = pytest.importorskip("pdfplumber")
    case = BY_KEY["bench_harbor_property_group"]
    with pdfplumber.open(case.path) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    assert "Net owner activity 3,547.75" in text
    assert Decimal("3547.75") == Decimal(str(case.total))
    # Six dated rows, two subtotals, one net line.
    assert sum(1 for line in text.splitlines() if line.startswith("05/")) == case.count
    assert text.count("Subtotal - ") == 2


def _transaction(amount: float, **fields) -> dict:
    return {"amount": amount, "fields": fields or {"Account": "8841"}}


def _perfect(case) -> list[dict]:
    """Rows that satisfy the case exactly: right count, total, signs, columns.

    Amounts are arbitrary — only their count, their signs and their sum are
    scored — so every row starts at a flat ±1000 and the first one absorbs
    whatever is left over to land on the case's total.
    """
    rows = [_transaction(0.0, **{c: "x" for c in case.required_columns})
            for _ in range(case.count)]
    for i in range(case.count):
        rows[i]["amount"] = 1000.0 if i < case.positives else -1000.0
    drift = round(case.total - sum(r["amount"] for r in rows), 2)
    rows[0]["amount"] = round(rows[0]["amount"] + drift, 2)
    return rows


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.key)
def test_score_accepts_a_correct_parser(case):
    outcome = score(case, {"ok": True, "transactions": _perfect(case)})
    assert outcome.built and outcome.gate_ok
    assert outcome.correct, outcome.misses


def test_score_catches_a_gate_pass_that_read_the_document_wrong():
    """The failure the harness's own gate cannot see, and the bench exists for."""
    case = BY_KEY["bench_riverbend_credit_union"]
    rows = _perfect(case)[:4]  # half the statement, and its own test would agree

    outcome = score(case, {"ok": True, "transactions": rows})

    assert outcome.gate_ok is True
    assert outcome.correct is False
    assert any("expected 8" in miss for miss in outcome.misses)


def test_score_catches_inverted_signs():
    case = BY_KEY["bench_summit_card_services"]
    rows = [dict(r, amount=-r["amount"]) for r in _perfect(case)]

    outcome = score(case, {"ok": True, "transactions": rows})

    assert outcome.correct is False
    assert any("signs are" in miss for miss in outcome.misses)


def test_score_catches_dropped_columns():
    case = BY_KEY["bench_riverbend_credit_union"]
    rows = [{"amount": r["amount"], "fields": {"Posted": "x"}} for r in _perfect(case)]

    outcome = score(case, {"ok": True, "transactions": rows})

    assert outcome.correct is False
    assert any("missing the source's columns" in miss for miss in outcome.misses)


def test_score_reports_a_parser_that_never_ran():
    case = BY_KEY["bench_harbor_property_group"]

    outcome = score(case, {"ok": False, "error": "ModuleNotFoundError: core.parsers.x"})

    assert outcome.built is False
    assert outcome.gate_ok is False
    assert outcome.correct is False
    assert "ModuleNotFoundError" in outcome.misses[0]


# --- the answer key must not be inside the checkout the agent explores --------
#
# It was. On the hard case an agent that explores properly read
# `orchestration/bench/cases.py` and `tests/test_bench.py` while looking for the
# project's conventions, then reported a total matching the key exactly. Nothing
# errored; the run simply stopped being a measurement of reading the document.
# `_scrub_answer_key` removes these files from the worktree — which is only worth
# anything if the list stays true, hence both tests below.

def test_every_scrubbed_path_still_exists():
    """A rename would turn the scrub into a silent no-op, and the next bench run
    would look exactly like a good one."""
    missing = [p for p in bench_main.ANSWER_KEY if not (REPO_ROOT / p).is_file()]

    assert not missing, f"ANSWER_KEY names files that are gone: {missing}"


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.key)
def test_a_cases_total_appears_only_where_the_bench_scrubs_it(case):
    """The real guard: grep every tracked file for the expected total. Anything
    holding it that the bench does not remove is a leak into the next run.

    The documents themselves are allowed — a statement stating its own control
    total is the thing the parser is supposed to reconcile against, and is the
    whole point of the medium and hard cases.
    """
    needle = f"{abs(case.total):,.2f}"
    plain = f"{abs(case.total):.2f}"
    tracked = subprocess.run(["git", "ls-files"], cwd=REPO_ROOT,
                             capture_output=True, text=True, check=True).stdout.split()

    holders = []
    for relative in tracked:
        path = REPO_ROOT / relative
        if not path.is_file() or relative.startswith("tests/fixtures/"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if needle in text or plain in text:
            holders.append(relative)

    leaked = [h for h in holders if h not in bench_main.ANSWER_KEY]
    assert not leaked, (
        f"{case.key}'s expected total {case.total} appears in {leaked}, which the "
        f"bench leaves in the worktree. Add it to ANSWER_KEY or take the number out.")
