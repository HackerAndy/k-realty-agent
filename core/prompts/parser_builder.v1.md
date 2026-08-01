# Parser-builder agent — BUILD a new parser (v1)

Loaded after `parser_contract.v1.md`, which says what a parser must be. This says
how to write one that doesn't exist yet.

## What to do

1. **Read the sample document you were given.** It is the source of truth for
   every column name and every value your parser must preserve.

2. **Study the pattern.** `core/parsers/base.py` (the `Parser` contract +
   `ParseError`) and `core/parsers/buildium_owner_statement.py` (a complete,
   working example — match its structure and rigor) are worth reading in full.
   For anything else, `outline` tells you how to call it.

3. **Write `core/parsers/<source_key>.py`** to the contract above. Factor the
   row-level work into a pure helper you can test without the document on disk.

4. **Register it.** Edit `core/parsers/__init__.py` to import your `parse`
   function and add it to `REGISTRY` under the exact source key.

5. **Write and run the test** the contract requires. Also do a quick sanity run
   of the parser against the real sample document with `run_command`. Iterate
   until it passes.

## When done

Report concisely: the parser file you wrote, the test file you wrote and that it
passes (with reconciliation numbers if the document had totals), and anything the
human reviewer should double-check.
