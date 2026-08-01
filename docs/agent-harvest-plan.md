# Harvesting the OpenHands SDK: a plan for the embedded coding agent

**The problem.** The embedded agent (`orchestration/agent.py`) has trouble writing
working code and working tests, especially on the self-hosted model. This plan
ports four ideas from the OpenHands agent SDK to fix the harness-caused share of
that — the failures that are our tooling's fault rather than the model's ceiling.

Branch: `feat/agent-harvest`, cut from `3b25088` on main.

## Where the useful code actually is

`OpenHands/OpenHands` — the repo with 82k stars — is **Agent Canvas**, a
self-hosted control center: React front-end, Electron shell, an Agent Server REST
API, an Automation Server. It *drives* agents (OpenHands, Claude Code, Codex,
Gemini via ACP); it is not one. Adopting it means replacing our GUI with a generic
one and losing the approval gate and the source graph, which are the product.

The agent is a different repo: **`OpenHands/software-agent-sdk`**, Python, MIT.
All upstream paths below are relative to it, read at `d9f3e16` (v1.39.1,
2026-07-31).

## Why harvest and not adopt

Wholesale adoption pulls in litellm, fastmcp, fakeredis, websockets and laminar,
and costs us three things we'd have to rebuild anyway:

- `outline` — our context-economy tool. Nothing upstream matches it; their
  `code_explorer` subagent uses raw `grep`/`sed`.
- the `log.failure` plumbing that makes `read_logs` worth calling.
- the `on_event` stream the GUI's live view is built on.

So: port ideas and specific files, keep our loop.

## What we already have, and must not replace

**The goal loop.** `run_codegen_gated` in `orchestration/codegen.py` is
structurally identical to their `/goal`
(`openhands-sdk/openhands/sdk/conversation/goal/`): run the agent, judge the
result, re-prompt with what's missing. Ours is capped at one retry.

**The judge, and ours is better.** `orchestration/verify.py` plus the `fold_*`
functions in `codegen.py` run real pytest, real coverage, real ruff, real
reconciliation. Theirs (`goal/judge.py`) is an LLM reading a transcript and
guessing. **Do not add an LLM critic.** Their good one
(`critic/impl/api/`) is a hosted service we can't run locally regardless.

**Stuck detection.** `orchestration/degeneration.py`, `repetition.py` and
`watchdog.py` already cover what their `conversation/stuck_detector.py` does.

---

## Measured so far

Model: `Qwen3-Coder-30B-A3B-Instruct-MLX-8bit` on the local server. One run per
case, so treat single-case moves as weak evidence and the pattern as strong.

**Baseline, `7fb415a` (before Phase 1)** — gate 1/3, correct 1/3:

- `riverbend` (easy): gate FAIL, **correct**. Refused over an unused
  `typing.List` import.
- `summit` (medium): **gate PASS, wrong**. Every sign inverted — `+367.92` where
  the answer is `-367.92`, 5 rows money-in where 5 are money-out. It wrote a test
  that agreed with itself. This is the failure `verify.py` cannot see, and the
  reason the bench scores `correct` separately.
- `harbor` (hard): gate FAIL, wrong. Crashed, then went in circles.

**After Phase 1, `1512def`** — one clean win, two runs void:

- `riverbend`: gate **FAIL → pass**, still correct. Tool mix went from
  `write_file × 6` to `write_file × 1` + `str_replace × 5`. Same work, five
  surgical edits instead of five whole-file re-emissions.
- `summit`, `harbor`: **void, twice.** Not a Phase 1 result — the model server
  refused the prompt outright:

  > `oMLX prefill memory guard rejected this prompt … kv_len=33809 … predicted
  > peak ~37.03 GB (current 33.31 GB + KV 3.10 GB)`

  Reproducible: both cases died on both attempts, at kv_len 17.7k/24.8k then
  33.8k/35.1k. They had done 19–30 turns of real work first.

### What that changes

**Phase 3 (condensation) moves ahead of Phase 2 (more loop rounds).** The two
harder cases cannot finish inside the local model's context at all, so:

- more judged rounds would make the binding constraint strictly worse;
- the bench cannot grade Phase 2 while two of its three cases die of context.

