"""Options a source can only learn by ASKING the portal.

The properties on an account are not knowable from the code, so a scraper has to
publish them once it has them. The harness had no way to do that, and the gap was
filled by hand, badly: the Epic scraper reached into settings._load_all(),
re-implemented the file writing including its header comment, and stored its
findings under a `properties` key that no schema declared — which save_for()
would have rejected outright, since undeclared keys are refused. Then it mutated
its own module-level SETTINGS at import time inside a bare `except: pass`, so the
schema depended on import order and on whether a scrape had ever run.

The net effect on screen: a Property dropdown with one entry and no error
anywhere. These pin the mechanism that replaces it.
"""

import pytest

from core import settings


@pytest.fixture(autouse=True)
def _isolated_settings_file(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SETTINGS_PATH", tmp_path / "source_settings.yaml")


DECLARED = [
    {"key": "lookback_days", "label": "Lookback days", "type": "number", "default": 30},
    {"key": "property_id", "label": "Property", "type": "choice", "default": "all",
     "options": [{"value": "all", "label": "All properties"}], "discovered": True},
    {"key": "basis", "label": "Basis", "type": "choice", "default": "cash",
     "options": [{"value": "cash", "label": "Cash"}, {"value": "accrual", "label": "Accrual"}]},
]


@pytest.fixture(autouse=True)
def _declaring_source(monkeypatch):
    monkeypatch.setattr(settings, "declared_for",
                        lambda key: [dict(f) for f in DECLARED] if key == "portal" else [])


def _found(*pairs):
    return [{"value": v, "label": lbl} for v, lbl in pairs]


def test_a_source_publishes_what_the_portal_offered():
    settings.record_options("portal", "property_id", _found(("7", "1029 E. Granet"),
                                                            ("9", "8095 Prospect")))

    options = next(f for f in settings.schema_for("portal") if f["key"] == "property_id")["options"]

    assert [o["label"] for o in options] == ["All properties", "1029 E. Granet", "8095 Prospect"]


def test_the_declared_catch_all_stays_first():
    """"All properties" exists before any run and must not be displaced by
    whatever order the portal happened to answer in."""
    settings.record_options("portal", "property_id", _found(("7", "One"), ("all", "Everything")))

    options = next(f for f in settings.schema_for("portal") if f["key"] == "property_id")["options"]

    assert options[0] == {"value": "all", "label": "All properties"}
    assert len(options) == 2, "the portal's duplicate 'all' didn't get added twice"


def test_recording_replaces_rather_than_accumulates():
    """A property removed from the account has to disappear from the dropdown."""
    settings.record_options("portal", "property_id", _found(("7", "One"), ("9", "Two")))
    settings.record_options("portal", "property_id", _found(("7", "One")))

    options = next(f for f in settings.schema_for("portal") if f["key"] == "property_id")["options"]

    assert [o["value"] for o in options] == ["all", "7"]


def test_a_field_that_never_declared_itself_discoverable_is_refused():
    """The basis is a fixed pair; a source filling it from the portal is a bug,
    and a silent one — the operator would see choices nothing validates."""
    with pytest.raises(settings.SettingsError, match="fixed list"):
        settings.record_options("portal", "basis", _found(("x", "X")))


def test_an_undeclared_key_is_refused_rather_than_stored():
    """Exactly what went wrong: `properties` was written to a store nothing reads
    back into the schema, so the dropdown stayed empty and nothing complained."""
    with pytest.raises(settings.SettingsError, match="declares no option"):
        settings.record_options("portal", "properties", _found(("7", "One")))


def test_a_choice_with_no_value_is_refused():
    with pytest.raises(settings.SettingsError, match="value"):
        settings.record_options("portal", "property_id", [{"label": "nameless"}])


def test_a_label_defaults_to_the_value_rather_than_showing_blank():
    settings.record_options("portal", "property_id", [{"value": "7"}])

    options = next(f for f in settings.schema_for("portal") if f["key"] == "property_id")["options"]
    assert options[-1] == {"value": "7", "label": "7"}


# --- how it sits beside the operator's own values ---------------------------

def test_a_discovered_choice_can_then_be_SAVED():
    """The point of the whole mechanism: validate() checks against the merged
    schema, so a property the portal offered is a legal thing to pick."""
    settings.record_options("portal", "property_id", _found(("9", "8095 Prospect")))

    saved = settings.save_for("portal", {"property_id": "9"})

    assert saved["property_id"] == "9"


def test_a_property_the_portal_never_offered_is_still_rejected():
    settings.record_options("portal", "property_id", _found(("9", "8095 Prospect")))

    with pytest.raises(settings.SettingsError, match="must be one of"):
        settings.save_for("portal", {"property_id": "impossible"})


def test_saving_values_does_not_forget_what_the_portal_offered():
    settings.record_options("portal", "property_id", _found(("9", "Prospect")))

    settings.save_for("portal", {"lookback_days": 60})

    assert settings.recorded_options("portal")["property_id"] == _found(("9", "Prospect"))


def test_resetting_your_choices_does_not_forget_them_either():
    """Reset means "back to the declared defaults", not "forget the account"."""
    settings.record_options("portal", "property_id", _found(("9", "Prospect")))
    settings.save_for("portal", {"property_id": "9"})

    settings.reset_for("portal")

    assert settings.values_for("portal")["property_id"] == "all"
    assert settings.recorded_options("portal")["property_id"] == _found(("9", "Prospect"))


def test_recorded_options_are_not_mixed_into_the_operator_s_values():
    """They answer different questions — what the portal offers vs what you
    chose — and merging them is what made the store un-validatable."""
    settings.record_options("portal", "property_id", _found(("9", "Prospect")))

    assert settings.stored_for("portal") == {}
    assert "properties" not in settings.values_for("portal")


def test_recording_the_same_list_again_leaves_the_file_alone():
    """Every scrape re-reads the property list; rewriting an identical file on
    each run churns mtimes for nothing."""
    settings.record_options("portal", "property_id", _found(("9", "Prospect")))
    before = settings.SETTINGS_PATH.stat().st_mtime_ns

    settings.record_options("portal", "property_id", _found(("9", "Prospect")))

    assert settings.SETTINGS_PATH.stat().st_mtime_ns == before


# --- a choice that goes away -------------------------------------------------

def test_a_selection_the_portal_no_longer_offers_is_reported():
    """Left silent, the scrape falls back to everything and the numbers change
    with no explanation. The screen has to be able to say which one went."""
    settings.record_options("portal", "property_id", _found(("9", "Prospect")))
    settings.save_for("portal", {"property_id": "9"})

    settings.record_options("portal", "property_id", _found(("7", "Granet")))

    stale = settings.stale_values("portal")
    assert len(stale) == 1
    assert stale[0]["key"] == "property_id" and stale[0]["value"] == "9"


def test_nothing_is_reported_stale_while_the_choice_still_exists():
    settings.record_options("portal", "property_id", _found(("9", "Prospect")))
    settings.save_for("portal", {"property_id": "9"})

    assert settings.stale_values("portal") == []


def test_a_source_with_nothing_recorded_still_works():
    assert settings.values_for("portal")["property_id"] == "all"
    assert settings.recorded_options("portal") == {}
    assert settings.stale_values("portal") == []


# --- what the screen is told -------------------------------------------------

def test_the_settings_tool_reports_a_choice_that_went_away(monkeypatch):
    """So the API node can say which selection stopped existing, rather than the
    next pull silently widening to everything."""
    from interfaces import mcp_tools

    settings.record_options("portal", "property_id", _found(("9", "Prospect")))
    settings.save_for("portal", {"property_id": "9"})
    settings.record_options("portal", "property_id", _found(("7", "Granet")))

    payload = mcp_tools.source_settings("portal")

    assert [f["key"] for f in payload["stale"]] == ["property_id"]


def test_the_settings_tool_offers_the_discovered_choices_to_render(monkeypatch):
    from interfaces import mcp_tools

    settings.record_options("portal", "property_id", _found(("7", "1029 E. Granet")))

    field = next(f for f in mcp_tools.source_settings("portal")["schema"]
                 if f["key"] == "property_id")

    assert [o["label"] for o in field["options"]] == ["All properties", "1029 E. Granet"]
