"""What a failed build tells the operator.

Field failure this pins, in full, as it appeared on screen:

    Build failed: 'choices'

That was `str(KeyError('choices'))` — the key, alone. It names an internal dict
key the operator has no way to connect to anything, and it reads like a
corrupted message rather than a report. It was worse than saying nothing,
because it looked like the harness had lost its mind rather than hit a bug.

Underneath it: `raw["choices"][0]` assumed the model server's response shape.
The server answered HTTP 200 — so the error handling one layer down, which only
covers non-200 statuses, never saw it — and put whatever it had to say in the
body instead.
"""

import pytest

from orchestration.agent import _first_choice
from orchestration.build_worker import describe_exception


# --- the message the operator reads -----------------------------------------

def _raised(exc_type, *args):
    """A real exception with a real traceback, so the location can be reported."""
    try:
        raise exc_type(*args)
    except exc_type as exc:
        return exc


def test_a_key_error_is_never_reported_as_just_the_key():
    """The exact failure: 'Build failed: ' + str(KeyError) == "Build failed: 'choices'"."""
    described = describe_exception(_raised(KeyError, "choices"))
    assert described != "'choices'"
    assert "KeyError" in described
    assert "choices" in described


def test_a_shape_bug_says_it_is_the_harness_at_fault():
    """The operator should not go looking for something they did wrong."""
    described = describe_exception(_raised(KeyError, "choices"))
    assert "bug in the harness" in described


def test_the_message_names_where_it_happened():
    described = describe_exception(_raised(KeyError, "choices"))
    assert "test_build_failure_messages.py:" in described


@pytest.mark.parametrize("exc_type,args", [
    (IndexError, ("list index out of range",)),
    (AttributeError, ("'NoneType' object has no attribute 'get'",)),
    (TypeError, ("'NoneType' object is not subscriptable",)),
])
def test_the_other_shape_assumptions_are_covered_too(exc_type, args):
    """Same family, same uselessly-terse `str()`, same wrong impression."""
    described = describe_exception(_raised(exc_type, *args))
    assert exc_type.__name__ in described and "bug in the harness" in described


def test_an_exception_with_no_message_still_says_something():
    described = describe_exception(_raised(RuntimeError))
    assert "RuntimeError" in described and "no message" in described


def test_an_ordinary_error_keeps_its_own_words():
    """A message written FOR the operator must not be buried in boilerplate."""
    described = describe_exception(_raised(RuntimeError, "No LLM provider is configured."))
    assert described.startswith("RuntimeError: No LLM provider is configured.")
    assert "bug in the harness" not in described


# --- the response that caused it ---------------------------------------------

def _fails(raw):
    with pytest.raises(RuntimeError) as exc:
        _first_choice(raw, "some-model", "http://localhost:10240/v1")
    return str(exc.value)


def test_a_normal_response_is_returned_untouched():
    raw = {"choices": [{"message": {"content": "hello"}}]}
    assert _first_choice(raw, "m", "u") == {"message": {"content": "hello"}}


def test_an_error_carried_in_a_200_body_is_reported_as_the_servers():
    """The likely shape of the real failure: HTTP 200, error in the body, so the
    non-200 handler one layer down never sees it."""
    text = _fails({"error": {"message": "context length 41000 exceeds 32768"}})
    assert "context length 41000 exceeds 32768" in text
    assert "from the model server, not the harness" in text


def test_a_response_with_no_choices_says_what_did_come_back():
    text = _fails({"id": "abc", "object": "chat.completion", "usage": {}})
    assert "'choices'" in text
    # The keys that WERE there — the only clue to what the server actually sent.
    assert "usage" in text or "object" in text


def test_an_empty_choices_list_is_treated_the_same_as_none():
    assert "choices" in _fails({"choices": []})


def test_a_non_json_body_says_what_type_arrived():
    """A base URL pointing at the wrong endpoint returns HTML, not a dict."""
    assert "str" in _fails("<html>404 Not Found</html>")


def test_the_model_and_url_are_named_so_the_operator_knows_which_server():
    text = _fails({})
    assert "some-model" in text and "localhost:10240" in text