Phase 1 does not fix this and was never going to. The dominant consumer is
whole-file `read_file` — the builder prompt tells the agent to read
`buildium_owner_statement.py` in full, and it also reads `base.py`,
`__init__.py` and `models.py`. That is the 22k measured in
[[agent-context-bloat-is-the-models-own-prose]], untouched by patch editing.
Phase 3a (truncate old tool results in place, no LLM) targets exactly it.

## Phase 0 — a baseline number

- [x] Done. `poetry run python -m orchestration.bench` — see
      [orchestration/bench/README.md](../orchestration/bench/README.md).

Three synthetic documents, scored two ways: `gate` (what `verify.py` approved,
i.e. what the operator sees) and `correct` (the rows against an answer key the
agent never sees). The gap between them is the number that matters, and a
blocker histogram says which gate refused.

Isolation is a git worktree at the measured ref, which is why it needs no
production-code change: `agent_tools.REPO_ROOT` and `verify.REPO_ROOT` come from
`__file__`, so running *inside* a throwaway checkout is what redirects them.

**Two things it caught immediately, both of which would have produced confident
wrong numbers:**

- A worktree has only what git tracks, so `.secrets/` was missing and the run
  silently resolved the built-in default model instead of the one chosen in
  Settings. There is now a preflight that refuses to start unless the worktree
  resolves the same model this repo does.
- `.gitignore` spells the exclusions `.venv/` and `.secrets/` — trailing slash,
  so they match directories. The links the bench adds are *symlinks*, which git
  treats as untracked files the pattern misses, so `git clean` in the between-case
  reset deleted both. The first full run therefore used the wrong model *and* a
  fallback interpreter, and looked fine doing it.

**Cost:** it measures a commit, not the working tree. Commit before measuring.

## Phase 1 — `str_replace` editing

- [x] Done. `orchestration/file_editor.py` (pure logic) +
      `agent_tools.str_replace` / `insert`; `write_file` is create-only.

**Three things came out differently from the plan below, and the plan was
wrong, not the implementation:**

1. **`fold_rewrite` is NOT retired.** The claim further down that it becomes
   "structurally unreachable" is false: an agent can still replace a whole file
   by passing its entire contents as `old_str`. Harder to do by accident,
   still worth catching, still enforced.
2. **`undo_edit` was deliberately skipped.** It is not what causes broken code,
   it needs per-run history state, and every extra tool costs context. Revisit
   only if a bench run shows the agent stuck on an edit it wants to take back.
3. **The gates nearly stopped working, silently.** `codegen.files_written` and
   `untested_code_files` decided what the agent changed by matching the literal
   tool name `write_file`. Adding a second write tool without touching them
   would have meant every gate — test required, coverage, lint, no-op, rewrite —
   quietly stopped firing on any run that used it. There is now one
   `agent_tools.WRITE_TOOLS` set that both read, and a parametrised test that
   asserts each write tool still trips them.

**Why first.** `write_file(path, content)` is the agent's only write tool, so
every edit is a whole-file regeneration — hundreds of lines re-emitted perfectly
or not at all, against a 16k `max_tokens` ceiling. This is the largest
harness-caused source of broken generated code. More loop rounds without this
just produces more bad rewrites, which is why it precedes Phase 2.

The evidence that it's the tool and not the prompt: `parser_reviser.v1.md` §3 has
to *threaten* the model ("THE BUILD FAILS IF YOU REWRITE A FILE", a 40% line
survival check), `fold_rewrite` exists to catch the damage, and `_ORIGINALS` in
`agent_tools.py` exists to show the operator what was lost. Three mechanisms
compensating for one wrong tool.

**Port from** `openhands-tools/openhands/tools/file_editor/`:
- `editor.py` (~815 lines; we want maybe 250)
- `definition.py` — the `TOOL_DESCRIPTION` text is usable near-verbatim
- `exceptions.py`

Take `str_replace`, `insert`, `undo_edit`. Skip their `view` (we have `read_file`
+ `outline`, which are better) and their on-disk history.

**Three details that are the whole point** — port them, don't paraphrase:
1. Exact-match plus a uniqueness check, with the "include 3–5 lines of context"
   guidance in the tool description.
2. Failure messages that teach the retry: *"matched 3 locations, add more
   context"* / *"no match found"*. A bare error produces a flailing retry.
3. On success, echo back the edited region ±2 lines, so the model sees the result
   without re-reading the file.

