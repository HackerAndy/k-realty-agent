"""Repo-scoped tools the embedded agent can call.

These are what make the harness able to CREATE / DEBUG / MAINTAIN its own
code: read files (to study existing parsers), write files (to author a new
parser), run commands (to test it), and list directories. Every path is
resolved inside the repo and escapes are rejected; commands run from the
repo root with a timeout and truncated output.

This layer is intentionally in orchestration/ (the agent layer), not core/
— core stays framework-free business logic; this is the agent's hands.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# Was 20,000. Three reads at that size are ~15,000 tokens — most of a local
# model's usable context spent before it has done anything. The cap is a backstop
# now that the agent can slice and search instead of swallowing whole files.
MAX_OUTPUT_CHARS = 8000
# Past this, a whole-file read gets told there was a cheaper way to ask.
NUDGE_TO_SLICE_CHARS = 6000
COMMAND_TIMEOUT_S = 180

SEARCHABLE_SUFFIXES = frozenset({".py", ".md", ".yaml", ".yml", ".json", ".html", ".toml"})
SKIP_DIRS = frozenset({
    ".git", ".venv", "node_modules", "__pycache__", ".browser_profiles",
    ".secrets", ".pytest_cache", ".ruff_cache",
    # Worktrees hold a whole second copy of the repo. Every hit in one is a
    # duplicate of a real hit, at an old revision, and an agent that reads one is
    # editing a file nobody ships.
    ".claude", "worktrees",
})


class ToolError(Exception):
    """Returned to the agent as an error tool_result (not raised to the caller)."""


def _resolve(path: str) -> Path:
    """Resolve a repo-relative (or absolute-inside-repo) path, rejecting any
    path that escapes the repo."""
    candidate = (REPO_ROOT / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
    if candidate != REPO_ROOT and REPO_ROOT not in candidate.parents:
        raise ToolError(f"Path '{path}' is outside the repository — refused.")
    return candidate


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n... [truncated, {len(text) - MAX_OUTPUT_CHARS} more chars]"


def read_file(path: str, start_line: int = 0, end_line: int = 0) -> str:
    """Read a file, or a slice of one.

    The slice exists because whole-file reads are what actually fills the context.
    Measured on a build that died: eight reads pulled in 90,000 characters —
    ~22,500 tokens — including all 21,000 of browser_session.py to find one
    function, and all 20,000 of a DIFFERENT source's scraper. The agent needed
    perhaps a tenth of it. Reading is cheap to ask for and expensive to hold, and
    on a local model the context it crowds out is the run itself.

    Lines are 1-based and inclusive, matching what `search_files` reports, so the
    natural next move after a hit at line 402 is to read around 402.
    """
    p = _resolve(path)
    if not p.is_file():
        raise ToolError(f"No file at '{path}'.")
    text = p.read_text()
    if not (start_line or end_line):
        if len(text) > NUDGE_TO_SLICE_CHARS:
            total = text.count("\n") + 1
            return _truncate(_numbered(text, 1)) + (
                f"\n\n[{path} is {len(text):,} characters / {total} lines. If you only "
                "need part of it, call search_files to find the line, then read_file "
                "with start_line/end_line — holding a whole large file in context "
                "crowds out what you read next.]"
            )
        return _truncate(text)

    lines = text.splitlines()
    start = max(1, start_line or 1)
    end = min(len(lines), end_line or len(lines))
    if start > len(lines):
        raise ToolError(
            f"'{path}' has {len(lines)} lines — line {start} is past the end."
        )
    body = _numbered("\n".join(lines[start - 1:end]), start)
    return _truncate(f"{path} lines {start}-{end} of {len(lines)}:\n{body}")


def _numbered(text: str, first: int) -> str:
    """Line-numbered text, so a slice can be asked for by number next time."""
    return "\n".join(
        f"{first + i:>5}  {line}" for i, line in enumerate(text.splitlines())
    )


def outline(path: str) -> str:
    """A module's public surface: what you can call, and what each thing is for.

    This is the answer to "which of these 37 modules do I need, and how do I use
    it" — and it is nearly free. Measured across every build on disk, the agent
    referred to exactly ONE member of `browser_session.py` (`launch`) and read all
    21,219 characters to find it. Half that file is docstrings and comments, which
    is right for the humans who maintain it and pure cost to an agent that needs a
    signature.

    The alternative people reach for is splitting big modules into smaller ones.
    That does not actually help here: whichever file ends up holding `launch` is
    still read whole. What the agent needs is the interface without the body, and
    that is a different tool, not a different file layout.

    Signatures and first docstring lines only. Read the body when you're changing
    it.
    """
    import ast

    p = _resolve(path)
    if not p.is_file():
        raise ToolError(f"No file at '{path}'.")
    if p.suffix != ".py":
        raise ToolError(f"'{path}' is not a Python module — use read_file.")
    try:
        tree = ast.parse(p.read_text())
    except SyntaxError as exc:
        raise ToolError(f"'{path}' does not parse: {exc}")

    out: list[str] = [f"{path} — public surface (use read_file for a body):"]
    module_doc = (ast.get_docstring(tree) or "").strip().splitlines()
    if module_doc:
        out.append(f'  """{module_doc[0]}"""')

    def summarize(node, indent: str = "  ") -> None:
        name = node.name
        if name.startswith("_") and not name.startswith("__"):
            return
        first = (ast.get_docstring(node) or "").strip().splitlines()
        note = f"  # {first[0]}" if first else ""
        if isinstance(node, ast.ClassDef):
            out.append(f"{indent}class {name}:{note}")
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    summarize(child, indent + "    ")
            return
        out.append(f"{indent}def {name}({_signature(node)}){note}")

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            summarize(node)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                # Module-level constants are interface too — SETTINGS, SERVICE_KEY,
                # METHOD are exactly what a scraper is asked about.
                if (isinstance(target, ast.Name) and target.id.isupper()
                        and not target.id.startswith("_")):
                    out.append(f"  {target.id} = ...")
    return _truncate("\n".join(out))


def _signature(node) -> str:
    args = node.args
    parts = [a.arg for a in args.posonlyargs]
    if parts:
        parts.append("/")
    parts += [a.arg for a in args.args]
    if args.vararg:
        parts.append("*" + args.vararg.arg)
    elif args.kwonlyargs:
        parts.append("*")
    parts += [a.arg for a in args.kwonlyargs]
    if args.kwarg:
        parts.append("**" + args.kwarg.arg)
    return ", ".join(parts)


def search_files(pattern: str, path: str = ".", max_results: int = 40) -> str:
    """Find `pattern` across the repo and report path:line:text.

    The cheap alternative to reading a file to find out whether it is the right
    file. A grep result is tens of characters; the read it replaces was tens of
    thousands.
    """
    import re

    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise ToolError(f"Bad search pattern: {exc}")

    root = _resolve(path)
    files = [root] if root.is_file() else sorted(
        f for f in root.rglob("*")
        if f.is_file()
        and f.suffix in SEARCHABLE_SUFFIXES
        and not any(part in SKIP_DIRS for part in f.parts)
    )

    hits: list[str] = []
    for f in files:
        try:
            for n, line in enumerate(f.read_text(errors="ignore").splitlines(), 1):
                if regex.search(line):
                    rel = f.relative_to(REPO_ROOT)
                    hits.append(f"{rel}:{n}: {line.strip()[:200]}")
                    if len(hits) >= max_results:
                        return "\n".join(hits) + f"\n[stopped at {max_results} matches]"
        except (OSError, UnicodeDecodeError):
            continue
    return "\n".join(hits) if hits else f"No match for '{pattern}' under '{path}'."


# What each file looked like the FIRST time this run overwrote it, keyed by the
# repo-relative path the agent used. The revise gates need a "before" to compare
# against, and only this function can capture one for a file nobody predicted the
# agent would touch. Reset per run by `codegen.run_codegen_gated`.
_ORIGINALS: dict[str, str] = {}


def originals() -> dict[str, str]:
    """Pre-write contents of everything overwritten since the last reset."""
    return dict(_ORIGINALS)


def forget_originals() -> None:
    _ORIGINALS.clear()


def write_file(path: str, content: str) -> str:
    p = _resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    existed = p.exists()
    # Capture BEFORE the write, and only the first time — later writes in the same
    # run are the agent iterating, and the run's "before" is what it started from.
    if existed and path not in _ORIGINALS:
        try:
            _ORIGINALS[path] = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            pass
    p.write_text(content)
    return f"{'Overwrote' if existed else 'Created'} {p.relative_to(REPO_ROOT)} ({len(content)} chars)."


def no_change_needed(reason: str) -> str:
    """The agent's way of saying the code is already right — the only honest exit
    from a revise that finds nothing wrong.

    Without it the harness deadlocks. `fold_noop` refuses any run that wrote no
    file, because an agent that re-runs a green suite and declares victory is the
    classic false pass. But a fix already applied on a previous run is a real and
    common state, and then EVERY attempt writes nothing and every attempt is
    refused, forever, with the screen saying "nothing changed" and the operator
    having no move. The retry prompt even invited "state plainly that no change is
    needed" — advice the gate could not hear, because it only counted files.

    So the escape hatch is a deliberate act with a reason attached, in the same
    spirit as `# fixed: <reason>`: cheap to do honestly, impossible to do by
    accident, and it lands in the verification where the operator reads it.
    """
    reason = (reason or "").strip()
    if len(reason) < 20:
        raise ToolError(
            "Say WHY no change is needed — name what you checked and what you found "
            "(e.g. 'the status= kwarg that crashed was already removed at line 197, "
            "and the 16 tests cover it'). A bare assertion is not a reason."
        )
    return (
        "Recorded: no change needed. The operator will see this reason and decide. "
        "Stop now — do not keep looking for something to edit."
    )


def list_directory(path: str = ".") -> str:
    p = _resolve(path)
    if not p.is_dir():
        raise ToolError(f"No directory at '{path}'.")
    entries = sorted(
        f"{child.name}/" if child.is_dir() else child.name
        for child in p.iterdir()
        if not child.name.startswith(".")
    )
    return "\n".join(entries) or "(empty)"


def read_logs(
    level: str = "error",
    limit: int = 15,
    since_minutes: int = 0,
    source_key: str = "",
) -> str:
    """The harness's own failure records, filtered to what's worth reading.

    Three things keep this small, because it is read on the turn where context is
    scarcest — the one diagnosing a failure:

    - identical records collapse to one with a count (the log holds 525 copies of
      a single HOT_RELOAD_FAILED; fifteen of them would push out the real cause);
    - the traceback goes only on the newest record, and clipped — it is the
      largest field by far and the same frames repeat down the list;
    - `since_minutes` and `source_key` narrow to the run being debugged.
    """
    from core.observability import format_record, read_relevant

    records, summary = read_relevant(
        limit=limit,
        level=(None if level == "all" else level),
        since_minutes=since_minutes or None,
        source_key=source_key or None,
    )
    if not records:
        scope = []
        if source_key:
            scope.append(f"source '{source_key}'")
        if since_minutes:
            scope.append(f"the last {since_minutes} min")
        if level and level != "all":
            scope.append(f"level '{level}'")
        return "No log records" + (" for " + ", ".join(scope) if scope else "") + "."

    lines: list[str] = []
    for i, r in enumerate(records):
        repeats = r.get("_count", 1)
        suffix = f"  [×{repeats}, last {r.get('_last_ts', '')[:19]}]" if repeats > 1 else ""
        lines.append(format_record(r) + suffix)
        if r.get("remediation"):
            lines.append(f"    remediation: {r['remediation']}")
        ctx = r.get("context")
        if ctx:
            lines.append(f"    context: {_clip_text(str(ctx), 400)}")
        # Only the newest record's traceback. The frames repeat down the list, and
        # a traceback is several times the size of everything else in a record.
        if r.get("traceback") and i == len(records) - 1:
            lines.append("    traceback (newest only):")
            lines.append(_indent(_last_frames(r["traceback"]), "      "))

    if summary["collapsed"]:
        lines.append(
            f"[{summary['matched']} matching records collapsed to {summary['distinct']} "
            f"distinct; showing {summary['shown']}. Repeats are counted above, not listed.]"
        )
    return _truncate("\n".join(lines))


def _clip_text(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"… (+{len(text) - limit} chars)"


def _indent(text: str, prefix: str) -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def _last_frames(traceback_text: str, frames: int = 6) -> str:
    """The tail of a traceback — where the error actually is. The top frames are
    the harness's own call stack and are the same on every failure."""
    lines = traceback_text.strip().splitlines()
    if len(lines) <= frames * 2:
        return traceback_text.strip()
    return "... (earlier frames omitted)\n" + "\n".join(lines[-frames * 2:])


