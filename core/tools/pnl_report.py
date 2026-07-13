# Template candidate: platform/domain-reusable (tier 2) — monthly P&L by
# property/category is reusable for any rental-property client; the category
# labels come from client policy. See agent-harness-template/docs/promotion-log.md.
"""Render a monthly P&L report per property from categorized transactions.

Plain-text output, designed to be readable straight from the TUI (and easy
to redirect to a file). Income and expenses are grouped by Schedule E
category; uncategorized (flagged) items are shown separately and excluded
from category totals so the report never silently absorbs a guess.

This module must stay framework-free (no langgraph/langchain imports).
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import yaml

from core.models import Decision, DecisionStatus, Transaction

DEFAULT_CATEGORIES_PATH = Path("core/policies/schedule_e_categories.yaml")


def _category_labels(categories_path: Path) -> dict[str, str]:
    data = yaml.safe_load(categories_path.read_text())
    return {
        entry["key"]: entry["label"]
        for section in ("income_categories", "expense_categories")
        for entry in data.get(section, [])
    }


def render_pnl(
    pairs: list[tuple[Transaction, Decision]],
    month_label: str,
    categories_path: Path = DEFAULT_CATEGORIES_PATH,
) -> str:
    labels = _category_labels(categories_path)
    by_property: dict[str, list[tuple[Transaction, Decision]]] = defaultdict(list)
    for transaction, decision in pairs:
        by_property[transaction.property_id].append((transaction, decision))

    lines: list[str] = [f"P&L — {month_label}", "=" * 46]
    grand_income = grand_expense = 0.0
    flagged_count = 0

    for property_id in sorted(by_property):
        income: dict[str, float] = defaultdict(float)
        expenses: dict[str, float] = defaultdict(float)
        flagged: list[tuple[Transaction, Decision]] = []

        for transaction, decision in by_property[property_id]:
            if decision.status == DecisionStatus.AUTO_APPROVED and decision.recommendation:
                if transaction.amount >= 0:
                    income[decision.recommendation] += transaction.amount
                else:
                    expenses[decision.recommendation] += transaction.amount
            else:
                flagged.append((transaction, decision))

        total_income = sum(income.values())
        total_expense = sum(expenses.values())
        grand_income += total_income
        grand_expense += total_expense
        flagged_count += len(flagged)

        lines += ["", f"Property: {property_id}", "-" * 46]
        if income:
            lines.append("  Income")
            for key, amount in sorted(income.items()):
                lines.append(f"    {labels.get(key, key):34} {amount:>10,.2f}")
        if expenses:
            lines.append("  Expenses")
            for key, amount in sorted(expenses.items()):
                lines.append(f"    {labels.get(key, key):34} {amount:>10,.2f}")
        lines.append(f"  {'Net (categorized only)':36} {total_income + total_expense:>10,.2f}")

        if flagged:
            lines.append(f"  Needs review ({len(flagged)} item(s), excluded from totals above):")
            for transaction, decision in flagged:
                suggestion = f" (suggested: {decision.recommendation})" if decision.recommendation else ""
                lines.append(
                    f"    {transaction.transaction_date:%m/%d} {transaction.amount:>10,.2f}  "
                    f"{transaction.description}{suggestion}"
                )

    lines += [
        "",
        "=" * 46,
        f"{'All properties — income':38} {grand_income:>10,.2f}",
        f"{'All properties — expenses':38} {grand_expense:>10,.2f}",
        f"{'All properties — net (categorized)':38} {grand_income + grand_expense:>10,.2f}",
    ]
    if flagged_count:
        lines.append(f"{flagged_count} transaction(s) still need review — net will change once resolved.")
    return "\n".join(lines)
