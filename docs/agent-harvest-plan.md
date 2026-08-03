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

**After Phase 3a, `20267d0`** — gate 0/3, correct 1/3, and **no OOM at all**:

| case | before 3a | after 3a | live context | trimmed |
|---|---|---|---|---|
| riverbend | pass / correct | FAIL / correct | ~17.4k tok | ~11.1k |
| summit | void (OOM ×2) | **completed**, wrong signs | ~19.3k tok | ~12.1k |
| harbor | void (OOM ×2) | **completed**, wrong | ~8.0k tok | ~9.3k |

**3a did its job.** The two cases that could not finish now finish. Nothing hit
the prefill guard; live context peaked at 19.3k where the deaths were at
33.8k–35.1k.

**gate 1/3 → 0/3 is not a regression.** The OOM was masking lint failures. All
three now fail the same gate, which makes the next thing to fix unambiguous.

### The lint gate is now the only thing blocking every build

And it is conflating two unrelated things:

- `harbor`: **F821 undefined name `Path`, `datetime`** — a real defect. The
  parser references names it never imported and crashes at runtime. The gate is
  exactly right to refuse this.
- `riverbend`, `summit`: **F401 unused import**, both in the *test* file
  (`pytest`, `core.models.Transaction`). Cosmetic. On riverbend this is the sole
  reason a parser the bench scores **correct** was refused.

The gate's stated rationale is F841 — "a value computed and never used is
usually a wire you forgot to connect; if it came from the operator's settings,
the setting is silently being ignored." An unused import in a test file is not
that. Keep F821 and F841; F401 should not fail a build on its own.

### The cost 3a introduced: trimming every turn

`riverbend` went 299s → 704s, the one case with a before-and-after. Trimming
fires on **30 of 47 turns**, and each one rewrites *old* messages — precisely
the prefix a server would otherwise keep in its KV cache, so every trim likely
forces a full re-prefill. (Plausible and consistent, not proven; n=1 on the
timing.)

The fix is to trim on a size threshold rather than every turn: the cacheable
prefix then stays stable until the conversation actually needs room. Worth doing
before reading anything into bench timings.

**After threshold trimming + the F401 change, `15173ca`** — gate 3/3, correct
1/3, and the bench caught what that bought:

- riverbend 704s → **339s**, 1 round. The KV-cache explanation for the slowdown
  holds: trimming only when there is a reason gave the time back.
- **harbor passed the gate having extracted ZERO transactions** from a statement
  holding six. `ok: True`, its own test green, `blockers: []`. Nothing in the
  harness asked whether the parser did the job — every gate asks whether the
  code is well-formed, and `[]` satisfies all of them. F401 had been the only
  thing in the way, which is an accident, not a gate. Fixed in `e9678a6`.

**After the sign-convention contracts, `912b320`** — gate 1/3, **correct 2/3**:

| case | gate | correct | note |
|---|---|---|---|
| riverbend | FAIL | **yes** | F841: `except ... as e`, unused |
| summit | pass | **yes** | the sign inversion is fixed |
| harbor | FAIL | no | parser raises; F841 + a hardcoded `amount=0.0` |

**`correct` moved for the first time: 1/3 → 2/3.** Summit had been inverted in
every previous run — baseline, Phase 1, 3a, threshold — and no tooling phase
touched it, because it was never a tooling problem. Telling the contract that
the source's sign convention is probably not ours fixed it.

### The gate now refuses more good work than it accepts bad

gate 1/3 is *below* correct 2/3 — the reverse of the baseline, where the gate
passed a parser with every sign inverted. For financial data that is the safer
direction to be wrong in, but it costs a full rebuild each time, and both
refusals are F841:

- harbor: `default_amount` assigned and never used — the real target. That
  parser is genuinely broken.
- riverbend: `except Exception as e` where `e` is unused — in a parser the bench
  scores **correct**.

F841 is earning its place on harbor, so the answer is not to drop it as F401 was
dropped. An unused exception binding is a swallowed error, which is a real smell
but not "a setting silently ignored". The cheap move is to name it in the retry
text so the agent knows the fix (use it in the log record, or drop the binding)
rather than being told only that something "does nothing".

