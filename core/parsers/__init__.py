# Template candidate: generic (tier 1) — the registry pattern is
# client-agnostic; the entries it maps to are per-source parsers.
# See agent-harness-template/docs/promotion-log.md.
"""Parser registry: maps a source's `parser` name (declared in
core/policies/services.yaml) to the callable that turns that source's
document into transactions.

To add a source parser: drop a module in this package exposing a
`(path) -> list[Transaction]` function, then register it below. That's the
whole seam between "the onboarding form said we need to handle source X"
and "here is the code that handles source X."
"""

from __future__ import annotations

from core.parsers.base import ParseError, Parser
from core.parsers.buildium_owner_statement import parse_statement as _buildium_parse
from core.parsers.dfcu_financial_bank import parse as _dfcu_financial_bank_parse

REGISTRY: dict[str, Parser] = {
    "buildium_owner_statement": _buildium_parse,
    "dfcu_financial_bank": _dfcu_financial_bank_parse,
}

__all__ = ["ParseError", "Parser", "REGISTRY", "get_parser"]


def get_parser(name: str) -> Parser:
    if name not in REGISTRY:
        raise KeyError(
            f"No parser registered under '{name}'. Registered: {sorted(REGISTRY)}. "
            "Add one in core/parsers/ and register it in core/parsers/__init__.py."
        )
    return REGISTRY[name]
