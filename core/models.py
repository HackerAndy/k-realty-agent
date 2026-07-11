"""Pydantic schemas shared by tools, validators, evals, and orchestration.

This module must stay framework-free (no langgraph/langchain imports) so it
can be reused unchanged if the orchestration harness is swapped.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, Field


class Transaction(BaseModel):
    """Starter schema for the primary domain object this agent works on.
    Rename/extend fields to match K-Realty's actual data."""

    entity_id: str
    source_uri: str | None = None
    received_at: datetime
    amount: float | None = None
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