### Three things the instrumentation settled, `3cebbfa`

**1. `str_replace` arguments are now the biggest single consumer.** Measured on
the harbor run at the moment the server refused it:

| | tokens |
|---|---|
| `str_replace` args | 9,324 |
| `run_command` results | 5,693 |
| `str_replace` results | 5,590 |
| `read_file` results | 4,932 |
| `write_file` args | 2,904 |
| system prompt | 1,817 |

`str_replace` and its echo together are ~60% of the live conversation. The plan
predicted the opposite — "a whole-file write parks the entire file in the
conversation forever; an edit parks ~9 lines". In practice the agent makes many
edits, each carrying `old_str` **and** `new_str`, and none of it is trimmable:
**3a collapses tool RESULTS only.** Call arguments live in the assistant
message and are never touched. That is the largest remaining lever, and it is a
gap in 3a rather than a fault in Phase 1.

**2. The context ceiling is not fixed.** This OOM came at kv_len **26,015** with
`current 34.28 GB`, where earlier deaths were at 33.8k–35.1k with 33.3–34.9 GB
resident. Headroom moves with whatever else is on the host, so a threshold tuned
to "~33k is the wall" is tuned to a number that isn't stable. 48,000 chars is
probably too high; the honest fix is to drive it from what the server reports
rather than from a constant.

**3. Single runs are noise, and this document has been over-reading them.**
riverbend's gate verdict across six runs: FAIL, pass, FAIL, pass, FAIL, FAIL —
with `correct` flipping to no on the last, where it hit the 40-turn cap. Two
signals have been robust enough to trust:

- **3a removing the OOM deaths** — two cases died twice each, then all three
  completed.
- **the sign-convention contracts** — summit was wrong in five consecutive runs
  and right in the two since.

Everything else reported here as a result sits inside run-to-run variance. Use
`--repeat 3` for any comparison that matters, and read the per-case gate column
as noise unless it moves the same way repeatedly.

### The n=3 re-baseline, `5b59f43` — and a claim it retracts

Nine builds, three per case. **gate 5/9 (56%), correct 2/9 (22%).**

| case | gate | correct | note |
|---|---|---|---|
| riverbend | 2/3 | **2/3** | the third died on `IncompleteRead`, a transport fault, not code |
| summit | 2/3 | **0/3** | sign inversion, all three |
| harbor | 1/3 | **0/3** | never correct in ~9 attempts total |

**Retraction: the sign-convention contracts did not fix summit.** This document
said they had, on the strength of two consecutive correct runs. At n=3 it is
0/3, and 2/5 across every run since the change — indistinguishable from the
coin-flip it looked like a fix for. The lesson about n=1 applied to the one
result claimed as robust, which is exactly where it was least welcome.

**No OOM in nine builds.** That signal does hold: the prefill refusals that
killed two cases four times over are gone, across three times the sample.

**The real problem is now the false-pass rate: 3 of 9 approved while wrong.**
One of those approved harbor having extracted **1 transaction out of 6**. The
empty-extraction gate catches nothing-at-all and cannot see a partial read.

What would: **reconciliation, which parsers are not required to do.** The
scraper contract makes it mandatory — `reconcile.record(...)` against the
source's own totals, or an explicit `NO_CONTROL_TOTALS`, and the build fails
without it. The parser contract only suggests it "if the document shows totals".
Every bench document publishes one: riverbend a Totals row and a running
balance, summit a New Balance, harbor per-property subtotals and a net line.
Harbor's 1-of-6 would have failed on arithmetic alone.

That is parity with a rule this project already argues for in its own words —
*the ONLY signal that answers "did we pull everything"* — and it is the shortest
path to the failure that matters most for financial data.

**Turn exhaustion is the joint-top blocker**: "hit the 40-turn cap" in 3 of 9,
all on summit and harbor, which matches the observed habit of debugging by
re-running `python -c` with print statements instead of reading the failure.

