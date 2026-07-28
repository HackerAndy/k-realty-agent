# Template candidate: generic (tier 1) — no client-specific logic.
# See agent-harness-template/docs/promotion-log.md.
"""Operator-adjustable options for a source, kept OUT of the code.

Nearly every portal makes you choose something before it will hand over data — a
date range, which properties, an accounting basis, which accounts. The
demonstration captures one set of those choices and the agent bakes them into the
scraper, which is fine until the operator wants a different window and their only
recourse is asking the agent to rewrite code.

So the values live here instead, in a plain config file (a database later), and
the scraper reads them at run time. Changing "last 30 days" to "last 90" becomes
a field on a screen rather than a code change, a test run, and an approval.

The part that keeps this honest: the GUI must not know a single one of Epic's
fields. A scraper DECLARES what it accepts, and the harness renders whatever it
finds:

    # in an agent-authored scraper
    SETTINGS = [
        {"key": "lookback_days", "label": "How far back to pull", "type": "number",
         "default": 30, "min": 1, "max": 365, "help": "Days before today."},
        {"key": "accounting_basis", "label": "Accounting basis", "type": "choice",
         "default": "accrual", "options": [{"value": "accrual", "label": "Accrual"},
                                           {"value": "cash", "label": "Cash"}]},
    ]

    # and at run time
    opts = settings.values_for(SERVICE_KEY)
    start = date.today() - timedelta(days=opts["lookback_days"])

Declaring the schema is the domain author's job (the agent, from the
demonstration); rendering and storing it is the harness's. That split is why the
web UI has no Buildium knowledge in it.

This module must stay framework-free (no langgraph/langchain imports).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from core.observability import get_logger

SETTINGS_PATH = Path("core/policies/source_settings.yaml")

TYPES = ("text", "number", "boolean", "choice", "date")

_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.width = 4096

log = get_logger("core.settings")


class SettingsError(RuntimeError):
    pass


def schema_for(source_key: str) -> list[dict]:
    """What options this source accepts, as declared by its own module.

    Looks at the scraper first, then the parser — a source could conceivably
    have knobs on either. Returns [] when nothing is declared, which is the
    normal case for a plain document parser and means "no options screen".
    """
    for package in ("core.scrapers", "core.parsers"):
        module = _import(f"{package}.{source_key}")
        declared = getattr(module, "SETTINGS", None) if module else None
        if declared:
            return [dict(field) for field in declared]
    return []


def _import(name: str):
    try:
        import importlib
        return importlib.import_module(name)
    except Exception:
        return None


def _load_all() -> dict[str, dict]:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        data = _yaml.load(SETTINGS_PATH.read_text()) or {}
    except Exception as exc:
        raise SettingsError(log.failure(
            operation="load_settings",
            code="SETTINGS_UNREADABLE",
            message=f"Could not read {SETTINGS_PATH}.",
            remediation="Fix the YAML by hand, or delete the file to fall back to defaults.",
            context={"path": str(SETTINGS_PATH)},
            exc=exc,
        )) from exc
    return dict(data.get("sources") or {})


def stored_for(source_key: str) -> dict:
    """Only the operator's overrides — no defaults mixed in."""
    return dict(_load_all().get(source_key) or {})


def values_for(source_key: str) -> dict:
    """The options a scrape should actually run with: declared defaults, with the
    operator's overrides on top. Safe to call when nothing is stored."""
    values = {f["key"]: f.get("default") for f in schema_for(source_key)}
    values.update(stored_for(source_key))
    return values


def validate(schema: list[dict], values: dict) -> dict:
    """Coerce and check submitted values against the schema.

    Unknown keys are rejected rather than quietly stored: a typo'd field that
    sits in the config doing nothing is worse than an error, because the operator
    believes they changed something.
    """
    by_key = {f["key"]: f for f in schema}
    unknown = [k for k in values if k not in by_key]
    if unknown:
        raise SettingsError(f"Not an option for this source: {', '.join(sorted(unknown))}.")

    cleaned: dict[str, Any] = {}
    for key, raw in values.items():
        field = by_key[key]
        kind = field.get("type", "text")
        label = field.get("label", key)
        try:
            if kind == "number":
                value = float(raw) if isinstance(raw, str) and "." in raw else int(raw)
                low, high = field.get("min"), field.get("max")
                if low is not None and value < low:
                    raise SettingsError(f"{label} must be at least {low}.")
                if high is not None and value > high:
                    raise SettingsError(f"{label} must be at most {high}.")
            elif kind == "boolean":
                value = raw if isinstance(raw, bool) else str(raw).lower() in ("1", "true", "yes", "on")
            elif kind == "choice":
                allowed = [o["value"] for o in field.get("options", [])]
                if raw not in allowed:
                    raise SettingsError(f"{label} must be one of: {', '.join(map(str, allowed))}.")
                value = raw
            else:  # text, date — stored verbatim
                value = "" if raw is None else str(raw)
        except SettingsError:
            raise
        except (TypeError, ValueError) as exc:
            raise SettingsError(f"{label}: '{raw}' isn't a valid {kind}.") from exc
        cleaned[key] = value
    return cleaned


def save_for(source_key: str, values: dict) -> dict:
    """Store the operator's overrides. Returns the full effective values."""
    schema = schema_for(source_key)
    if not schema:
        raise SettingsError(f"'{source_key}' declares no adjustable options.")

    cleaned = validate(schema, values)
    data = _load_all()
    data[source_key] = {**data.get(source_key, {}), **cleaned}

    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    from io import StringIO
    buf = StringIO()
    _yaml.dump({"sources": data}, buf)
    SETTINGS_PATH.write_text(
        "# Operator-adjustable options per source — edited from the app, not by hand.\n"
        "# The available fields are declared by each source's own module (SETTINGS);\n"
        "# see core/settings.py. Values here override those declared defaults.\n"
        + buf.getvalue()
    )

    log.event(
        operation="save_settings",
        code="SETTINGS_SAVED",
        message=f"Updated options for '{source_key}'.",
        context={"source_key": source_key, "keys": sorted(cleaned)},
    )
    return values_for(source_key)


def reset_for(source_key: str) -> dict:
    """Drop the overrides and go back to what the source declares."""
    data = _load_all()
    data.pop(source_key, None)
    if SETTINGS_PATH.exists():
        from io import StringIO
        buf = StringIO()
        _yaml.dump({"sources": data}, buf)
        SETTINGS_PATH.write_text(buf.getvalue())
    return values_for(source_key)
