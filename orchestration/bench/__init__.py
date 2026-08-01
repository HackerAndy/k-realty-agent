"""The codegen bench — measures the embedded agent, not the parsers it writes.

`evals/` scores the harness's decisions about transactions and is under a
portability lint that forbids importing `orchestration/`. This asks a different
question — can the agent write working code — which requires driving the agent,
so it lives here instead.

Run it with `poetry run python -m orchestration.bench`; see that module's
docstring, and `orchestration/bench/README.md`.
"""
