# Property Finance Tracker Agent

Property finance agent for K-Realty. **Current scope:** parse a Buildium
Owner Statement PDF into clean transactions. Categorization, P&L, and the
other financial sources are planned but deliberately left out of the
starting flow to keep it focused on getting parsing right first.

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

## Running the agent

Everything is controlled from one TUI:

```bash
poetry run agent
```

Menu: parse a statement (Owner Statement PDF → transactions), view the
latest parsed transactions, manage services & credentials, and status. All
data stays local: parsed output in `data/` (gitignored), credentials
encrypted in `.secrets/`.

If the built-in parser can't read a statement's layout and `ANTHROPIC_API_KEY`
is set, the TUI offers an AI extraction fallback — the one step where data
leaves the machine, gated behind an explicit consent prompt.

Credential setup (also reachable from the TUI menu) is for the future portal
scrapers; parsing a statement PDF needs no credentials:

```bash
poetry run python scripts/manage_secrets.py generate-key   # once; save the key
poetry run python scripts/manage_secrets.py setup          # guided, all 9 services
```

## Portability check

```bash
poetry run python scripts/check_portability.py
```
