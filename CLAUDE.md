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
- Python **3.14**, Poetry. Tests: `poetry run pytest`. Portability lint (`core/`/`evals/`
  must stay framework-free): `poetry run python scripts/check_portability.py`. Both run in
  CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)), which installs with Poetry —
  `pip install -e ".[dev]"` cannot work here (metadata is `[tool.poetry]`, dev deps are a
  Poetry group, four top-level packages defeat setuptools' flat-layout discovery) and
  silently failed every CI run for months.
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
- **What the gate checks, all of it** (`orchestration/verify.py` + the `fold_*`
  functions in `orchestration/codegen.py`). Each exists because the one before it
  passed something broken; `verify.blockers()` turns whichever fired into the
  sentence the operator reads, so the screen never says "the test failed" about
  something else:
  - the test exists and passes;
  - the test actually EXECUTES the changed lines (`covers_changes`) — a stale
    green test and real coverage look identical to "was a test written?";
  - no configuration-shaped literals baked into the code (`hardcoded_options`),
    escape hatch `# fixed: <reason>` per line;
  - nothing in the code that does nothing (`lint`, ruff `F,E9`) — a value computed
    and discarded is usually a setting being silently ignored;
  - the run wrote something at all (`fold_noop`);
  - the agent didn't end itself — `orchestration/watchdog.py` (a run gone silent,
    budgeted against its own pace) and `orchestration/repetition.py` (the same
    call three times gets one nudge, then the run stops).
  The agent gets ONE automatic retry with the reason, then the operator sees it.

After onboarding, the operator works with the harness (its browser GUI), not a code editor.

## The shape of ingestion (where each screen's settings belong)

Ingesting anything is the same three questions, and the UI must keep them apart:

1. **What is the source?** A body of financial data — a statement, a bank
   account. Not a route, and not a mailbox.
2. **How do we get at it?** *Access*: a sign-in. A username+password, a portal
   login, a Google token for an inbox. Shared, reusable, secret → **Settings →
   Sign-ins**, and nothing else lives there.
3. **What do we take?** *Configuration*, per source → **Data ingestion**, on that
   source: which message carries the attachment (sender, subject, attachment
   type, how far back), which dropdown and date range to ask the portal for.
4. **What reads it?** Getting the data and reading it are separate acts that fail
   separately — a mailbox that won't connect and a PDF the parser can't read are
   not the same problem — so they are separate nodes on that source's graph.

The test for where a setting goes: could two sources share it? A mailbox is
shared (access). "Subject contains Owner's Statement" is one source's business
(configuration). Putting a search on the inbox capped a connected account at one
source — see `Service.email_search` in `core/tools/service_manifest.py`.

**Routes and readers** (`core/transports.py`, `core/readers.py`). A source has
several ways IN — File upload, Website, Mailbox — and each has exactly ONE
reader, named for what it is: `Parser · <name>`, `API call`, `Replays your
clicks`, `Read by the model · <name>`, or `No reader yet`. Upload and Mailbox
converge on the same parser, because they really do hand the document to the
same code. Two rules the screen depends on:

- **What RAN beats what is configured.** Rows a model produced must never look
  like rows a tested parser produced, so a run reports the reader that actually
  read it, model name and all.
- **A count belongs to the run that produced it.** Runs are stored per route
  (`data/parsed/<source>-<route>-<month>.json`); a route that has never run says
  "Not run · by this route" rather than borrowing another route's number.

## Other standing rules

- **User transparency / nothing hidden.** Never mask a field or behavior unless
  it's a genuine secret. Everything happens via the GUI, visibly.
- **Guide the operator through hard access setups.** Gmail OAuth (a Cloud
  project, an enabled API, a consent screen, a Desktop client) is the example:
  the GUI spells out the steps, says what is stored and where, and says the scope
  is read-only. Never reduce a fiddly setup to one unexplained button.
- **Faithful data.** Preserve each source's ACTUAL columns verbatim in
  `Transaction.fields`; invent nothing. Only `date`/`amount`/`description` are
  normalized.
- **A source declares its own options; the harness renders them.** The choices a
  portal asks for before handing over data belong in a module-level `SETTINGS`
  list, read at run time via `settings.values_for(SERVICE_KEY)` — never baked
  into the code. The GUI knows none of Epic's fields; it renders whatever it
  finds, which is why adding a source needs no UI change. Options only the portal
  knows (the properties on an account) declare `"discovered": True` and are
  published with `settings.record_options()` — never by writing
  `core/policies/source_settings.yaml` directly, and never by mutating `SETTINGS`
  at import. **A declared setting must reach the request**: reading a value and
  not using it is worse than hardcoding, because the screen then offers a choice
  that silently does nothing. The lint gate fails the build on it.
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
