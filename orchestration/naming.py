"""Have the model suggest what to call a new source.

Naming is the first thing the wizard asks for and the least interesting thing to
type: the operator has a statement in front of them whose issuer is printed at the
top of page one. So the harness reads it and proposes a label — the operator only
has to correct it.

Deliberately failure-tolerant: a naming suggestion is a convenience, never a gate.
Any problem (no provider, network down, a model that answers with an essay) falls
back to a title derived from the filename, and the operator can type over either.

One short completion, no tools — this is not an agent run.
"""

from __future__ import annotations

import os
import re

MAX_SAMPLE_CHARS = 2000     # the top of page one is where the issuer's name lives
MAX_LABEL_CHARS = 60

SYSTEM = (
    "You name financial data sources for a bookkeeping tool. Given the start of a "
    "statement or export, reply with ONLY the name of the institution or company "
    "that issued it, as a person would say it — for example 'Chase Business "
    "Checking' or 'Epic Property Management'. No quotes, no explanation, no more "
    "than six words. If you cannot tell, reply with exactly: UNKNOWN"
)


def label_from_filename(filename: str) -> str:
    """The fallback, and a decent one: people name their downloads sensibly."""
    stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", (filename or "").strip())
    stem = re.sub(r"[_\-]+", " ", stem)
    stem = re.sub(r"\b\d{1,4}[-/]?\d{0,2}[-/]?\d{0,4}\b", " ", stem)   # drop dates
    stem = re.sub(r"\s+", " ", stem).strip()
    return stem[:MAX_LABEL_CHARS].title() or "New source"


def _clean(text: str) -> str:
    """A local model may answer with a sentence; take the first line and strip
    the punctuation it wraps names in."""
    first = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    first = first.strip().strip('"\'`.*')
    first = re.sub(r"^(the\s+)?(source|name|label)\s*(is|:)\s*", "", first, flags=re.I)
    return first[:MAX_LABEL_CHARS].strip()


def _complete(system: str, user: str) -> str:
    """One completion from whichever provider is configured."""
    provider = (os.getenv("LLM_PROVIDER") or "anthropic").strip().lower()

    if provider in {"anthropic", "claude"}:
        import anthropic

        from orchestration.agent import DEFAULT_MODEL

        message = anthropic.Anthropic().messages.create(
            model=os.getenv("AGENT_MODEL") or DEFAULT_MODEL,
            max_tokens=64,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(b.text for b in message.content if b.type == "text")

    from orchestration.agent import (
        DEFAULT_OMLX_API_KEY,
        DEFAULT_OMLX_BASE_URL,
        DEFAULT_OMLX_MODEL,
        _openai_chat_completion,
    )

    data = _openai_chat_completion(
        os.getenv("OMLX_BASE_URL") or DEFAULT_OMLX_BASE_URL,
        os.getenv("OMLX_API_KEY", DEFAULT_OMLX_API_KEY),
        {
            "model": os.getenv("OMLX_MODEL") or DEFAULT_OMLX_MODEL,
            "max_tokens": 64,
            "temperature": 0,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        },
    )
    return data["choices"][0]["message"]["content"]


def suggest_label(sample_text: str, filename: str = "") -> dict:
    """Propose a label for a source, from the top of one of its documents.

    Returns {"label", "source"} where source is "model" or "filename", so the
    screen can be honest about where the suggestion came from.
    """
    fallback = {"label": label_from_filename(filename), "source": "filename"}
    text = (sample_text or "").strip()
    if not text:
        return fallback
    try:
        answer = _clean(_complete(SYSTEM, text[:MAX_SAMPLE_CHARS]))
    except Exception:
        return fallback
    if not answer or answer.upper().startswith("UNKNOWN"):
        return fallback
    return {"label": answer, "source": "model"}
