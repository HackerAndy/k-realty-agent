"""Postgres-backed checkpointer for graph state persistence (Phase 2)."""

from __future__ import annotations

from langgraph.checkpoint.postgres import PostgresSaver


def build_checkpointer(conn_string: str) -> PostgresSaver:
    return PostgresSaver.from_conn_string(conn_string)
