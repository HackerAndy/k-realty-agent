"""HITL pause/resume at dollar-threshold gates (Phase 2 Slack approvals)."""

from __future__ import annotations

from langgraph.types import interrupt

from core.models import Decision


def request_approval(decision: Decision) -> Decision:
    """Pause graph execution until a human approves/rejects via interfaces/."""
    return interrupt({"decision": decision.model_dump()})
