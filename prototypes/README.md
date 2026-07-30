# Prototypes — throwaway, kept as primary sources

Not shipped, not imported by anything. Each file answers one design question, and
the answer is written down here once it's settled. A summary of a prototype loses
the thing that made it useful (step order, what appears when), so the artifact
stays on this branch rather than being deleted.

Run any of them:

```bash
python3 -m http.server 8790 --bind 127.0.0.1 --directory prototypes
```

---

## `source_graph.html` — is acquiring the data a separate stage from reading it?

**Branch:** `proto/source-graph` · **date:** 2026-07-29

**The question.** Andy: *"up to this point we've assumed the file ingestion and
parsing is all a part of the same process, though I've mentioned we need to start
treating them as entirely separate actions and processes. Therefore, we need to
add a whole new section in the 'graph': File upload should point to 'parser'
which then points to '# of transactions'."*

It came out of a smaller complaint: a source's panel showed the email settings
permanently, between the route chips and the data, and clicking a chip appeared to
do nothing. The desired behaviour he stated: chips, then the data, nothing in
between — and only when a route is selected does its settings appear there. If the
selected route isn't the one that produced the data on screen, the data clears
(and comes back when he re-selects the route that did produce it).

**Three variants**, `?variant=a|b|c`:

- **A — one drawing, two hops.** The existing funnel extended: doors → parser →
  count, one picture. The scrape line visibly skips the parser.
- **B — three labelled stages.** Stacked bands: `1 How it arrives` / `2 How it's
  read` / `3 What came out`. States the separation in words.
- **C — two panels in series.** Acquire and Read as sibling panels, each with its
  own ⏵. Makes the functional consequence obvious: re-read the document already on
  disk without fetching it again.

**Assumption made visible in all three (unconfirmed).** A portal scrape needs no
parser — the scraper returns rows itself — so the parser stage has two inputs
(upload, email), not three.

**Open questions the prototype raised, not yet answered.**

1. Variant C still shows the `14 transactions` count while the data below is
   cleared. Should the count blank out too, or is it a property of the source
   rather than of the selected route?
2. Where the agent's build/fix panel belongs once the parser has a node of its
   own — inside the parser's settings, or below the data.
3. Whether `★ Can run itself — email` survives "nothing between the chips and the
   data".

**Verdict:** _(awaiting Andy)_
