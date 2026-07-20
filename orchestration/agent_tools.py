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
MAX_OUTPUT_CHARS = 20000
COMMAND_TIMEOUT_S = 180


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


def read_file(path: str) -> str:
    p = _resolve(path)
    if not p.is_file():
        raise ToolError(f"No file at '{path}'.")
    return _truncate(p.read_text())


def write_file(path: str, content: str) -> str:
    p = _resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    existed = p.exists()
    p.write_text(content)
    return f"{'Overwrote' if existed else 'Created'} {p.relative_to(REPO_ROOT)} ({len(content)} chars)."


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


def read_logs(level: str = "error", limit: int = 15) -> str:
    """Read the harness's own recent structured failure records (from
    core/observability.py) — so the agent can diagnose what went wrong and fix
    it, instead of running blind. Each record carries the component/operation, a
    stable code, the context, the underlying cause, and a remediation hint."""
    from core.observability import format_record, read_recent

    records = read_recent(limit=limit, level=(None if level == "all" else level))
    if not records:
        return "No log records found."
    lines = []
    for r in records:
        lines.append(format_record(r))
        if r.get("remediation"):
            lines.append(f"    remediation: {r['remediation']}")
        ctx = r.get("context")
        if ctx:
            lines.append(f"    context: {ctx}")
    return "\n".join(lines)


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
        "description": "Read a UTF-8 text file from the repository. Use this to study existing "
        "code (e.g. core/parsers/buildium_owner_statement.py as a pattern) or inspect a sample.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Repo-relative path."}},
            "required": ["path"],
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
        "description": "Read the harness's OWN recent structured failure records (component, "
        "operation, code, context, cause, remediation). Use this to diagnose why something failed "
        "and decide how to fix it — the harness logs every deterministic failure here.",
        "input_schema": {
            "type": "object",
            "properties": {
                "level": {"type": "string", "description": "'error' (default), 'warning', or 'all'."},
                "limit": {"type": "integer", "description": "How many recent records (default 15)."},
            },
            "required": [],
        },
    },
]

_DISPATCH = {
    "read_file": read_file,
    "write_file": write_file,
    "list_directory": list_directory,
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
