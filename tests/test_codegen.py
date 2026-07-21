"""The universal code-gen standards + untested-code safety net.

This is the harness's own quality gate, so it gets its own test.
"""

from orchestration.codegen import CODE_STANDARDS, fold_untested, untested_code_files


def _writes(*paths):
    return [("write_file", {"path": p}) for p in paths]


def test_standards_require_a_test():
    assert "SELF-CONTAINED TEST" in CODE_STANDARDS
    assert "read_logs" in CODE_STANDARDS


def test_code_without_a_test_is_flagged():
    calls = _writes("core/scrapers/foo.py", "core/scrapers/__init__.py")
    assert untested_code_files(calls) == ["core/scrapers/foo.py"]


def test_code_with_a_test_is_clean():
    calls = _writes("core/parsers/foo.py", "tests/test_parser_foo.py")
    assert untested_code_files(calls) == []


def test_init_and_config_writes_are_not_code():
    # registering in __init__ or writing a prompt shouldn't count as untested code
    calls = _writes("core/parsers/__init__.py", "core/prompts/x.md")
    assert untested_code_files(calls) == []


def test_fold_untested_marks_verification_not_ok():
    v = fold_untested({"ok": True}, _writes("core/scrapers/foo.py"))
    assert v["ok"] is False and v["untested_code"] == ["core/scrapers/foo.py"]

    clean = fold_untested({"ok": True}, _writes("core/scrapers/foo.py", "tests/test_scraper_foo.py"))
    assert clean["ok"] is True and "untested_code" not in clean
