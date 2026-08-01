"""Patch-shaped edits: change the lines you mean, leave the rest byte for byte.

Ported in spirit from the OpenHands agent SDK's file editor
(`openhands-tools/openhands/tools/file_editor/editor.py`, MIT, read at d9f3e16);
the matching rules, the failure wording and the echo-a-snippet habit are theirs.
Rewritten as pure functions over text so the interesting parts are testable
without touching a disk — the I/O, path safety and repo bookkeeping stay in
`orchestration/agent_tools.py`.

**Why this exists.** The agent's only way to change a file used to be writing the
whole of it back. That asks a model to re-emit hundreds of lines perfectly to fix
one, against a token cap, and it is the single largest harness-caused source of
broken generated code. Three separate mechanisms existed to cope with the damage
— a 40%-of-lines-survived check, `fold_rewrite`, and the `_ORIGINALS` snapshot —
which is what a wrong tool looks like from the outside.

**The failure messages are load-bearing.** A match that fails has to tell the
model how to retry: which lines it matched instead, or that it matched nothing.
A bare "edit failed" produces a flailing retry, and on a small local model that
is most of a run.
"""

from __future__ import annotations

from dataclasses import dataclass

# How many lines of context to show either side of a change. Enough to see what
# moved without re-sending the file the edit exists to avoid re-sending.
CONTEXT_LINES = 4


class EditError(Exception):
    """An edit that could not be applied, with the reason the agent should read."""


@dataclass(frozen=True)
class Edit:
    """The result of an edit: the new text, and where in it to look."""

    content: str
    line: int          # 1-based, the first line the change touches
    span: int          # how many lines the new text occupies there


def _line_of(content: str, index: int) -> int:
    return content.count("\n", 0, index) + 1


def _occurrences(content: str, needle: str) -> list[int]:
    """Every start index of `needle`, matched literally.

    Plain string search rather than a regex: `old` is a chunk of source code,
    and escaping it to search for it literally is a step that can only go wrong.
    """
    found: list[int] = []
    start = content.find(needle)
    while start != -1:
        found.append(start)
        start = content.find(needle, start + 1)
    return found


def replace_once(content: str, old: str, new: str) -> Edit:
    """Replace the single occurrence of `old` with `new`.

    Refuses on zero matches and on more than one — an edit that lands in the
    wrong place, or in several places, is worse than an edit that didn't happen,
    because nothing downstream can tell it was unintended.
    """
    if not old:
        raise EditError("old_str was empty. Give the exact text you want replaced.")
    if old == new:
        raise EditError("old_str and new_str are identical, so there is nothing to change.")

    matches = _occurrences(content, old)
    if not matches:
        # Retry the MATCH with surrounding whitespace trimmed — a model that
        # copied a block out of a numbered read often brings a stray leading or
        # trailing newline with it. `new` is never stripped: it is content the
        # caller asked to write, and trimming it would silently drop indentation.
        stripped = old.strip()
        matches = _occurrences(content, stripped) if stripped else []
        if not matches:
            raise EditError(
                "old_str did not appear in the file, so nothing was changed. It has to "
                "match the file exactly — every space and every blank line. Read the "
                "lines you mean with read_file and copy them from that output.")
        old = stripped

    if len(matches) > 1:
        lines = ", ".join(str(_line_of(content, index)) for index in matches)
        raise EditError(
            f"old_str appears {len(matches)} times, on lines {lines}, so it is ambiguous "
            "and nothing was changed. Include more of the surrounding lines — three to "
            "five either side is usually enough to make it unique.")

    index = matches[0]
    updated = content[:index] + new + content[index + len(old):]
    return Edit(content=updated, line=_line_of(content, index), span=new.count("\n") + 1)


def insert_at(content: str, after_line: int, text: str) -> Edit:
    """Insert `text` after 1-based `after_line`; 0 puts it at the top of the file."""
    lines = content.splitlines(keepends=True)
    if after_line < 0 or after_line > len(lines):
        raise EditError(
            f"insert_line {after_line} is outside the file, which has {len(lines)} lines. "
            "Use 0 to insert at the top, or the number of the line to insert after.")

    addition = text if text.endswith("\n") or not text else text + "\n"
    # A file whose last line has no newline would otherwise get the insertion
    # welded onto the end of it.
    if after_line == len(lines) and lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"

    new_lines = lines[:after_line] + [addition] + lines[after_line:]
    return Edit(content="".join(new_lines), line=after_line + 1,
                span=addition.count("\n"))


def snippet(content: str, line: int, span: int, context: int = CONTEXT_LINES) -> str:
    """The changed region with line numbers, so the caller can see what it did.

    This is the other half of why patch edits are cheaper: the agent gets to
    check its own edit without a second read of the file.
    """
    lines = content.splitlines()
    first = max(1, line - context)
    last = min(len(lines), line + span - 1 + context)
    width = len(str(last))
    return "\n".join(f"{number:>{width}}\t{lines[number - 1]}"
                     for number in range(first, last + 1))
