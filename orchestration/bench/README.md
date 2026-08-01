# The codegen bench

Answers one question: **how often does the embedded agent build a parser that
actually works?** Everything in [docs/agent-harvest-plan.md](../../docs/agent-harvest-plan.md)
is judged against the number this prints.

```bash
poetry run python -m orchestration.bench
```

## What it does

Three synthetic documents in `tests/fixtures/` (`bench_*.csv`, `bench_*.pdf`),
each a shape no existing parser already handles — otherwise the bench would be
measuring how well the agent copies `dfcu_financial_bank.py`. For each one it
runs the real `build_parser_for_source`, then scores the result two ways:

- **gate** — did `verify.py` approve it? This is what the operator would see.
- **correct** — did the parser actually read the document? Judged against an
  answer key in `cases.py` that the agent never sees: transaction count, the
  signed total, how many rows are money-in vs money-out, and whether the
  source's real columns survived into `Transaction.fields`.

**The gap between the two is the point.** An agent can write a parser that reads
half a statement and a test that agrees with it — its test passes, coverage is
real, nothing is hardcoded, and the gate says yes. Only `correct` notices. If
`gate` runs ahead of `correct`, the gate is being satisfied rather than the
document read.

The **blocker histogram** at the end says which gate refused, across every run.
That is what decides which phase of the plan to do next.

## Isolation

Each run happens in a **git worktree** checked out at `--ref` (default `HEAD`),
with `.venv` symlinked in. `agent_tools.REPO_ROOT` and `verify.REPO_ROOT` are
derived from `__file__`, so running inside a throwaway checkout is what points
the agent at it — no production code knows the bench exists.

Consequences worth knowing:

- **It measures a commit, not your working tree.** Commit before you measure.
  The run warns when you have uncommitted changes.
- The worktree is reset between cases, so one case never inherits the previous
  one's registration in `core/parsers/__init__.py`.
- Your real repo cannot be written to by a bench run.

## Reading a result

Results land in `orchestration/bench/results/<timestamp>/` (gitignored): a
`summary.json` with every run, a `.log` per run holding the agent's full
progress stream, and a copy of the parser and test it wrote. When a run scores
badly the generated code is the first thing to read.

Record headline numbers in the plan doc, not here — the results directory is
scratch.

## Cases

| key | difficulty | the trap |
|---|---|---|
| `bench_riverbend_credit_union` | easy | preamble above the header, parenthesised negatives, a Totals row that is not a transaction |
| `bench_summit_card_services` | medium | the file's sign convention is inverted against ours — a charge is positive in the source |
| `bench_harbor_property_group` | hard | PDF; the sign comes from which of two columns a number sits in, which plain text extraction cannot tell you |

Regenerate the documents with
`poetry run python tests/fixtures/generate_bench_documents.py`.
`tests/test_bench.py` recomputes every expectation straight from the files, so
changing a fixture without updating `cases.py` fails there rather than quietly
moving the baseline.

## Cost

Three builds against a local model take a while, and `--repeat` multiplies it.
One run per case is enough to compare phases; use `--repeat 3` when a change
looks marginal, since a single LLM run is noisy.
