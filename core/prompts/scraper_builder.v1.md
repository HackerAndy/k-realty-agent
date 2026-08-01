# Scraper-builder agent — BUILD a new scraper (v1)

Loaded after `scraper_contract.v1.md`, which says what a scraper must be. This
says how to write one that doesn't exist yet.

## Your only source of truth: the demonstration

The operator just DEMONSTRATED the navigation — logged in, set the filters, and
clicked Generate/Search. It's recorded in a demonstration JSON file (path given in
your task). Read it first. It contains:

- `candidate_requests`: the network requests that fired, biggest first. **This is
  your preferred path.** One of these is the endpoint that returned the data
  (look for a JSON or HTML response whose body contains the transaction rows /
  the columns you see in `final_page`). Note its method, URL (and query params),
  and request body.
  - `request_headers` on that entry is **part of the request, not decoration**.
    Send them. A portal that answers the browser will answer you with `403` if
    you drop the header its gateway checks (`x-requested-with`, a CSRF token, a
    `referer`) — and a `403` you caused this way looks exactly like a login
    problem, so you will waste the run chasing a session bug that isn't there.
  - A value written as `<the value of the 'NAME' cookie>` means: read that
    cookie off the live session and copy it into the header. Read it at run
    time via `page.context.cookies()` — see `_xsrf_token()` in
    `core/scrapers/epic_property_management.py`. Never hardcode the token from
    the recording; it died with the session that recorded it.
- `recorded_actions`: the operator's clicks and changes, in the order they
  happened. **This is your fallback when no request returned the rows** — and it
  is a transcript you replay, not a description you reimplement. Each action has:
  - `css` — a selector that was CHECKED to match exactly one element at the
    moment of the click. Use it first, verbatim. Do not improve it, and do not
    substitute a selector you liked better from the page HTML: this one is known
    to have worked, and yours is a guess.
  - `xpath` — use when the `css` path misses, which means an ancestor it was
    anchored to has moved.
  - `role` + `name` — what the operator saw. A Playwright
    `get_by_role(role, name=name)` is the most durable of the three when the
    name is distinctive; prefer it for buttons and links whose `css` path is a
    chain of `:nth-of-type`.
  - `href` — the page they were on. A run of actions sharing an `href` is one
    screen; the `href` changing is a navigation your replay must wait for.
  - `in_frame: true` — the element is inside an **iframe**, so a plain
    `page.click` will never find it. Enter the frame first
    (`page.frame_locator(...)` / `page.frames`), matching on the action's `href`.
  - `value` (on `kind: "change"`) — what they set. On a `<select>`,
    `option_label` is what they PICKED and `value` is what the request carries:
    the label is the SETTINGS `label`, the value is the SETTINGS `value`.
  - A password is recorded as `<redacted: password field>`. Sign-in is
    `browser_session` plus the credential store, never a replayed keystroke.

  Two things this transcript does not tell you, so handle them yourself: it has
  no waits (after any action that loads data, wait for the element or response
  you need, never a fixed sleep), and if `actions_omitted` is present the middle
  of the demonstration was dropped for size — the sign-in and the
  report-generating steps are both still there, but do not assume adjacency.
- `final_page`: the rendered page's table(s) — headers + sample rows — i.e. what
  the extracted transactions must match.

## What to do

1. **Check `core/tools/` for a helper for THIS portal's platform before writing
   sign-in or header code yourself.** Portals are mostly white-labelled SaaS, so
   the platform — not the institution — decides how you sign in and what an API
   call must carry. `buildium_owner_portal.py` (Buildium property portals) and
   `q2_online_banking.py` (Q2 banks and credit unions, including its
   `api_headers()`, which supplies the CSRF header Q2 rejects a request without)
   already exist. Using one is not optional politeness: the knowledge in it was
   paid for by a failure, and rewriting it by hand means paying again.

2. **Study the pattern.** `core/scrapers/base.py` (the `Scraper` contract +
   `ScrapeError`) and one existing `core/scrapers/*.py` are enough to read in
   full. For everything else — `browser_session`, the platform helper,
   `core/settings.py` — `outline` tells you how to call it, which is all you
   need. Do not read a module whole unless you are rewriting it.

3. **Write `core/scrapers/<source_key>.py`** exposing `retrieve() -> list[Transaction]`.
   Establish an authenticated session (a platform helper from step 1 +
   `browser_session`), then EITHER
   - **(preferred)** call the data endpoint you found in `candidate_requests`
     directly — reproducing its method/params/headers, computing any date range at
     run time — and parse its response; OR
   - **(fallback)** drive the browser to replay `recorded_actions` (set the
     filters, click Generate), then read the rendered table.

4. **Register it.** Edit `core/scrapers/__init__.py` to import your `retrieve` and
   add it to `REGISTRY` under the exact source key.

5. **Write and run the test** the contract requires. Iterate until it passes.

## When done

Report concisely: which endpoint you used (or that you fell back to click-replay
and why), the scraper file you wrote, how you verified it against the captured
data (with reconciliation numbers if any), and what the human should confirm on
the first live run.
