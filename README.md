# Property Finance Tracker Agent

Property finance agent for K-Realty. **Current scope:** parse a Buildium
Owner Statement PDF into clean transactions. Categorization, P&L, and the
other financial sources are planned but deliberately left out of the
starting flow to keep it focused on getting parsing right first.

Client: K-Realty

## Architecture (portability contract)

```
core/            zero framework imports — 100% portable
├── parsers/     one module per source: document → transactions
│                (base.py = Parser contract + ParseError; __init__ = REGISTRY)
├── tools/       credential store, browser session, service manifest, LLM fallback
├── validators/  deterministic business-rule checks
├── prompts/     all prompts as versioned text/YAML files
├── policies/    services.yaml = the source registry (which source, format, parser, status)
├── ingest.py    source-driven: look up a source's parser, run it, persist
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

Menu: ingest a source (pick a source, point at its document → transactions),
view the latest parsed transactions, manage services & credentials, and
status. All data stays local: parsed output in `data/` (gitignored),
credentials encrypted in `.secrets/`.

**Adding a source.** `core/policies/services.yaml` lists all 8 financial
sources from the onboarding form, each with an `input_type`, `access`, a
`parser` name, and a `status`. To handle a new one: get a real sample
document, write a parser in `core/parsers/<source>.py` exposing
`(path) -> list[Transaction]`, register it in `core/parsers/__init__.py`,
and set that source's `parser` + `status: implemented` in `services.yaml`.
The Status screen shows how many sources have parsers (1 of 8 today).

If a source's parser can't read a document's layout and `ANTHROPIC_API_KEY`
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
