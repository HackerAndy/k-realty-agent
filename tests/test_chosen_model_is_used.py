"""Every LLM call runs the model the operator chose in Settings.

The harness talks to a model from three places — the agent loop that writes
parsers, the extractor that reads a document without one, and the naming/preview
pass on a new source. Each used to decide for itself which provider and model to
use, and the extractor's decision was a hardcoded Claude model. An operator
pointing the harness at their own local server got "check ANTHROPIC_API_KEY" for
a model they never picked; with a key present it would have quietly run a
different, billable model than the one on screen.

So: one resolver, and every caller uses it. These tests pin that at each site.
"""

import json
from pathlib import Path

import pytest

import core.tools.credential_store as cs
import core.tools.llm_extractor as extractor
import core.tools.llm_provider as lp
import orchestration.agent as agent
import orchestration.naming as naming


@pytest.fixture
def local_server(tmp_path, monkeypatch):
    """Settings say: a local OpenAI-compatible server, running qwen-30b."""
    monkeypatch.setenv("AGENT_SECRET_KEY", cs.generate_key())
    store = cs.CredentialStore(tmp_path / "creds.enc")
    monkeypatch.setattr(lp, "CredentialStore", lambda: store)
    lp.store_llm_credential("openai_compatible", base_url="http://box:9090/v1", model="qwen-30b")
    return store


@pytest.fixture
def calls(monkeypatch):
    """Capture what would have been POSTed to the local server."""
    seen = []

    def fake(base_url, api_key, payload, timeout_s=120):
        seen.append({"base_url": base_url, "payload": payload})
        return {"choices": [{"message": {"content": REPLY}}]}

    monkeypatch.setattr(lp, "chat_completion", fake)
    return seen


REPLY = json.dumps({"transactions": [
    {"date": "5/16/2026", "description": "Rent", "amount": 1200.0},
]})


# --- the extractor -----------------------------------------------------------

def test_extraction_runs_on_the_configured_local_model(local_server, calls):
    """It used to reach for anthropic.Anthropic() no matter what was configured."""
    txns, choice = extractor.extract_with_model("some statement text", "fresh", "A New Source")

    assert calls[0]["payload"]["model"] == "qwen-30b"
    assert calls[0]["base_url"] == "http://box:9090/v1"
    assert choice.model == "qwen-30b" and choice.model_source == "settings"
    assert [t.description for t in txns] == ["Rent"]


def test_a_local_model_that_chats_before_the_json_is_still_understood(local_server, monkeypatch):
    """No structured-output API on a local server — and small models like to say
    hello first. Ending the run over that would send the operator back to a
    parser build they were trying to avoid."""
    monkeypatch.setattr(lp, "chat_completion", lambda *a, **k: {
        "choices": [{"message": {"content": f"Sure! Here you go:\n```json\n{REPLY}\n```"}}]})

    txns, _ = extractor.extract_with_model("text", "fresh", "A New Source")

    assert len(txns) == 1


def test_the_anthropic_path_uses_the_chosen_model_too(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SECRET_KEY", cs.generate_key())
    store = cs.CredentialStore(tmp_path / "creds.enc")
    monkeypatch.setattr(lp, "CredentialStore", lambda: store)
    lp.store_llm_credential("anthropic", api_key="sk-test", model="claude-haiku-4-5-20251001")
    used = {}

    def fake_complete_json(choice, system, user, schema, **kwargs):
        used["model"] = choice.model
        return extractor._Extracted(
            transactions=[extractor._Row(date="5/16/2026", description="Rent", amount=1200.0)]
        )

    monkeypatch.setattr(lp, "complete_json", fake_complete_json)

    extractor.extract_with_model("text", "fresh", "A New Source")

    assert used["model"] == "claude-haiku-4-5-20251001", "not a model constant in the extractor"


def test_a_failure_names_the_model_it_actually_tried(local_server, monkeypatch):
    """'Check ANTHROPIC_API_KEY' was the wrong advice for someone running a local
    server — and the wrong advice is worse than none."""
    def boom(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(lp, "chat_completion", boom)

    with pytest.raises(extractor.ExtractionError, match="qwen-30b"):
        extractor.extract_with_model("text", "fresh", "A New Source")


def test_the_run_records_which_model_read_it(local_server, calls, tmp_path, monkeypatch):
    """Rows a model produced are traceable to the model that produced them,
    without inferring it from whatever Settings says later."""
    import core.ingest as ingest
    from core.tools.service_manifest import Service

    monkeypatch.setattr(ingest, "DATA_DIR", tmp_path / "parsed")
    monkeypatch.setattr(extractor, "read_document_text", lambda p: "text")

    class FakeManifest:
        def get(self, key):
            return Service(key=key, label="Fresh")

    run = ingest.ingest_via_llm("fresh", tmp_path / "doc.pdf", manifest=FakeManifest())

    assert "qwen-30b" in run["model"]
    assert json.loads(Path(run["run_path"]).read_text())["model"]


# --- naming / the new-source preview ----------------------------------------

def test_the_name_suggestion_comes_from_the_configured_model(local_server, monkeypatch):
    seen = {}

    def fake(base_url, api_key, payload, timeout_s=120):
        seen.update(payload)
        return {"choices": [{"message": {"content": "Chase Business Checking"}}]}

    monkeypatch.setattr(lp, "chat_completion", fake)

    result = naming.suggest_label("CHASE BUSINESS CHECKING…", "statement.pdf")

    assert seen["model"] == "qwen-30b"
    assert result == {"label": "Chase Business Checking", "source": "model"}


# --- the agent loop ----------------------------------------------------------

def test_the_agent_builds_its_client_from_the_same_choice(local_server):
    adapter = agent._build_adapter(lp.resolve())

    assert isinstance(adapter, agent.OpenAICompatibleAdapter)
    assert adapter.model == "qwen-30b"
    assert adapter.base_url == "http://box:9090/v1"


def test_an_agent_run_announces_the_model_before_it_starts(local_server, monkeypatch):
    """Transparency: the operator sees which model did the work, in the same
    stream as the files it wrote."""
    class _Adapter:
        def init_messages(self, task):
            return []

        def next_turn(self, messages, system):
            return agent.ProviderTurn(assistant_payload=[], text_blocks=["done"], tool_calls=[])

        def append_assistant(self, messages, payload):
            pass

    monkeypatch.setattr(agent, "_build_adapter", lambda choice: _Adapter())
    lines = []

    agent.run_agent("task", "system", on_event=lines.append)

    assert any("qwen-30b" in line and "model" in line for line in lines)
