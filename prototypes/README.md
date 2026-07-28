# THROWAWAY — the add-a-data-source prototype

Recovered from the session transcript after being deleted. Kept on this branch,
out of main, so the agreed design survives the next context compaction.

- `add_source_v1.html` — first pass, three variants of the question order.
- `add_source_v2_chosen.html` — **variant A, revised and chosen**: the agent
  suggests the name (so naming moves to the END, after it has learned the
  source), and there is no URL field.

Verdict it settled: three steps — *How it arrives › Let the agent learn it ›
Name it and save* — with "what the agent found" shown before you name it, and a
fork ("a new source, or another way into one you have?") asked after the learn
step.

## Verdict (28 Jul 2026)

**Variant B — three doors, then a stepper — wins**, with one change the operator
asked for: **clicking an option advances**. No radio, no Next button; the box is
the button. A radio plus a Next is two clicks to say one thing, and step 1 has
nothing to review before committing.

Steps: *How it arrives › Let it learn the source › Check it and name it.*

Non-negotiables carried through from the earlier round:
- the agent suggests the name, shown only AFTER it has read the source;
- no URL field — a website is captured from a demonstration;
- the fork ("a new source, or another way into one you have?") is asked at the
  end, when the operator can actually answer it.
