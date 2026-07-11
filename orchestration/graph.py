"""LangGraph wiring: nodes, edges, state schema.

This is the ONLY module (with checkpointer.py / interrupts.py) allowed to
import langgraph. Nodes must be thin — call into core.tools / core.validators
for all business logic. No prompts or decision logic inline here.
"""

from __future__ import annotations

from langgraph.graph import StateGraph

from core.models import Decision, Transaction


class AgentState(Transaction):
    """Graph state schema. Extends the portable Transaction model
    with whatever transient fields the graph needs to pass between nodes."""

    decision: Decision | None = None


def build_graph() -> StateGraph:
    graph = StateGraph(AgentState)
    # TODO: add nodes that call core.tools / core.validators for Property Finance Tracker Agent,
    # e.g. graph.add_node("classify", classify_node)
    return graph
