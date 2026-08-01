"""What the bench builds, and what counts as having built it correctly.

Two different questions, deliberately kept apart:

- **Did the gate pass?** `orchestration/verify.py` re-ran the agent's own test,
  checked coverage, lint, reconciliation. That is what the operator sees.
- **Is the parser actually right?** The extracted rows are compared against what
  we know each document contains.

They come apart, and the gap between them is the number worth watching. An agent
can write a parser that reads half a statement and a test that agrees with it:
its test passes, coverage is real, nothing is hardcoded, and the gate says yes.
Only `correct` notices. When `gate_ok` runs ahead of `correct`, the gate is
being satisfied rather than the document being read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures"


@dataclass(frozen=True)
class Case:
    """One document the agent is asked to write a parser for.

    `label` is what the operator would have typed in the GUI; the agent sees it
    and the document, and nothing else. Everything below `label` is the answer
    key, which the agent never sees.
    """

    key: str
    label: str
    document: str
    difficulty: str
    note: str
    count: int
    total: float
    positives: int
    negatives: int
    required_columns: tuple[str, ...]
    control_total: str = ""

    @property
    def path(self) -> Path:
        return FIXTURES / self.document


CASES: tuple[Case, ...] = (
    Case(
        key="bench_riverbend_credit_union",
        label="Riverbend Credit Union checking export",
        document="bench_riverbend_credit_union.csv",
        difficulty="easy",
        note="Preamble above the header, parenthesised negatives, a Totals row "
             "that is not a transaction.",
        count=8,
        total=1420.45,
        positives=3,
        negatives=5,
        required_columns=("Account", "Posted", "Description", "Type", "Amount",
                          "Running Balance"),
        control_total="Running Balance ends at 3,420.45; the Totals row states 1,420.45.",
    ),
    Case(
        key="bench_summit_card_services",
        label="Summit Card Services statement export",
        document="bench_summit_card_services.csv",
        difficulty="medium",
        note="The file's sign convention is inverted against ours: a charge is "
             "POSITIVE in the source and must come out negative. An agent that "
             "passes the column through writes a test that agrees with it.",
        count=7,
        total=-367.92,
        positives=2,
        negatives=5,
        required_columns=("Transaction Date", "Post Date", "Description",
                          "Category", "Amount"),
        control_total="New Balance 367.92.",
    ),
    Case(
        key="bench_harbor_property_group",
        label="Harbor Property Group owner activity statement",
        document="bench_harbor_property_group.pdf",
        difficulty="hard",
        note="PDF. Per-property sections with split Charges/Credits columns, so "
             "the sign comes from which column a number sits in — which plain "
             "text extraction cannot tell you. Subtotals and a net line to skip.",
        count=6,
        total=3547.75,
        positives=3,
        negatives=3,
        required_columns=("Date", "Unit", "Category", "Payee / Payer"),
        control_total="Per-property subtotals, and Net owner activity 3,547.75.",
    ),
)

BY_KEY = {case.key: case for case in CASES}


@dataclass
class Score:
    """How one run of one case did. `misses` says which check failed and how."""

    case_key: str
    built: bool = False
    gate_ok: bool = False
    correct: bool = False
    misses: list[str] = field(default_factory=list)


def score(case: Case, verification: dict) -> Score:
    """Compare a verification dict against the case's answer key.

    `verification` is what `build_parser_for_source` returns under
    "verification": {ok, test, transactions|error}.
    """
    result = Score(case_key=case.key)
    transactions = verification.get("transactions")
    result.built = isinstance(transactions, list)
    result.gate_ok = bool(verification.get("ok"))

    if not result.built:
        result.misses.append(
            "no transactions came back — " +
            (verification.get("error") or "the parser did not run").strip()[:300])
        return result

    if len(transactions) != case.count:
        result.misses.append(
            f"extracted {len(transactions)} transactions, expected {case.count}")

    amounts = [float(t.get("amount", 0.0)) for t in transactions]
    total = round(sum(amounts), 2)
    if total != case.total:
        result.misses.append(f"amounts total {total:+.2f}, expected {case.total:+.2f}")

    positives = sum(1 for a in amounts if a > 0)
    negatives = sum(1 for a in amounts if a < 0)
    if (positives, negatives) != (case.positives, case.negatives):
        result.misses.append(
            f"signs are {positives} in / {negatives} out, expected "
            f"{case.positives} in / {case.negatives} out")

    # The source's real columns, verbatim, on every row — the faithful-data rule.
    # Checked on all rows because a parser can get the header right and then drop
    # a column on the rows that wrap.
    seen = set()
    for transaction in transactions:
        seen |= set((transaction.get("fields") or {}).keys())
    missing = [column for column in case.required_columns if column not in seen]
    if missing:
        result.misses.append("fields are missing the source's columns: " + ", ".join(missing))

    result.correct = not result.misses
    return result