**Ours to change:**
- new `orchestration/file_editor.py`
- `orchestration/agent_tools.py` — add `str_replace`/`insert` to `TOOL_SCHEMAS`
  and `_DISPATCH`; **restrict `write_file` to files that don't exist** (their
  `create` semantic). This is the load-bearing line.
- `core/prompts/parser_contract.v1.md`, `scraper_contract.v1.md` — tool lists
- `core/prompts/parser_reviser.v1.md`, `scraper_reviser.v1.md` — §3 becomes "use
  `str_replace`" instead of the survival threat

**What it retires:** `fold_rewrite` and the `wholesale_rewrite` blocker become
structurally unreachable rather than load-bearing. Keep `_ORIGINALS` — the
operator still wants the diff.

**What it improves:** `fold_uncovered` gets exact changed lines from the edit
itself instead of inferring them from a whole-file diff.

**Tests:** `tests/test_file_editor.py` — exact match, multi-match rejection,
no-match rejection, whitespace sensitivity, undo. `tests/test_prompts_split.py`
already fails on a prompt advertising a tool the agent lacks, so it will catch a
half-finished prompt update.

**Done when:** the agent edits by patch, `write_file` cannot overwrite, and the
Phase 0 number moved.

## Phase 2 — turn the goal loop up

- [ ] Not started. **Deferred behind Phase 3** — see "What that changes" above.
      More rounds worsen the context exhaustion that is currently killing runs,
      and the bench cannot grade this phase until its cases can finish.

Generalize `run_codegen_gated` from `max_retries=1` to a real loop, cap ~4.

**First, unify the blocker lists.** `verify.blockers()` (operator-facing, nine
conditions) and the `reasons` list in `codegen.py` (agent-facing, the same nine)
are drifting copies of one body of knowledge. Collapse to a single table of
`Blocker(code, operator_text, agent_text)`. Do this *before* adding rounds or
we'll maintain two divergent lists across more paths.

**Then three things upstream doesn't have:**

1. **Stall detection.** If round N's blocker set is identical to round N−1's,
   stop. Otherwise the remaining rounds re-make the same mistake. Their
   `/goal` only caps iterations; the analogous idea is `minimum_progress` in
   their condenser. Report `stalled` distinctly from `capped`.
2. **A carry-forward brief, not a restart.** Today the retry calls `run_agent`
   with a *fresh* conversation (`reasons` + the original task). The context reset
   is right for a small window, but the agent re-derives its exploration every
   round. Pass a short structured brief instead — files touched, what the test
   said, what the last round tried — built deterministically from
   `result.tool_calls` and the verification dict. No LLM call.
3. **A reported outcome.** `complete | capped | stalled`, round count, final
   blockers. The GUI already renders blockers.

**Reference:** `openhands-sdk/openhands/sdk/conversation/goal/controller.py` for
the continue-vs-stop split (the controller does no I/O, so sync and async drivers
share one decision path). `goal/prompts.py` `FOLLOWUP_PROMPT` for the re-prompt
wording.

**Done when:** a build that fails a gate gets up to four judged rounds, stops
early when it stops improving, and says which of the three ways it ended.

## Phase 3 — condensation

- [ ] Not started. **Promoted ahead of Phase 2** — this is now the binding
      constraint, measured, not predicted. Start with 3a; it needs no LLM call
      and targets the whole-file reads that dominate.

We have none: messages grow monotonically until the local model's fixed window
blows. More rounds from Phase 2 makes this urgent. `degeneration.collapse` trims
prose but never drops an old tool result.

**3a — truncate old tool results in place. No LLM.** A `read_file` result from 20
turns ago becomes `[3,200 chars of core/parsers/x.py — re-read if needed]`. This
is most of the 22k tokens of whole-file reads we measured, and it costs one
function in `run_agent`. Keep the last N results intact. Do this first; it may be
enough.

**3b — LLM summarization.** Port
`openhands-sdk/openhands/sdk/context/condenser/prompts/summarizing_prompt.j2` to
`core/prompts/condense.v1.md` — a versioned file, per the `core/prompts/README.md`
rule that prompts are never inline strings in `orchestration/`. Its
`CODE_STATE / TESTS / CHANGES / DEPS` sections are already shaped for our job.

