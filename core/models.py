"""Pydantic schemas shared by tools, validators, evals, and orchestration.

This module must stay framework-free (no langgraph/langchain imports) so it
can be reused unchanged if the orchestration harness is swapped.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, Field


class Transaction(BaseModel):
    """A single financial transaction, kept FAITHFUL to its source.

    `fields` preserves the source's ACTUAL columns, verbatim — a bank export's
    columns for a bank, a statement's columns for a statement. Nothing is
    invented: a source that has no "property" or "unit" simply doesn't have
    those keys.

    The only forced-universal fields are the three that genuinely exist for
    every financial transaction — `date`, a signed `amount` (positive = money
    in, negative = money out), and a `description`. These are drawn from the
    source's own columns for display, sorting, and reconciliation; they are
    never fabricated. Everything else lives in `fields`.
    """

    source_key: str            # which source produced this row (e.g. "dfcu_financial_bank")
    date: datetime
    amount: float
    description: str
    fields: dict[str, str] = Field(default_factory=dict)  # the source's real columns, verbatim
    source_uri: str | None = None


class DecisionStatus(str, Enum):
    AUTO_APPROVED = "auto_approved"
    NEEDS_REVIEW = "needs_review"
    REJECTED = "rejected"


class Decision(BaseModel):
    entity_id: str
    status: DecisionStatus
    recommendation: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    validator_failures: list[str] = Field(default_factory=list)


class AuditRecord(BaseModel):
    entity_id: str
    decision: Decision
    actor: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    notes: str | None = None
