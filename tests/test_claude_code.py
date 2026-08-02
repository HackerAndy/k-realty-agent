"""Handing the job to the Claude Code CLI instead of driving the loop ourselves.

Claude Code is a whole agent, not a model endpoint, so it is an alternative
EXECUTOR rather than another adapter: it edits the working tree and every gate
downstream reads the working tree. The only real integration is vocabulary.

That translation is where a silent failure would live. Every gate decides what
the agent did by reading its tool calls back — a write tool that does not map
into `agent_tools.WRITE_TOOLS` makes the whole build look like a no-op: no test
required, no coverage checked, and "files written: none" on the screen for a run
that rewrote three files. Nothing fails loudly when that happens, which is why
most of this file is about names and paths.
"""

from __future__ import annotations

import json

import pytest

from orchestration import agent_tools, codegen, claude_code
from orchestration.claude_code import TOOL_NAMES, _translate, run_claude_code


# --- vocabulary --------------------------------------------------------------

@pytest.mark.parametrize("theirs,ours", [
    ("Write", "write_file"),
    ("Edit", "str_replace"),
    ("MultiEdit", "str_replace"),
    ("NotebookEdit", "write_file"),
])
def test_every_editing_tool_maps_to_one_the_gates_count(theirs, ours):
    name, _ = _translate(theirs, {"file_path": "/x/core/parsers/a.py"})

    assert name == ours
    assert name in agent_tools.WRITE_TOOLS, "or the build silently scores as a no-op"


def test_the_write_tools_are_a_complete_mapping():
    """A tool of theirs that edits files and is missing here is invisible to
    every gate at once. Listed explicitly so adding one is a decision."""
    editing = {t for t, ours in TOOL_NAMES.items() if ours in agent_tools.WRITE_TOOLS}

    assert editing == {"Write", "Edit", "MultiEdit", "NotebookEdit"}


def test_commands_and_reads_map_too():
    assert _translate("Bash", {"command": "pytest -q"}) == ("run_command", {"command": "pytest -q"})
    assert _translate("Read", {"file_path": "/x/a.py"})[0] == "read_file"
    assert _translate("Grep", {"pattern": "x"})[0] == "search_files"


def test_an_unknown_tool_passes_through_without_pretending_to_be_a_write():
    name, _ = _translate("WebFetch", {"url": "https://example.com"})

    assert name == "WebFetch"
    assert name not in agent_tools.WRITE_TOOLS


def test_paths_come_back_repo_relative(monkeypatch, tmp_path):
    """Claude Code reports absolute paths; every gate matches repo-relative ones,
    and a gate that matches nothing is indistinguishable from a clean build."""
    monkeypatch.setattr(claude_code, "REPO_ROOT", tmp_path)
    target = tmp_path / "core" / "parsers" / "acme.py"
    target.parent.mkdir(parents=True)
    target.write_text("x = 1")

    _, arguments = _translate("Write", {"file_path": str(target)})

    assert arguments["path"] == "core/parsers/acme.py"


def test_a_path_outside_the_repo_is_left_alone_rather_than_mangled(monkeypatch, tmp_path):
    monkeypatch.setattr(claude_code, "REPO_ROOT", tmp_path)

    _, arguments = _translate("Write", {"file_path": "/etc/hosts"})

    assert arguments["path"] == "/etc/hosts"


# --- the run -----------------------------------------------------------------

def _stream(*events: dict) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\n"


class _FakeProcess:
    def __init__(self, text, returncode=0):
        self.stdout = iter(text.splitlines(keepends=True))
        self.returncode = returncode

    def wait(self):
        return self.returncode