Algorithm from `condenser/llm_summarizing_condenser.py`: keep the first N
messages, forget a middle chunk, splice the summary in at that offset.

**Two adaptations:**
- Their defaults (`max_size=80` events, `keep_first=4`) assume a large window.
  Trigger off estimated **size**, not event count — our ceiling is fixed and small.
- **The trap:** you cannot forget an assistant `tool_use` without its matching
  `tool_result`, or the provider rejects the conversation. Both adapters are
  affected in different shapes — Anthropic is assistant content blocks plus one
  user message of results; OpenAI-compatible is assistant `tool_calls` plus N
  `role:"tool"` messages. Forget-boundaries must land between complete pairs.
  Read `context/view/manipulation_indices.py` and
  `context/view/properties/tool_call_matching.py` before writing ours. This bug
  surfaces as a provider 400 several turns later, not at the condensation site.

**Done when:** a 40-turn run on the local model finishes instead of running out
of room.

## Phase 4 — per-model-family prompt deltas

- [ ] Not started

We send Claude's prompt to the local model. Port the *shape* of
`context/prompts/sections/static.py` `ModelSpecificSection`.

- `core/prompts/model_local.v1.md`, `core/prompts/model_anthropic.v1.md`,
  appended by `_system()` in `build_parser.py` / `build_scraper.py`, selected
  from `llm_provider.resolve()`.
- Local-model content: one tool call per turn, the exact tool-call JSON shape,
  don't restate, act rather than analyze.
- Add their `<TROUBLESHOOTING>` block — "step back, list 5–7 possible sources,
  rank by likelihood, work the highest first" — but only on retry rounds 2+.
  It's a stuck-reset, not general advice.
- Extend `tests/test_prompts_split.py` to cover the new files.

## Phase 5 — role agents

- [ ] Not started. Decide with the Phase 0 number, not in advance.

The end goal: separate agents for separate aspects of coding, run **sequentially**
— we have one self-hosted model, so parallelism isn't available and isn't needed.

**Shape** — port from `openhands-sdk/openhands/sdk/subagent/schema.py`, heavily
trimmed. An agent is one markdown file: YAML frontmatter (`name`, `description`,
`tools`, `max_turns`) and the body is the system prompt. Ours live in
`core/prompts/agents/`, loaded by a new `orchestration/roles.py`. Their shipped
examples are in `openhands-tools/openhands/tools/preset/subagents/*.md` —
`code_explorer.md` is the one worth reading.

**The lesson to copy:** the value is the **restricted tool set, enforced by the
frontmatter**, plus a fresh context — not a cleverer prompt. `code_explorer`
cannot edit a file because `tools: [terminal]` says so. So `agent_tools.dispatch`
needs a per-role allowlist. Same instinct as our verify gates: make the wrong
thing impossible rather than asking for it.

**Roles:**
- `explorer` — `outline`, `search_files`, `read_file`, `list_directory`. Returns
  file paths, line numbers, the pattern to follow. This is where context is
  burned today and the role that most benefits from a throwaway context.
- `writer` — `read_file`, `str_replace`, `write_file` (create-only). No shell.
- `fixer` — `run_command`, `read_logs`, `read_file`, `str_replace`. Runs pytest,
  reads the structured failure, fixes.

Each is a separate `run_agent` with its own fresh context, handing forward the
Phase 2 brief — never a transcript.

**The cost:** serialized calls on one tunneled model. If Phases 1–3 get build
success where we want it, this may not be worth its latency.

---

## Not doing

- **Agent Canvas.** It's a front-end; we have one, and ours holds the approval gate.
- **The SDK as a dependency.** Weight, and we'd lose `outline`, `log.failure`
  and `on_event`.
- **An LLM critic.** `verify.py` is deterministic and better.
- **Their static prompt blocks** — git, pull requests, AI disclosure, security
  risk tiers, browser. Irrelevant to building a parser, and our
  `browser_session.py` + `demo_recorder.py` beat their browser guidance for
  portals.

## Licence

The SDK is MIT (`Copyright (c) 2026 OpenHands contributors`). Copying is fine
with attribution — note the upstream path and licence in the header of any file
we port.

## Promotion

`orchestration/` is domain-agnostic, so Phases 1–5 are all template-promotion
candidates. Record them in `../agent-harness-template`'s promotion log once they
settle.
