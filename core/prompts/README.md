# Prompts

Versioned files, never inline strings in `orchestration/`.

Each kind of code-gen job is **a contract plus a job**, concatenated at run time
by `_system()` in `orchestration/build_scraper.py` / `build_parser.py`:

| file | sent to | holds |
|---|---|---|
| `<kind>_contract.v1.md` | both jobs | what the code must ALWAYS be — the transaction schema, the settings rules, the test gate, the repo rules |
| `<kind>_builder.v1.md` | build only | how to write one that doesn't exist (for scrapers: how to read a demonstration) |
| `<kind>_reviser.v1.md` | revise only | how to fix one that does — read the logs, change the least, or declare no change needed |

**Why split.** Building and fixing want different instructions, and the half that
doesn't apply is context the agent has to read past on the runs least able to
afford it. Most of the scraper build prompt is the demonstration guide, which is
not even the source of truth on a revise: the failure being fixed happened after
the demonstration was recorded.

**Where a new rule goes.** If a scraper must always satisfy it, the contract. If
it is advice about how to do one of the two jobs, that job's file. Putting an
invariant in a job half means the agent is told about it when building and not
when fixing — `tests/test_prompts_split.py` fails the build on the ones that
matter, and on any prompt that advertises a tool the agent doesn't actually have.