### Reconciliation, n=3 at `41e60d7` — the false passes are gone

**gate 2/9, correct 2/9, approved-but-wrong 0/9.**

| case | gate | correct |
|---|---|---|
| riverbend | 2/3 | 2/3 |
| summit | 0/3 | 0/3 |
| harbor | 0/3 | 0/3 |

The number this was aimed at: **3/9 approved-while-wrong became 0/9.** Every
build the harness now approves is correct — precision went from 40% (2 of 5
approvals right) to 100% (2 of 2). Harbor's approved 1-of-6 read cannot recur;
its own subtotals refuse it.

`correct` did not move, and was not going to: reconciliation cannot make a
parser right, only stop a wrong one shipping. What it bought is that the
operator is no longer handed a finished build that silently ingests a fraction
of a statement — the worst failure available on financial data.

The cost is honest and worth stating: fewer approvals (5/9 to 2/9) and longer
runs (up to 702s), because a gate that refuses more causes more retries.

**Turn exhaustion is now the dominant blocker: "hit the 40-turn cap" in 4 of 9**,
up from 3. Summit and harbor both burn their budget re-running `python -c` with
print statements instead of reading the failure they already have. That is the
next thing to fix, and it is prompt work.

Two other notes from this run: summit's inversion survived again (2 of 3, the
third lost to a transport fault), so the sign contracts remain unproven; and
`IncompleteRead` from the local server cost 2 of 9 builds outright — flakiness
worth watching, not yet worth handling.

### The debugging-loop fix did not work, `0436326`

**gate 1/9, correct 2/9, approved-wrong 0/9, turn cap 5/9** — the cap went UP,
from 4 of 9.

The behaviour did shift, slightly and in the right direction: `run_command`
11.1 → 9.7 calls per run, `search_files` 0.6 → 1.2. So the agent read the
prompt and did somewhat less of what it was told not to. It changed nothing
about whether builds succeed.

**Which suggests the turn cap was a symptom, not a cause.** Look at where it
fires:

| case | correct | caps |
|---|---|---|
| riverbend | 2/3 | none |
| summit | 0/3 | yes |
| harbor | 0/3 | yes |

It fires on exactly the two cases that are *never* correct — summit across every
run since the bench existed, harbor in roughly eighteen attempts. That is what a
model burning its budget on a problem it cannot solve looks like. Better habits
make such a run cheaper, not successful, which is precisely the result observed.

The guidance is kept: it is correct on its merits, it moved the behaviour, and
removing it would cost another hour to re-measure. But it is unproven as a fix
and should not be counted as one.

### The question that now blocks everything else

Is what remains the **model's ceiling** or the **harness's fault**? Every
intervention left is guesswork until that is settled, and the experiment is
cheap: run the same three cases against a stronger model. The bench already
measures whatever Settings resolves, and its preflight makes a model mismatch
impossible to report by accident.

- If a stronger model gets summit and harbor right, the harness is fine and the
  local 30B is the limit — which is an operational answer (bigger model for
  builds, or accept it), not a harness one.
- If a stronger model fails them too, the fault is in the prompts or the cases
  themselves, and there is something real left to fix.

Either answer redirects the work. Neither can be guessed from here.

### Answered, `904aae1` — it was the model, and the bench was leaking

Ran the same three cases against Claude Code (sonnet), `--repeat 1`. **gate 3/3,
correct 3/3.** `correct` had never been above 2/9 before, and summit had been
wrong in every run since the bench existed.

Two things have to be said about that number before it is used.

**Harbor's result does not count.** While looking for the project's conventions
the agent read `orchestration/bench/cases.py` and `tests/test_bench.py` — the
answer key and the file asserting `Decimal("3547.75")` — and then reported "6
rows, total +3547.75". Nothing errored. The run simply stopped being a
measurement of reading the document. The bench's premise, "an answer key the
agent never sees", was never true: the key sat in the repository the agent is
handed and told to explore. The local 30B never went looking, so the hole stayed
shut by luck for eighteen runs. `_scrub_answer_key` now removes those files from
the worktree and commits the removal, so `git show HEAD:` cannot reach them
either, and `one.py` takes the case as arguments instead of importing it.
`tests/test_bench.py` greps every tracked file for each case's total and fails if
one appears somewhere the bench does not scrub. Residual, and worth stating: the
worktree shares the parent's object database, so `git log -p` would still find it.