@pytest.fixture
def cli(monkeypatch):
    """Stand in for the CLI, capturing the command it would have been given."""
    captured: dict = {}

    def spawn(text, returncode=0):
        def fake_popen(command, **kwargs):
            captured["command"] = command
            captured["env"] = kwargs.get("env") or {}
            captured["cwd"] = kwargs.get("cwd")
            return _FakeProcess(text, returncode)
        monkeypatch.setattr(claude_code.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(claude_code, "available", lambda: True)
        return captured

    return spawn


def _assistant(*blocks):
    return {"type": "assistant", "message": {"content": list(blocks)}}


def test_a_run_reports_its_edits_in_the_harness_vocabulary(cli, monkeypatch, tmp_path):
    monkeypatch.setattr(claude_code, "REPO_ROOT", tmp_path)
    cli(_stream(
        _assistant({"type": "text", "text": "Writing the parser."},
                   {"type": "tool_use", "name": "Write",
                    "input": {"file_path": str(tmp_path / "core/parsers/acme.py")}}),
        _assistant({"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}}),
        {"type": "result", "subtype": "success", "is_error": False,
         "num_turns": 7, "total_cost_usd": 0.42, "result": "done"},
    ))

    result = run_claude_code("build it", "CONTRACT", on_event=lambda _: None,
                             provider="claude_code")

    assert result.turns == 7 and result.final_text == "done"
    assert codegen.files_written(result.tool_calls) == ["core/parsers/acme.py"]
    assert ("run_command", {"command": "pytest -q"}) in result.tool_calls


def test_the_untested_code_gate_still_fires_on_a_cli_run(cli, monkeypatch, tmp_path):
    """The whole point of translating: the gates work unchanged."""
    monkeypatch.setattr(claude_code, "REPO_ROOT", tmp_path)
    cli(_stream(
        _assistant({"type": "tool_use", "name": "Write",
                    "input": {"file_path": str(tmp_path / "core/parsers/acme.py")}}),
        {"type": "result", "subtype": "success", "is_error": False, "num_turns": 2},
    ))

    result = run_claude_code("build it", "CONTRACT", on_event=lambda _: None,
                             provider="claude_code")

    assert codegen.untested_code_files(result.tool_calls) == ["core/parsers/acme.py"]


def test_the_contract_is_appended_not_substituted(cli):
    """Replacing Claude Code's prompt would discard the thing we invoked it for."""
    captured = cli(_stream({"type": "result", "subtype": "success", "num_turns": 1}))

    run_claude_code("build it", "CONTRACT TEXT", on_event=lambda _: None,
                    provider="claude_code")

    assert "--append-system-prompt" in captured["command"]
    assert "CONTRACT TEXT" in captured["command"]
    assert "--system-prompt" not in captured["command"]


def test_the_model_comes_from_the_resolver(cli):
    captured = cli(_stream({"type": "result", "subtype": "success", "num_turns": 1}))

    run_claude_code("t", "s", on_event=lambda _: None, provider="claude_code", model="opus")

    assert captured["command"][captured["command"].index("--model") + 1] == "opus"


def test_no_stored_key_means_the_cli_uses_its_own_login(cli, monkeypatch):
    """The normal case, and why this provider needs no credential."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(claude_code.llm_provider, "resolve",
                        lambda **kw: claude_code.llm_provider.LLMChoice(
                            provider="claude_code", model="sonnet", api_key=None))
    captured = cli(_stream({"type": "result", "subtype": "success", "num_turns": 1}))

    run_claude_code("t", "s", on_event=lambda _: None)

    assert "ANTHROPIC_API_KEY" not in captured["env"]


def test_a_stored_key_is_handed_to_the_subprocess(cli, monkeypatch):
    monkeypatch.setattr(claude_code.llm_provider, "resolve",
                        lambda **kw: claude_code.llm_provider.LLMChoice(
                            provider="claude_code", model="sonnet", api_key="sk-test"))
    captured = cli(_stream({"type": "result", "subtype": "success", "num_turns": 1}))

    run_claude_code("t", "s", on_event=lambda _: None)

    assert captured["env"]["ANTHROPIC_API_KEY"] == "sk-test"


def test_an_errored_run_says_so_rather_than_looking_finished(cli):
    cli(_stream({"type": "result", "subtype": "error_max_turns", "is_error": True,
                 "num_turns": 40}))

    result = run_claude_code("t", "s", on_event=lambda _: None, provider="claude_code")

    assert "error" in result.stopped_reason and "error_max_turns" in result.stopped_reason


def test_a_nonzero_exit_is_reported(cli):
    cli(_stream({"type": "system", "subtype": "init"}), returncode=1)

    result = run_claude_code("t", "s", on_event=lambda _: None, provider="claude_code")

    assert "status 1" in result.stopped_reason


def test_plain_diagnostics_are_not_swallowed(cli):
    """Losing the CLI's own output is how 'it just failed' happens."""
    seen: list[str] = []
    cli("not json at all\n" + _stream({"type": "result", "subtype": "success", "num_turns": 1}))

    run_claude_code("t", "s", on_event=seen.append, provider="claude_code")

    assert any("not json at all" in line for line in seen)


def test_a_missing_cli_says_what_to_do(monkeypatch):
    monkeypatch.setattr(claude_code, "available", lambda: False)

    with pytest.raises(RuntimeError, match="not on PATH"):
        run_claude_code("t", "s", on_event=lambda _: None, provider="claude_code")


# --- choosing it -------------------------------------------------------------

def test_settings_decides_which_executor_runs(monkeypatch):
    """One answer to "what is this harness running", same as every model choice."""
    from orchestration.agent import run_agent

    monkeypatch.setattr(codegen.llm_provider, "resolve",
                        lambda **kw: codegen.llm_provider.LLMChoice(
                            provider="claude_code", model="sonnet"))
    assert codegen._executor() is run_claude_code

    monkeypatch.setattr(codegen.llm_provider, "resolve",
                        lambda **kw: codegen.llm_provider.LLMChoice(
                            provider="openai_compatible", model="qwen"))
    assert codegen._executor() is run_agent


# --- how it appears in Settings ----------------------------------------------

def test_the_screen_says_the_data_still_leaves_this_machine():
    """The command runs locally, which must not read as "the data stays here"."""
    from interfaces import mcp_tools

    cfg = {"provider": "claude_code", "model": "sonnet"}

    assert mcp_tools._llm_is_offsite(cfg) is True
    assert "Anthropic API" in mcp_tools._llm_destination(cfg)
    assert "on this machine" in mcp_tools._llm_destination(cfg)


def test_choosing_it_needs_no_api_key(monkeypatch, tmp_path):
    """The CLI has its own login; demanding a key would be a fiction."""
    from interfaces import mcp_tools

    saved: dict = {}
    monkeypatch.setattr(mcp_tools.llm_provider, "store_llm_credential",
                        lambda provider, **kw: saved.update(provider=provider, **kw))
    monkeypatch.setattr(mcp_tools.llm_provider, "load_into_env", lambda: True)
    monkeypatch.setattr(mcp_tools.llm_provider, "stored_api_key", lambda p=None: None)
    monkeypatch.setattr(mcp_tools, "llm_status", lambda: {})
    monkeypatch.setattr(claude_code, "available", lambda: True)

    mcp_tools.set_llm_provider("claude_code", model="opus")

    assert saved["provider"] == "claude_code" and saved["model"] == "opus"
    assert saved["api_key"] is None
    assert saved["base_url"] is None, "it is not an endpoint; storing a URL would be a fiction"


def test_choosing_it_without_the_cli_installed_is_refused(monkeypatch):
    from interfaces import mcp_tools

    monkeypatch.setattr(mcp_tools.llm_provider, "stored_api_key", lambda p=None: None)
    monkeypatch.setattr(claude_code, "available", lambda: False)

    with pytest.raises(mcp_tools.ToolError, match="isn't on this machine's PATH"):
        mcp_tools.set_llm_provider("claude_code", model="sonnet")


# --- whose bill --------------------------------------------------------------
#
# The CLI signed in to a Claude subscription draws on that plan. Hand it an API
# key and it bills per token instead. That makes the presence of a key a billing
# decision, and it must be the operator's, not a side effect.

def test_an_ambient_api_key_does_not_move_the_operator_off_their_subscription(cli, monkeypatch):
    """load_into_env() exports ANTHROPIC_API_KEY whenever the anthropic provider
    has ever been configured. Inheriting it here would switch the CLI to
    per-token billing because of a setting made for a different provider."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-some-other-provider")
    monkeypatch.setattr(claude_code.llm_provider, "resolve",
                        lambda **kw: claude_code.llm_provider.LLMChoice(
                            provider="claude_code", model="sonnet", api_key=None))
    captured = cli(_stream({"type": "result", "subtype": "success", "num_turns": 1}))

    run_claude_code("t", "s", on_event=lambda _: None)

    assert "ANTHROPIC_API_KEY" not in captured["env"]
    assert "ANTHROPIC_AUTH_TOKEN" not in captured["env"]


def test_the_resolver_does_not_invent_a_key_from_the_environment(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ambient")
    monkeypatch.setattr(claude_code.llm_provider, "_stored", lambda: {})

    choice = claude_code.llm_provider.resolve(provider="claude_code")

    assert choice.api_key is None, "blank in Settings must mean the CLI's own login"
