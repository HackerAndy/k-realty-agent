"""Build and revise get different instructions — and neither loses the contract.

The prompts were one file per kind, sent to both jobs. Most of the build prompt
is how to read a demonstration, which is dead weight on a revise — the
demonstration isn't even the source of truth there, since the failure being fixed
happened after it was recorded.

Splitting them creates the risk this file exists to prevent: an invariant that
lives in only one half, so the agent is told about it when building and not when
fixing (or the reverse). Everything a scraper or parser must ALWAYS satisfy
belongs in the contract, which both halves are built on.
"""

import pytest

from orchestration import build_parser, build_scraper


SCRAPER_JOBS = [build_scraper.SYSTEM_PROMPT_PATH, build_scraper.REVISE_PROMPT_PATH]
PARSER_JOBS = [build_parser.SYSTEM_PROMPT_PATH, build_parser.REVISE_PROMPT_PATH]


def _scraper(job):
    return build_scraper._system(job)


def _parser(job):
    return build_parser._system(job)


# --- the contract reaches both halves ---------------------------------------

@pytest.mark.parametrize("job", SCRAPER_JOBS)
@pytest.mark.parametrize("invariant", [
    "record_options",           # the only sanctioned way to publish discovered options
    "must actually reach the request",
    "invent nothing",
    "REQUIRED, not optional",   # the test gate
    "services.yaml",            # the approval gate it must not touch
    "check_portability",
])
def test_every_scraper_invariant_reaches_both_jobs(job, invariant):
    assert invariant in _scraper(job)


@pytest.mark.parametrize("job", PARSER_JOBS)
@pytest.mark.parametrize("invariant", [
    "verbatim",
    "Invent nothing",
    "REQUIRED, not optional",
    "services.yaml",
    "check_portability",
])
def test_every_parser_invariant_reaches_both_jobs(job, invariant):
    assert invariant in _parser(job)


# --- and the halves really are different ------------------------------------

def test_the_revise_prompt_does_not_carry_the_demonstration_guide():
    """The bulk of the build prompt, and irrelevant when fixing a live failure."""
    revise = _scraper(build_scraper.REVISE_PROMPT_PATH)
    assert "candidate_requests" not in revise
    assert "recorded_actions" not in revise


def test_the_scraper_revise_prompt_is_smaller_than_its_build_prompt():
    """Only asserted for scrapers, and deliberately.

    The scraper build prompt is mostly the demonstration guide, so dropping it is
    a real saving. The PARSER build prompt was already short — its reviser comes
    out slightly larger, because debugging guidance is genuinely more to say than
    "read the sample and write the parser". Splitting it was worth doing for
    correctness (revise-specific instructions, the no-change hatch, the current
    tool list), not for size, and an assertion here would only invite padding one
    file to satisfy it."""
    assert len(_scraper(build_scraper.REVISE_PROMPT_PATH)) < len(
        _scraper(build_scraper.SYSTEM_PROMPT_PATH))


@pytest.mark.parametrize("job", SCRAPER_JOBS + PARSER_JOBS)
def test_no_prompt_advertises_a_tool_that_does_not_exist(job):
    """A prompt naming a tool the agent hasn't got sends it down a dead end."""
    from orchestration import agent_tools

    real = {schema["name"] for schema in agent_tools.TOOL_SCHEMAS}
    text = job.read_text()
    for claimed in ("outline", "search_files", "read_file", "write_file",
                    "run_command", "read_logs", "list_directory", "no_change_needed"):
        if f"`{claimed}`" in text:
            assert claimed in real


@pytest.mark.parametrize("job", [build_scraper.REVISE_PROMPT_PATH,
                                 build_parser.REVISE_PROMPT_PATH])
def test_only_the_revise_prompts_offer_the_no_change_escape_hatch(job):
    """Build writes a file by definition; the hatch is a revise-only move."""
    assert "no_change_needed" in job.read_text()
