# Template candidate: generic (tier 1) — rules engine is client-agnostic;
# the rules/categories YAML it loads are client policy (tier 3).
# See agent-harness-template/docs/promotion-log.md.
"""Deterministic, rules-based transaction categorizer.

Matches a transaction's description against case-insensitive substring rules
(core/policies/categorization_rules.yaml) that map to categories defined in
core/policies/schedule_e_categories.yaml. By design it never guesses:

- matched rule, no review flag  -> AUTO_APPROVED, confidence 1.0
- matched rule with review flag -> NEEDS_REVIEW (category suggested)
- no matching rule              -> NEEDS_REVIEW (no category)

This meets the intake's accuracy bar directly: 100% for known vendors,
everything unknown flagged for a human. An LLM assist can be layered on
later for the unknowns, but is deliberately not needed for this engine.

This module must stay framework-free (no langgraph/langchain imports).
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel

from core.models import Decision, DecisionStatus, Transaction

DEFAULT_RULES_PATH = Path("core/policies/categorization_rules.yaml")
DEFAULT_CATEGORIES_PATH = Path("core/policies/schedule_e_categories.yaml")


class Rule(BaseModel):
    match: list[str]
    category: str
    review: bool = False
    note: str | None = None


class CategorizerError(RuntimeError):
    pass


class Categorizer:
    def __init__(
        self,
        rules_path: Path = DEFAULT_RULES_PATH,
        categories_path: Path = DEFAULT_CATEGORIES_PATH,
    ):
        categories_data = yaml.safe_load(categories_path.read_text())
        self.valid_categories = {
            entry["key"]
            for section in ("income_categories", "expense_categories")
            for entry in categories_data.get(section, [])
        }

        rules_data = yaml.safe_load(rules_path.read_text())
        self.rules = [Rule.model_validate(entry) for entry in rules_data.get("rules", [])]

        unknown = {r.category for r in self.rules} - self.valid_categories
        if unknown:
            raise CategorizerError(
                f"Rules reference categories not defined in {categories_path}: {sorted(unknown)}"
            )

    def categorize(self, transaction: Transaction) -> Decision:
        description = transaction.description.lower()
        for rule in self.rules:
            if any(pattern.lower() in description for pattern in rule.match):
                if rule.review:
                    return Decision(
                        entity_id=transaction.entity_id,
                        status=DecisionStatus.NEEDS_REVIEW,
                        recommendation=rule.category,
                        confidence=1.0,
                        reasoning=(
                            f"Matched rule {rule.match} -> '{rule.category}', but the rule "
                            f"requires human review. {rule.note or ''}".strip()
                        ),
                    )
                return Decision(
                    entity_id=transaction.entity_id,
                    status=DecisionStatus.AUTO_APPROVED,
                    recommendation=rule.category,
                    confidence=1.0,
                    reasoning=f"Matched rule {rule.match} -> '{rule.category}'.",
                )
        return Decision(
            entity_id=transaction.entity_id,
            status=DecisionStatus.NEEDS_REVIEW,
            recommendation=None,
            confidence=0.0,
            reasoning="No categorization rule matched — flagged rather than guessed.",
        )
