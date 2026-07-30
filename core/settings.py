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

Some choices can only be known by ASKING the portal — the list of properties on
an account, the accounts in a ledger. A source declares such a field with
`"discovered": True` and publishes what it found during a run:

    # in SETTINGS
    {"key": "property_id", "label": "Property", "type": "choice", "default": "all",
     "options": [{"value": "all", "label": "All properties"}], "discovered": True}

    # during retrieve(), once the portal has answered
    settings.record_options(SERVICE_KEY, "property_id",
                            [{"value": p["id"], "label": p["name"]} for p in props])

That exists because its absence was filled in by hand, badly: a scraper reached
into this module's private loader, re-implemented the file writing, and stored
its findings under a key no schema declared — which save_for() would have
rejected, since undeclared keys are refused. Recorded options are kept apart from
the operator's values on purpose: one is what the portal offers, the other is
what the operator chose, and merging them loses the difference.

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


def declared_for(source_key: str) -> list[dict]:
    """What options this source's own module declares, verbatim.

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


def schema_for(source_key: str) -> list[dict]:
    """What options this source accepts — its declaration, plus whatever its last
    run learned from the portal.

    Merged here rather than by mutating the module's SETTINGS at import time,
    which is what happened before: a source rewrote its own declaration as a
    side effect of being imported, so the schema depended on import order and on
    whether a scrape had ever run.
    """
    fields = declared_for(source_key)
    if not fields:
        return []
    found = recorded_options(source_key)
    for field in fields:
        extra = found.get(field["key"])
        if not extra:
            continue
        # Declared options first: they are the ones that exist before any run
        # (an "All properties" catch-all), and they must not be displaced by
        # whatever the portal happened to return.
        seen = {o.get("value") for o in field.get("options") or []}
        field["options"] = list(field.get("options") or []) + [
            o for o in extra if o.get("value") not in seen
        ]
    return fields


def _import(name: str):
    try:
        import importlib
        return importlib.import_module(name)
    except Exception:
        return None


def _read_file() -> dict:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        return _yaml.load(SETTINGS_PATH.read_text()) or {}
    except Exception as exc:
        raise SettingsError(log.failure(
            operation="load_settings",
            code="SETTINGS_UNREADABLE",
            message=f"Could not read {SETTINGS_PATH}.",
            remediation="Fix the YAML by hand, or delete the file to fall back to defaults.",
            context={"path": str(SETTINGS_PATH)},
            exc=exc,
        )) from exc


def _load_all() -> dict[str, dict]:
    return dict(_read_file().get("sources") or {})


def _write(sources: dict, discovered: dict) -> None:
    """One writer for the file, so nothing has to re-implement its shape.

    A scraper doing its own dump is how the header comment ended up duplicated
    in a domain module, and how a section that no schema declares ended up in
    the operator's values.
    """
    from io import StringIO

    payload: dict = {"sources": sources}
    if discovered:
        payload["discovered"] = discovered
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    buf = StringIO()
    _yaml.dump(payload, buf)
    SETTINGS_PATH.write_text(
        "# Operator-adjustable options per source — edited from the app, not by hand.\n"
        "# The available fields are declared by each source's own module (SETTINGS);\n"
        "# see core/settings.py. Values here override those declared defaults.\n"
        "#\n"
        "# `discovered` holds choices a source learned from its portal on the last\n"
        "# run (the properties on the account, say). Written by the harness, not by\n"
        "# you, and kept apart from your values on purpose: one is what the portal\n"
        "# offers, the other is what you chose.\n"
        + buf.getvalue()
    )


def stored_for(source_key: str) -> dict:
    """Only the operator's overrides — no defaults mixed in."""
    return dict(_load_all().get(source_key) or {})


def recorded_options(source_key: str) -> dict[str, list[dict]]:
    """Choices this source last learned from its portal, by field key."""
    found = _read_file().get("discovered") or {}
    entry = found.get(source_key) or {}
    return {key: [dict(o) for o in (options or [])] for key, options in entry.items()}


def record_options(source_key: str, field_key: str, options: list[dict]) -> list[dict]:
    """Publish choices this source just learned from its portal.

    Only a field that DECLARED itself discoverable may be filled in this way. A
    source quietly inventing a key it never declared is the thing that broke
    before — it lands in a store nothing renders and nothing validates, so the
    operator sees a dropdown that stays empty and no error anywhere.
    """
    field = next((f for f in declared_for(source_key) if f.get("key") == field_key), None)
    if field is None:
        raise SettingsError(
            f"'{source_key}' declares no option called '{field_key}', so there is nothing "
            f"to record choices for. Add it to that module's SETTINGS first."
        )
    if not field.get("discovered"):
        raise SettingsError(
            f"'{field_key}' is a fixed list of choices. Mark it \"discovered\": True in "
            f"SETTINGS if its options really do come from the portal."
        )

    clean: list[dict] = []
    for option in options or []:
        if not isinstance(option, dict) or "value" not in option:
            raise SettingsError(
                f"Each recorded choice needs a 'value' (and ideally a 'label'); got {option!r}."
            )
        clean.append({"value": str(option["value"]),
                      "label": str(option.get("label") or option["value"])})

    data = _read_file()
    discovered = dict(data.get("discovered") or {})
    entry = dict(discovered.get(source_key) or {})
    if entry.get(field_key) == clean:
        return clean                     # nothing new; leave the file alone
    entry[field_key] = clean
    discovered[source_key] = entry
    _write(dict(data.get("sources") or {}), discovered)

    log.event(
        operation="record_options",
        code="OPTIONS_RECORDED",
        message=f"{source_key}.{field_key}: {len(clean)} choices offered by the portal.",
        context={"source_key": source_key, "field": field_key, "count": len(clean)},
    )
    return clean


def stale_values(source_key: str) -> list[dict]:
    """Stored choices that the portal no longer offers.

    A property gets removed from the account and the operator's saved selection
    silently stops matching anything. Left alone, the scrape falls back to
    "everything" and the numbers change with no explanation — so this is what the
    screen needs in order to say which choice went away.
    """
    stored = stored_for(source_key)
    gone: list[dict] = []
    for field in schema_for(source_key):
        if field.get("type") != "choice" or field["key"] not in stored:
            continue
        allowed = [o.get("value") for o in field.get("options") or []]
        value = stored[field["key"]]
        if allowed and value not in allowed:
            gone.append({"key": field["key"], "label": field.get("label", field["key"]),
                         "value": value})
    return gone


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
    file_data = _read_file()
    data = dict(file_data.get("sources") or {})
    data[source_key] = {**data.get(source_key, {}), **cleaned}
    _write(data, dict(file_data.get("discovered") or {}))

    log.event(
        operation="save_settings",
        code="SETTINGS_SAVED",
        message=f"Updated options for '{source_key}'.",
        context={"source_key": source_key, "keys": sorted(cleaned)},
    )
    return values_for(source_key)


def reset_for(source_key: str) -> dict:
    """Drop the overrides and go back to what the source declares.

    What the PORTAL offers is not an override, so it survives: resetting your
    choices should not also forget the list of properties to choose from.
    """
    file_data = _read_file()
    data = dict(file_data.get("sources") or {})
    data.pop(source_key, None)
    if SETTINGS_PATH.exists():
        _write(data, dict(file_data.get("discovered") or {}))
    return values_for(source_key)
