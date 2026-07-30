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

**Still open:** in a two-stage graph, does selecting a route that didn't produce the
rows also blank the `14 transactions` count, or is the count a property of the source?
(Today the prototype keeps it and clears only the table.)