**What does count: riverbend and summit, both clean, both correct.** Neither log
touches the key. Summit is the sign-convention case — the one that was wrong in
every previous run, and the one the retracted `5b59f43` claim was about — and it
inverted the sign correctly and reconciled against the document's own "New
Balance 367.92". That is a real first, on n=1.

So the blocking question resolves toward **the model's ceiling, not the
harness's fault**: the same prompts, the same gates, the same reconciliation
requirement, a different model, and two cases that never worked now work. The
harness-side work already done (reconciliation, the gate classification, the
budget note) is what those runs passed through, and it held.

**Harbor re-run at `ed7c23c`, scrubbed: gate pass, correct, 18 turns, 420s.** The
log touches none of the four key files — it could not; they are not in the
checkout. It reconciled against the document's own "Net owner activity", which is
the mechanism the hard case exists to test, and put the property name in
`fields["Property"]`. So the contaminated run's *answer* was right; only its
*provenance* was worthless. All three cases now stand on clean runs.

What this does NOT establish: n=1 per case, so summit — the case the retracted
`5b59f43` claim was about — deserves `--repeat 3` before it is treated as
settled. And 3a's context work is unmeasurable here: Claude Code manages its own
context, so the ledger reports ~3k tokens because it only ever sees prose and
tool calls.

### What that changes

**Phase 3 (condensation) moves ahead of Phase 2 (more loop rounds).** The two
harder cases cannot finish inside the local model's context at all, so:

- more judged rounds would make the binding constraint strictly worse;
- the bench cannot grade Phase 2 while two of its three cases die of context.

Phase 1 does not fix this and was never going to.

### What actually fills the context — measured, not assumed

Decomposed from the `summit` run at the moment the server refused it
(kv_len 33,809):

| | tokens | share |
|---|---|---|
| system prompt + tool schemas | ~3,089 | 9% |
| the model's own prose | ~2,216 | 7% |
| `read_file` results | ~3,411 | 10% |
| unaccounted | ~25,100 | 74% |

**An earlier draft of this document said whole-file reads were the dominant
consumer. That was wrong** — they are about a tenth. The 74% is arithmetic
(kv_len minus the measured parts) and is attributable to `run_command`:
**17 of summit's 32 tool calls** and 12 of harbor's 29, being `pytest -v` runs
and a long tail of `python -c` debug scripts — eight consecutively on summit.
Each result is capped at `MAX_OUTPUT_CHARS` = 8,000 chars (~2,000 tok) and
every one stays in the conversation for the rest of the run.

Splitting that remainder exactly (command output vs the `write_file` /
`str_replace` payloads) needs the loop instrumented; the direction does not.

Two consequences for Phase 3:

- **3a targets stale `run_command` output first**, not stale file reads. Same
  mechanism, right target — reads would have bought ~10%.
- **The agent debugs by print statement**, re-running `python -c` with debug
  output rather than reading the failure it already has. That is a prompt
  problem as much as a context one, and it is what generates the volume. Worth
  a line in the contract prompts pointing at `read_logs` and targeted
  assertions.

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

- [x] **3a done** (`20267d0`) — context accounting, then collapsing stale tool
      results. Measured: the OOM deaths are gone; live context peaks at ~19.3k
      tokens where runs used to die at 33.8k–35.1k.
- [ ] 3a follow-up: trim on a size threshold instead of on every turn, so the
      cacheable prefix stops being rewritten. See "The cost 3a introduced".
- [ ] 3b (LLM summarisation) — not started, and possibly unnecessary: 3a alone
      got every case under the ceiling. Revisit only if runs start dying again.

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
