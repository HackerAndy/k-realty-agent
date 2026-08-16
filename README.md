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
cd frontend && npm install && npm run build && cd ..
```

The frontend build is a one-time (or per-change) step, not something
`agent-web` does for you — it serves whatever's already in
`interfaces/web/dist/` (gitignored, generated). Rebuild after touching
anything under `frontend/src/`.

## Running the agent

The browser GUI is the front-end — everything the operator does happens there:

```bash
poetry run agent-web
```

It serves http://127.0.0.1:8765. Three pillars: **Ingest** (each source, how
its data arrives, and the transactions it produced), **Process** and **Report**
(not built yet, and the screen says so). **Settings** holds the LLM provider and
every sign-in — portal logins, the Gmail inbox, the API key — stored encrypted
in `.secrets/` and never shown back to you. All data stays local: parsed output
in `data/` (gitignored).

The same tool surface is also exposed over MCP for a Claude host:

```bash
poetry run agent-mcp
```

### Desktop app

```bash
cd desktop && npm install && npm start
```

Opens the same GUI in a native window — `desktop/main.js` spawns `poetry run
agent-web` as a child process, waits for it to answer, and points a
`BrowserWindow` at it. No separate frontend code: it's the identical
`frontend/dist` bundle making the identical `/api/tool/...` calls, just inside
Electron's Chromium instead of your regular browser. Requires Poetry and the
frontend build to already be set up (see Setup, above) — packaging a
self-contained desktop build that doesn't need Poetry installed is not done
yet.

### Frontend development

```bash
cd frontend && npm run dev
```

Runs Vite's dev server with hot reload, proxying `/api/*` to a separately
running `poetry run agent-web` (port 8765) so `callTool()`'s relative
`fetch('/api/tool/...')` keeps working unchanged. `frontend/src/legacy/app.js`
is the pre-modularization dashboard, moved into this project mechanically and
unchanged in behavior; `frontend/src/views/` is where logic is being pulled
out of it view-by-view (see `wizard.js` for the pattern: import shared state
and helpers from `legacy/app.js`, export what `legacy/app.js` needs back —
`addSourceHTML` — and re-attach every function an inline `onclick`/`onchange`
in this file's own generated markup calls onto `window`, since ES modules
don't do that automatically the way the old single `<script>` did).
`tests/test_web_dashboard.py` guards this contract — a function that exists
but isn't window-exported is invisible to a click, not a compile error.

**Adding a source.** `core/policies/services.yaml` is the registry: each source
with its `input_type`, `access`, `parser`/`scraper`, and `status`. The code that
reads a source is **written by the harness's own agent**, not by hand — you give
it a real sample document (or demonstrate the portal navigation) and it writes
the parser/scraper *and a test*, which the harness re-runs independently before
you approve it. See the Foundational directive in [CLAUDE.md](CLAUDE.md).

When a source has no parser yet, or its parser can't read this month's layout,
the GUI offers to read the document with the configured model instead — enough
to get today's numbers, explicitly marked unverified, and gated on your consent
because it's the step where a document's text leaves the parser.

Credentials are managed in Settings. The CLI equivalent still exists for
first-run key generation and bulk edits:

```bash
poetry run python scripts/manage_secrets.py generate-key   # once; save the key
poetry run python scripts/manage_secrets.py setup          # guided, all services
```

## Portability check

```bash
poetry run python scripts/check_portability.py
```

## Troubleshooting

- **Local LLM unreachable on macOS** — a local/LAN model server fails with
  `[Errno 65] No route to host` even though `curl` reaches it from the same
  shell. This is macOS Local Network privacy denying the Python binary, not a
  network fault. Cause, the persistent SSH-tunnel workaround (with launchd
  plist), and why granting the permission isn't durable across interpreter
  upgrades: [docs/local-llm-on-macos.md](docs/local-llm-on-macos.md).
