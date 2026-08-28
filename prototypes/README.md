# Prototypes — throwaway, kept as primary sources

Not shipped, not imported by anything. Each file answers one design question, and
the answer is written down here once it's settled. A summary of a prototype loses
the thing that made it useful (step order, what appears when), so the artifact
stays here rather than being deleted.

Run any of them:

```bash
python3 -m http.server 8790 --bind 127.0.0.1 --directory prototypes
```

---

## `add_source_v1/v2/v3.html` — the add-a-data-source flow

Recovered from the session transcript after being deleted; kept here so the
agreed design survives the next context compaction.

- `add_source_v1.html` — first pass, three variants of the question order.
- `add_source_v2_chosen.html` — **variant A, revised and chosen**: the agent
  suggests the name (so naming moves to the END, after it has learned the
  source), and there is no URL field.
- `add_source_v3_fresh.html` — fresh pass, four variants (A artifact-first, B
  stepper, C conversation, D row-first).

Verdict it settled: three steps — *How it arrives › Let the agent learn it ›
Name it and save* — with "what the agent found" shown before you name it, and a
fork ("a new source, or another way into one you have?") asked after the learn
step.

**VERDICT — 28 Jul 2026.**

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

**VERDICT — Andy, 2026-07-29: variant A, refined.**

- **A** is the shape. It now draws **three ways in → two kinds of reading → the rows**.
- A website is a way IN, not a reader. So the portal route is two nodes: a
  **Website** node (address, username, password — the same sign-in as Settings →
  Websites, editable in both places) pointing at an **API call** node.
- It really is an API call, not replayed clicks: `core/scrapers/epic_property_management.py`
  logs in for cookies + the XSRF token, then POSTs `/manager/api/generalLedger/transactions`.
  So the node is named for what it is. A source whose agent-built code drives the UI
  instead would presumably read `Scraper`.
- **The agent's fix panel belongs on the reading node, and only there** — Andy:
  *"when is an agent needed? it's needed when a parsing action needs to be fixed."*
  The Parser node offers "Have the agent fix it"; the API node offers the same plus
  the variables the call needs.
- `★ Can run itself — email` **survives**, directly under the graph.
- Reader nodes get their own ⏵: re-read the document already on disk, or call the
  API again with the session we have — neither re-fetches.

**What building it will expose.** The API node's "what the call needs" panel has
nothing to edit yet: the date window is hardcoded (`date.today() - timedelta(days=30)`
in the scraper) and Epic declares no `SETTINGS`. That is issue #43, and this design
is what makes it visible — the panel would otherwise be an empty box.

**Answered, 2026-07-29:** the count belongs to the RUN, not the source. Select a
route that didn't produce the rows and the count reads **"Not run · by this route"**,
with the table replaced by one line naming the route that did produce them.

**New question this raises (unresolved).** Andy qualified it: *"when you select a
route that didn't produce the rows **and there is no previous run to show**"* — which
implies that if that route HAS run before, its own last result should appear.
Storage can't do that today: `core/ingest._persist` writes one file per
`<source>-<month>`, so a later run by any route overwrites the earlier one. The
transport is recorded ON the run, but no per-route history is kept. Showing a
route's own last result means keying runs by transport too.
