# K-Realty Agent — project directives

## Foundational directive: the harness builds its own domain code

**k-realty-agent IS an LLM agent/harness.** It must create, debug, and maintain
its own domain-specific processing — parsers, scrapers, the navigation clicks,
URLs, and rows/columns needed to handle K-Realty's sources — through its OWN
embedded agent (`orchestration/agent.py`), working *with* the operator.

- The **template** (`../agent-harness-template`) ships only **generic** capability.
- The **domain** work (which dropdown, which button, which columns) is the
  harness's job, produced by its agent + a user demonstration — **not** hand-written
  by a developer into this repo. A human authoring domain selectors/columns here is
  the anti-pattern.
- Pattern to follow, for every source: **human demonstrates/provides a sample →
  embedded agent writes + self-verifies the code → human approves in the TUI.**
  - Documents: `build_parser` (sample document → parser).
  - Portals: `build_scraper` (demonstrated navigation → scraper; capture the data
    request to prefer a direct API call, fall back to replaying recorded clicks).

After onboarding, the operator works with the harness (its TUI), not a code editor.

## Other standing rules

- **User transparency / nothing hidden.** Never mask a field or behavior unless
  it's a genuine secret. Everything happens via the TUI, visibly.
- **Faithful data.** Preserve each source's ACTUAL columns verbatim in
  `Transaction.fields`; invent nothing. Only `date`/`amount`/`description` are
  normalized.
- **Portability.** `core/` and `evals/` never import langgraph/langchain (CI lint:
  `scripts/check_portability.py`). The anthropic SDK is allowed.
- **Logging standard.** Every deterministic failure → one structured record via
  `core/observability.py` (`log.failure(...)`); see that module.
- **Financial data never committed.** `data/`, `.secrets/`, `.browser_profiles/`,
  `*.pdf`/`*.csv` are gitignored (with test-fixture exceptions).