def run_command(command: str) -> str:
    """Run a shell command from the repo root. Timeboxed; stdout+stderr are
    captured and truncated. This is how the agent tests the code it writes."""
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        raise ToolError(f"Command timed out after {COMMAND_TIMEOUT_S}s: {command}")
    out = result.stdout + (("\n[stderr]\n" + result.stderr) if result.stderr else "")
    return _truncate(f"exit={result.returncode}\n{out}".strip())


# --- Tool schemas + dispatch for the Anthropic tool-use loop -----------------

TOOL_SCHEMAS = [
    {
        "name": "read_file",
        "description": "Read a UTF-8 text file, or a slice of one. Use this to study existing "
        "code (e.g. core/parsers/buildium_owner_statement.py as a pattern) or inspect a sample. "
        "For a large file, run search_files first and read only the lines around the hit — "
        "whole-file reads crowd out everything you read afterwards.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Repo-relative path."},
                "start_line": {"type": "integer", "description": "First line (1-based). "
                               "Omit with end_line to read the whole file."},
                "end_line": {"type": "integer", "description": "Last line, inclusive."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "outline",
        "description": "List a Python module's public surface — function signatures, classes, "
        "module constants, and one line of each docstring — without the bodies. Use this to "
        "learn how to CALL something (e.g. core/tools/browser_session.py). It is a few hundred "
        "characters where the file is tens of thousands. Read the body only when changing it.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Repo-relative .py path."}},
            "required": ["path"],
        },
    },
    {
        "name": "search_files",
        "description": "Search the repo for a regular expression, returning path:line:text for "
        "each match. Far cheaper than reading files to find out which one you want — use it to "
        "locate a function, a setting, or a caller, then read_file just those lines.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Python regular expression."},
                "path": {"type": "string", "description": "Directory or file to search "
                         "(default '.'). Narrow it when you know where to look."},
                "max_results": {"type": "integer", "description": "Cap on matches (default 40)."},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "write_file",
        "description": "Create or overwrite a repository file with the given content.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Repo-relative path."},
                "content": {"type": "string", "description": "Full file contents."},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "no_change_needed",
        "description": "Declare that the code is ALREADY correct and needs no edit — for example "
        "the reported bug was fixed on an earlier run. Use this instead of writing a file you "
        "don't believe in: a revise that writes nothing is otherwise refused as a no-op. Call it "
        "once, with what you checked and what you found, then stop.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "What you checked and why it is already correct. Be specific "
                    "— cite the file and line, and say what proves it (a test, a log record).",
                }
            },
            "required": ["reason"],
        },
    },
    {
        "name": "list_directory",
        "description": "List the entries in a repository directory.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Repo-relative path (default '.')."}},
            "required": [],
        },
    },
    {
        "name": "run_command",
        "description": "Run a shell command from the repo root (e.g. 'poetry run python -c ...' to "
        "test a parser). Timeboxed; returns exit code + stdout/stderr.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "Shell command."}},
            "required": ["command"],
        },
    },
    {
        "name": "read_logs",
        "description": "Read the harness's OWN structured failure records (component, operation, "
        "code, context, cause, remediation). Use this FIRST to diagnose why something failed — the "
        "harness logs every deterministic failure here. Narrow it: pass source_key for the source "
        "you're fixing, and since_minutes to the run you care about. Identical records are "
        "collapsed with a count, so a repeat shows as '×12' instead of twelve copies.",
        "input_schema": {
            "type": "object",
            "properties": {
                "level": {"type": "string", "description": "'error' (default), 'warning', or 'all'."},
                "limit": {"type": "integer", "description": "How many distinct records (default 15)."},
                "since_minutes": {"type": "integer", "description": "Only records from the last N "
                                  "minutes. Use this to see one run instead of the whole history."},
                "source_key": {"type": "string", "description": "Only records about this source."},
            },
            "required": [],
        },
    },
]

_DISPATCH = {
    "read_file": read_file,
    "outline": outline,
    "search_files": search_files,
    "write_file": write_file,
    "list_directory": list_directory,
    "no_change_needed": no_change_needed,
    "run_command": run_command,
    "read_logs": read_logs,
}


def dispatch(name: str, arguments: dict) -> tuple[str, bool]:
    """Run a tool by name. Returns (result_text, is_error)."""
    func = _DISPATCH.get(name)
    if func is None:
        return f"Unknown tool '{name}'.", True
    try:
        return func(**arguments), False
    except ToolError as exc:
        return str(exc), True
    except Exception as exc:  # surface unexpected errors to the agent, don't crash the loop
        return f"{type(exc).__name__}: {exc}", True
