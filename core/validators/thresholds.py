# Template candidate: generic (tier 1) — threshold values come from client
# policy; the check itself is client-agnostic.
# See agent-harness-template/docs/promotion-log.md.
"""Deterministic dollar-threshold check.

Per the intake: any single property expense exceeding $250 per unit in a
given month must be flagged for Andy's attention (eventually via Telegram;
for now it surfaces in the review queue and the P&L report).

This module must stay framework-free (no langgraph/langchain imports).
"""

from __future__ import annotations

from core.models import Transaction

# From clients/k-realty intake: approval_thresholds -> "High single-unit expense"
EXPENSE_FLAG_THRESHOLD_PER_UNIT = 250.00


def exceeds_expense_threshold(transaction: Transaction) -> bool:
    """True if this single expense exceeds the per-unit flag threshold.

    Expenses are negative amounts in our convention (money out); income is
    positive. Only expenses are subject to the threshold.
    """
    return transaction.amount < 0 and abs(transaction.amount) > EXPENSE_FLAG_THRESHOLD_PER_UNIT
