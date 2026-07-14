# Template candidate: generic (tier 1) — the parser contract + registry
# pattern is client-agnostic; individual parsers are per-source (tier 2/3).
# See agent-harness-template/docs/promotion-log.md.
"""Shared contract for source parsers.

Every source declared in core/policies/services.yaml with a `parser` value
maps (via core/parsers/REGISTRY) to a callable that turns that source's
document into a list of Transaction records. Keeping one small contract
here lets core/ingest.py stay source-agnostic: it looks up the parser for
a source and runs it, without knowing anything about that source's format.

This module must stay framework-free (no langgraph/langchain imports).
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from core.models import Transaction


class ParseError(RuntimeError):
    """A parser could not parse its input.

    Carries the raw extracted text (when the parser got that far) so the
    caller can dump it for inspection or hand it to the LLM fallback.
    """

    def __init__(self, message: str, extracted_text: str = ""):
        super().__init__(message)
        self.extracted_text = extracted_text


class Parser(Protocol):
    """A source parser: given a path to the source document, return its
    transactions (or raise ParseError)."""

    def __call__(self, path: Path) -> list[Transaction]: ...
