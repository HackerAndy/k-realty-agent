"""Pydantic schemas shared by tools, validators, evals, and orchestration.

This module must stay framework-free (no langgraph/langchain imports) so it
can be reused unchanged if the orchestration harness is swapped.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, Field


class Transaction(BaseModel):
    """A single financial transaction extracted from one of K-Realty's
    financial sources (property manager, bank, loan servicers, insurance,
    business services), normalized for Schedule E categorization and
    monthly per-property P&L reporting."""

    entity_id: str
    source_system: str  # e.g. "epic_property_management", "dfcu_bank"
    source_uri: str | None = None
    property_id: str
    unit_id: str | None = None  # duplex: which of the 2 doors, if applicable
    transaction_date: datetime
    amount: float
    description: str
    schedule_e_category: str | None = None  # set once categorized
    metadata: dict[str, str] = Field(default_factory=dict)


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
