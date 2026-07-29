# K-Realty Agent — project directives

## Agent Rules
- When reporting information to me, be extremely concise and sacrifice grammar for the sake of concision.

## Quick reference

- Setup: `poetry install`. Two entry points ([pyproject.toml](pyproject.toml)):
  `poetry run agent-web` (REST + browser UI, [interfaces/rest_server.py](interfaces/rest_server.py) +
  [interfaces/web/index.html](interfaces/web/index.html)) and `poetry run agent-mcp`
  (MCP server, [interfaces/mcp_server.py](interfaces/mcp_server.py)). The GUI is the sole
  front-end; the questionary TUI was deleted once the GUI reached parity.
  [scripts/manage_secrets.py](scripts/manage_secrets.py) remains as a CLI for key
  generation and bulk credential edits.
- Tests: `poetry run pytest`. Portability lint (`core/`/`evals/` must stay framework-free):
  `poetry run python scripts/check_portability.py`. Both run in CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)).
- Architecture and the `core/`/`orchestration/`/`evals/` portability contract: see
  [README.md](README.md#architecture-portability-contract).
- Building a new source's parser/scraper: the embedded agent does this, driven by
  [core/prompts/parser_builder.v1.md](core/prompts/parser_builder.v1.md) /
  [core/prompts/scraper_builder.v1.md](core/prompts/scraper_builder.v1.md) — read those
  before touching `core/parsers/` or `core/scrapers/` by hand (see Foundational directive below).

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
  embedded agent writes + TESTS the code → human approves in the GUI.**
  - Documents: `build_parser` (sample document → parser).
  - Portals: `build_scraper` (demonstrated navigation → scraper; capture the data
    request to prefer a direct API call, fall back to replaying recorded clicks).
- **The harness tests the code it writes, and won't ship untested code.** Every
  build makes the agent write a self-contained test (`tests/test_parser_<key>.py`
  / `tests/test_scraper_<key>.py`, inline sample — never a gitignored `data/`
  file). The workflow (`orchestration/verify.py`) re-runs that test independently;
  a missing or failing test is NOT `ok`, and the GUI shows the pass/fail and gates
  approval on it. See `parser_builder.v1.md` / `scraper_builder.v1.md`.

After onboarding, the operator works with the harness (its browser GUI), not a code editor.

## Other standing rules

- **User transparency / nothing hidden.** Never mask a field or behavior unless
  it's a genuine secret. Everything happens via the GUI, visibly.
- **Faithful data.** Preserve each source's ACTUAL columns verbatim in
  `Transaction.fields`; invent nothing. Only `date`/`amount`/`description` are
  normalized.
- **One model choice.** Every LLM call resolves through
  `core/tools/llm_provider.resolve()` — never a hardcoded model constant, never
  `os.getenv` plus a local default. Precedence: caller argument → Settings →
  environment → documented default (Settings beats a stale env var). Whatever
  runs a model also says which one: the agent announces it, an extraction
  records it on the run, Settings shows the resolved model.
- **Portability.** `core/` and `evals/` never import langgraph/langchain (CI lint:
  `scripts/check_portability.py`). The anthropic SDK is allowed.
- **Logging standard.** Every deterministic failure → one structured record via
  `core/observability.py` (`log.failure(...)`); see that module.
- **Financial data never committed.** `data/`, `.secrets/`, `.browser_profiles/`,
  `*.pdf`/`*.csv` are gitignored (with test-fixture exceptions).
