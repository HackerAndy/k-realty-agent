# Property Finance Tracker Agent

Browser automation agent that extracts transactions from 8 financial sources (property manager, bank, loans, insurance, services), categorizes them against a Schedule E-aligned tax category list, and delivers a monthly P&L report per property — replacing a manual process that currently goes undone.

Client: K-Realty

## Architecture (portability contract)

```
core/            zero framework imports — 100% portable
├── tools/       external API, email/Slack, doc parsing (plain Python)
├── validators/  deterministic business-rule checks
├── prompts/     all prompts as versioned text/YAML files
├── policies/    escalation rules, approval thresholds (config)
└── models.py    Pydantic schemas: Transaction, Decision, AuditRecord

evals/           standalone harness — runs against core/, not LangGraph
├── golden_set/  historical cases + expected outputs
└── runner.py    accuracy, latency, cost per task

orchestration/   ONLY directory that imports LangGraph (~10-15% of code)
├── graph.py         nodes, edges, state schema
├── checkpointer.py  Postgres persistence
└── interrupts.py    HITL pause/resume at $ gates

interfaces/      Slack approvals, email triggers — calls orchestration API
```

### Rules (enforced by CI)

1. `core/` and `evals/` never import `langgraph` or `langchain`.
2. Tools take/return Pydantic models, not framework state objects.
3. Prompts are files, never inline strings in graph code.
4. Orchestration layer is glue only — no business logic in nodes.

Rule 1 is enforced mechanically by [scripts/check_portability.py](scripts/check_portability.py),
run in CI on every push/PR (see [.github/workflows/ci.yml](.github/workflows/ci.yml)).

## Setup

```bash
poetry install
```

## Portability check

```bash
poetry run python scripts/check_portability.py
```
