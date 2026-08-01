"""The codegen bench: how often does the embedded agent build a parser that works?

    poetry run python -m orchestration.bench                 # every case, once
    poetry run python -m orchestration.bench --repeat 3      # three runs each
    poetry run python -m orchestration.bench --case bench_summit_card_services

**It measures a commit, not your working tree.** Each case runs inside a git
worktree checked out at `--ref` (default HEAD), so results are attributable to a
SHA and the agent physically cannot write into the repo you are editing. Commit
before you measure; uncommitted changes are not in the run.

Two numbers come out, and the gap between them is the point. `gate` is how often
`verify.py` approved the build — what the operator would have seen. `correct` is
how often the parser actually read the document, judged against an answer key the
agent never sees. `gate` above `correct` means the gate is being satisfied rather
than the document read.

The blocker histogram at the end says WHICH gate refused, across every run. That
is the number that decides what to fix next.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import datetime, UTC
from pathlib import Path

from core.tools import llm_provider
from orchestration import context_budget
from orchestration.bench.cases import BY_KEY, CASES, score

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RESULTS_ROOT = Path(__file__).resolve().parent / "results"
VENV = REPO_ROOT / ".venv"
SECRETS = REPO_ROOT / ".secrets"


def _git(*args: str, cwd: Path = REPO_ROOT) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {(proc.stderr or proc.stdout).strip()}")
    return proc.stdout.strip()


def _make_worktree(ref: str, parent: Path) -> tuple[Path, str]:
    """A throwaway checkout of `ref`, with the two gitignored directories a run
    needs linked in.

    Both links are load-bearing, and both are needed because a fresh worktree
    contains only what git tracks:

    - `.venv` is what makes `poetry run` work in there. Poetry is configured for
      an in-project virtualenv. Linking rather than installing keeps setup to
      seconds, and the packages are identical because it is the same venv.
    - `.secrets` is what makes the run use the model the operator actually
      chose. `CredentialStore` reads `REPO_ROOT/.secrets`, so without this the
      inner run finds no stored credential, falls back to the built-in default
      provider, and benchmarks a different model than the one you asked about —
      quietly, if that provider happens to have a key in the environment.

    Links, not copies: no secret material is written into the temp directory,
    and both disappear with the worktree. `_reset` uses `git clean` without
    `-x`, so neither link is swept between cases.
    """
    path = parent / "worktree"
    _git("worktree", "add", "--detach", str(path), ref)
    sha = _git("rev-parse", "HEAD", cwd=path)
    _link_support_dirs(path)
    return path, sha


def _link_support_dirs(worktree: Path) -> None:
    """(Re)create the `.venv` and `.secrets` links. Idempotent, and called again
    after every reset — see `_reset` for why they do not survive on their own.
    """
    for link, target in ((".venv", VENV), (".secrets", SECRETS)):
        destination = worktree / link
        if target.is_dir() and not destination.exists():
            destination.symlink_to(target)


def _reset(worktree: Path, sha: str) -> None:
    """Put the worktree back to `sha` between cases.

    Without this each case inherits the last one's registrations in
    `core/parsers/__init__.py`, and a repeat run measures a repo that already
    contains the answer.

    The `-e` flags and the re-link are both needed, and neither is belt-and-
    braces. `.gitignore` spells these `.venv/` and `.secrets/`, with the
    trailing slash that matches a directory — but what we put here is a
    *symlink*, which git sees as an untracked file the pattern does not cover,
    so a plain `git clean -fd` deletes both. It did: the first bench run reset
    the links away and then spent three builds on the wrong model with a
    fallback interpreter. The excludes stop it happening; the re-link means a
    future change to those patterns cannot quietly break it again.
    """
    _git("reset", "--hard", sha, cwd=worktree)
    _git("clean", "-fd", "-e", ".venv", "-e", ".secrets", cwd=worktree)
    _link_support_dirs(worktree)


def _preflight(worktree: Path, expected: str) -> None:
    """Refuse to start unless the worktree resolves the same model we do.

    Cheap, and it replaces the worst failure this thing can have: three finished
    builds and a summary that is quietly about a different model than the one
    the operator chose. Run after a reset, because that is the state the cases
    actually execute in.
    """
    proc = subprocess.run(
        ["poetry", "run", "python", "-c",
         "from core.tools import llm_provider; print(llm_provider.resolve().describe())"],
        cwd=str(worktree), capture_output=True, text=True, timeout=180,
    )
    resolved = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    if resolved != expected:
        raise SystemExit(
            f"[bench] the worktree resolves '{resolved or (proc.stderr or '').strip()[-300:]}' "
            f"but this repo resolves '{expected}'.\n"
            f"        Every number would be about the wrong model, so nothing was run.\n"
            f"        The worktree only has what git tracks plus the .venv/.secrets links; "
            f"check those exist in {worktree}."
        )


def _run_case(worktree: Path, case_key: str, out_json: Path, log: Path, timeout: int,
              keep_last: int | None = None) -> None:
    """Drive one build in the worktree, teeing the agent's own progress to a log.

    `keep_last` reaches the agent as an environment variable because the build
    workflow between here and `run_agent` takes no such argument, and threading
    one through it for the bench's benefit would be the bench changing
    production code to measure it.
    """
    env = dict(os.environ)
    if keep_last is not None:
        env["AGENT_KEEP_LAST_RESULTS"] = str(keep_last)
    proc = subprocess.Popen(
        ["poetry", "run", "python", "-m", "orchestration.bench.one", case_key, str(out_json)],
        cwd=str(worktree),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    with log.open("w", encoding="utf-8") as handle:
        try:
            for line in proc.stdout:
                handle.write(line)
                sys.stdout.write(line)
                sys.stdout.flush()
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            handle.write(f"\n[bench] killed after {timeout}s\n")


def _keep_artifacts(worktree: Path, case_key: str, into: Path) -> None:
    """Copy out what the agent wrote, before the worktree is reset over it.

    The generated parser and its test are the only way to see WHY a run scored
    the way it did once the checkout is gone.
    """
    into.mkdir(parents=True, exist_ok=True)
    for relative in (f"core/parsers/{case_key}.py", f"tests/test_parser_{case_key}.py"):
        source = worktree / relative
        if source.is_file():
            shutil.copy2(source, into / Path(relative).name)


def _report(rows: list[dict]) -> None:
    if not rows:
        print("\nNo runs completed.")
        return

    print("\n" + "=" * 78)
    print(f"{'case':<34}{'difficulty':<11}{'gate':<7}{'correct':<9}{'rounds':<8}{'secs'}")
    print("-" * 78)
    for row in rows:
        print(f"{row['case_key']:<34}{row['difficulty']:<11}"
              f"{'pass' if row['gate_ok'] else 'FAIL':<7}"
              f"{'yes' if row['correct'] else 'no':<9}"
              f"{row['rounds']:<8}{row['seconds']:.0f}")
        if row.get("context"):
            print(f"    {row['context']}")
        for miss in row["misses"]:
            print(f"    ~ {miss}")

    total = len(rows)
    gate = sum(1 for r in rows if r["gate_ok"])
    correct = sum(1 for r in rows if r["correct"])
    print("-" * 78)
    print(f"gate approved {gate}/{total} ({gate / total:.0%})   "
          f"actually correct {correct}/{total} ({correct / total:.0%})")
    if gate > correct:
        print(f"  {gate - correct} run(s) passed the gate while reading the document wrong.")

    histogram = Counter(b for row in rows for b in row["blockers"])
    if histogram:
        print("\nWhat refused the builds, most common first:")
        for blocker, count in histogram.most_common():
            print(f"  {count:>3}x  {blocker[:110]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="orchestration.bench", description=__doc__)
    parser.add_argument("--ref", default="HEAD", help="Git ref to measure (default HEAD).")
    parser.add_argument("--repeat", type=int, default=1, help="Runs per case (default 1).")
    parser.add_argument("--case", action="append", dest="cases", metavar="KEY",
                        help="Only this case; repeatable. Default: all.")
    parser.add_argument("--timeout", type=int, default=1800,
                        help="Seconds per build before it is killed (default 1800).")
    parser.add_argument("--keep-last", type=int, default=None, metavar="N",
                        help="How many recent tool results the agent keeps verbatim "
                             "(0 keeps none). Default: the agent's own.")
    parser.add_argument("--no-trim", action="store_true",
                        help="Leave every tool result in the conversation, as it was "
                             "before context trimming existed. Use this for the "
                             "before-number when measuring what trimming is worth.")
    args = parser.parse_args(argv)

    selected = [BY_KEY[k] for k in args.cases] if args.cases else list(CASES)
    keep_last = context_budget.KEEP_ALL if args.no_trim else args.keep_last

    # Fail before spending an hour on it if no model is configured.
    choice = llm_provider.resolve()
    print(f"[bench] model: {choice.describe()}")

    dirty = _git("status", "--porcelain")
    if dirty:
        print(f"[bench] NOTE: {len(dirty.splitlines())} uncommitted change(s) are NOT "
              f"in this run — the bench measures {args.ref} as committed.")

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    results_dir = RESULTS_ROOT / stamp
    results_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="k-realty-bench-") as parent:
        worktree, sha = _make_worktree(args.ref, Path(parent))
        print(f"[bench] measuring {sha[:12]} in {worktree}")
        try:
            _reset(worktree, sha)
            _preflight(worktree, choice.describe())
            for attempt in range(1, args.repeat + 1):
                for case in selected:
                    tag = f"{case.key}.{attempt}"
                    print(f"\n{'=' * 78}\n[bench] {tag} ({case.difficulty})\n{'=' * 78}")
                    _reset(worktree, sha)
                    out_json = results_dir / f"{tag}.json"
                    _run_case(worktree, case.key, out_json, results_dir / f"{tag}.log",
                              args.timeout, keep_last=keep_last)
                    _keep_artifacts(worktree, case.key, results_dir / tag)

                    if not out_json.is_file():
                        record = {"case_key": case.key, "verification": {
                            "ok": False, "error": "the build produced no result "
                                                  "(killed, or it crashed before writing one)"},
                            "blockers": ["The build produced no result."],
                            "rounds": 0, "seconds": 0.0, "tool_calls": []}
                        out_json.write_text(json.dumps(record, indent=2), encoding="utf-8")
                    else:
                        record = json.loads(out_json.read_text(encoding="utf-8"))

                    # The run is only a measurement of the harness if it ran the
                    # model the harness would have run. A worktree missing the
                    # credential store resolves the built-in default instead,
                    # and every number below would silently be about that.
                    ran = (record.get("model") or "").removeprefix("[model] ").strip()
                    if ran and ran != choice.describe():
                        print(f"[bench] WARNING: this run used {ran}, not "
                              f"{choice.describe()}. The result is not comparable.")

                    outcome = score(case, record.get("verification") or {})
                    rows.append({
                        "case_key": case.key,
                        "attempt": attempt,
                        "difficulty": case.difficulty,
                        "gate_ok": outcome.gate_ok,
                        "correct": outcome.correct,
                        "built": outcome.built,
                        "misses": outcome.misses,
                        "blockers": record.get("blockers") or [],
                        "rounds": record.get("rounds", 0),
                        "seconds": record.get("seconds", 0.0),
                        "tool_calls": len(record.get("tool_calls") or []),
                        "context": record.get("context", ""),
                    })
        finally:
            _git("worktree", "remove", "--force", str(worktree))

    summary = {
        "ref": args.ref,
        "sha": sha,
        "keep_last": keep_last,
        "model": choice.describe(),
        "started": stamp,
        "runs": rows,
    }
    (results_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    _report(rows)
    print(f"\n[bench] results, logs and generated code: {results_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
