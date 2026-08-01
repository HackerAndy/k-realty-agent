# Template candidate: generic (tier 1) — records a user's navigation demo so the
# embedded agent can author a scraper from it. No client specifics.
# See agent-harness-template/docs/promotion-log.md.
"""Record a DEMONSTRATION of how to reach a portal's data, for the scraper builder.

This is the scraper analog of "here's a sample document" for the parser builder:
the operator opens the real portal, logs in, sets whatever filters/dropdowns, and
clicks Generate/Search so their data renders — and we record what happened, so the
harness's embedded agent can WRITE the scraper (it, not a developer, owns the
domain navigation).

We capture, for the agent to work from:
  - NETWORK (preferred): every request/response as a HAR, so the agent can find
    the data endpoint "Generate" fired and call it directly with computed dates —
    robust, and usually returns clean structured data.
  - ACTIONS (fallback): the operator's clicks/changes (via an injected listener),
    each carrying a locator verified unique against the live DOM at the moment it
    happened, so the agent can replay the navigation if there's no clean API.
  - The final rendered page's table structure (headers + sample rows).

Everything lands under data/ (gitignored) — HARs and demos contain real values.
This module must stay framework-free (no langgraph/langchain imports).
"""

from __future__ import annotations

import json
from pathlib import Path

from playwright.sync_api import sync_playwright

PROFILE_ROOT = Path(".browser_profiles")
DEMO_DIR = Path("data/demos")

# Injected before any page script, in EVERY frame of every document — records the
# operator's clicks/changes to a window array. Idempotent so it survives an
# Angular SPA's in-app navigations.
#
# What this captures is what a replay can be written from, and the first version
# captured a DESCRIPTION of the click rather than a way to perform it: `{tag:
# 'BUTTON', cls: 'btn btn-primary', text: 'Generate'}` leaves the agent to INVENT
# a selector, and invented selectors are why click-replay was the fallback nobody
# wanted to reach. So every event now carries
#
#   css      — a locator VERIFIED unique against the live DOM as the click landed
#   xpath    — an unambiguous fallback for when the css path's anchors move
#   role/name— what the operator actually read off the screen, for a semantic locator
#   href     — where they were; in_frame says the element is inside an iframe
#   t        — when, so clicks from several tabs sort into the order they happened
#              rather than the order the tabs happen to appear in
#
# Wall-clock (Date.now), deliberately: performance.now() is per-document, so it
# cannot order an event in one tab against an event in another, which is the
# whole reason the timestamp is here.
#
# CALLED, not merely defined. add_init_script takes a script to EVALUATE, so the
# arrow function this used to be was evaluated to a function value and thrown
# away — the listener never installed, on any run, and every demonstration ever
# captured carried `recorded_actions: []`. Nothing caught it: the fallback path
# was never reached (both live sources found an endpoint), and the recorder's
# tests read their actions from a fake `page.evaluate`, which answers whether or
# not anything was ever injected. Hence the browser-backed tests.
_ACTION_LOGGER = """
(() => {
  if (window.__demo_hooked) return;
  window.__demo_hooked = true;
  window.__demo_actions = [];

  var MAX = 400;  // a page that fires clicks at itself must not eat the tab

  // Attributes worth building a selector on: a redeploy keeps them, and they say
  // what the element IS. Class names and sibling positions are the last resort.
  var ATTRS = ['data-testid', 'data-test', 'data-qa', 'data-automation-id',
               'name', 'aria-label'];
  // Framework-generated ids change on every render, so a selector built on one
  // works exactly once — on the recording.
  var GENERATED_ID = /^[0-9:]|^(ng|mat|cdk|ember|radix|react|aria)-?[0-9]|[0-9a-f]{8,}/i;
  var INTERACTIVE = 'a,button,input,select,textarea,label,summary,[role],[onclick],[tabindex]';

  var esc = function (v) { return CSS.escape(String(v)); };
  var clean = function (s) { return String(s || '').replace(/\\s+/g, ' ').trim(); };
  var unique = function (sel) {
    try { return document.querySelectorAll(sel).length === 1; } catch (e) { return false; }
  };

  // A password is not navigation. It belongs in the credential store; a
  // demonstration is written to disk and read aloud to a model.
  var secret = function (el) {
    var auto = (el.getAttribute('autocomplete') || '').toLowerCase();
    return el.type === 'password' || auto.indexOf('password') >= 0 || auto === 'one-time-code';
  };

  var attrPart = function (el) {
    for (var i = 0; i < ATTRS.length; i++) {
      var v = el.getAttribute(ATTRS[i]);
      if (v) return '[' + ATTRS[i] + '=' + JSON.stringify(v) + ']';
    }
    return '';
  };

  // Walk up until the path is unique IN THIS DOCUMENT — checked, not assumed.
  var cssPath = function (el) {
    var parts = [], node = el, depth = 0;
    while (node && node.nodeType === 1 && depth++ < 8) {
      var tag = node.tagName.toLowerCase();
      if (node.id && !GENERATED_ID.test(node.id)) {
        var byId = [tag + '#' + esc(node.id)].concat(parts).join(' > ');
        if (unique(byId)) return byId;
      }
      var part = tag + attrPart(node);
      if (part === tag) {
        var parent = node.parentElement;
        if (parent) {
          var same = [], kids = parent.children;
          for (var i = 0; i < kids.length; i++) {
            if (kids[i].tagName === node.tagName) same.push(kids[i]);
          }
          if (same.length > 1) part += ':nth-of-type(' + (same.indexOf(node) + 1) + ')';
        }
      }
      parts.unshift(part);
      var whole = parts.join(' > ');
      if (unique(whole)) return whole;
      node = node.parentElement;
    }
    return parts.join(' > ');
  };

  var xPath = function (el) {
    var parts = [], node = el, depth = 0;
    while (node && node.nodeType === 1 && depth++ < 12) {
      var parent = node.parentElement, tag = node.tagName.toLowerCase();
      if (!parent) { parts.unshift(tag); break; }
      var same = [], kids = parent.children;
      for (var i = 0; i < kids.length; i++) {
        if (kids[i].tagName === node.tagName) same.push(kids[i]);
      }
      parts.unshift(same.length > 1 ? tag + '[' + (same.indexOf(node) + 1) + ']' : tag);
      node = parent;
    }
    // Rooted only if we really reached the top; otherwise say so with '//'.
    return (node && node.parentElement ? '//' : '/') + parts.join('/');
  };

  var IMPLICIT = {A: 'link', BUTTON: 'button', SELECT: 'combobox', TEXTAREA: 'textbox',
                  SUMMARY: 'button', OPTION: 'option', TH: 'columnheader'};
  var INPUT_ROLE = {checkbox: 'checkbox', radio: 'radio', submit: 'button',
                    button: 'button', reset: 'button', search: 'searchbox'};
  var roleOf = function (el) {
    return el.getAttribute('role') ||
      (el.tagName === 'INPUT' ? (INPUT_ROLE[el.type] || 'textbox')
                              : (IMPLICIT[el.tagName] || ''));
  };

  // The accessible name: what the operator read on screen, in the order a
  // screen reader would resolve it. Never el.value — that is their data.
  var nameOf = function (el) {
    var found = clean(el.getAttribute('aria-label'));
    if (!found) {
      var ids = clean(el.getAttribute('aria-labelledby')).split(' ').filter(Boolean);
      found = clean(ids.map(function (id) {
        var node = document.getElementById(id);
        return node ? node.innerText : '';
      }).join(' '));
    }
    if (!found && el.labels && el.labels.length) found = clean(el.labels[0].innerText);
    if (!found && el.closest) {
      var wrapper = el.closest('label');
      if (wrapper) found = clean(wrapper.innerText);
    }
    if (!found) found = clean(el.getAttribute('title') || el.getAttribute('placeholder'));
    if (!found) found = clean(el.innerText);
    return found.slice(0, 80);
  };

  var describe = function (el) {
    return {
      t: Date.now(),
      href: location.href,
      in_frame: window.top !== window,
      tag: el.tagName,
      role: roleOf(el),
      name: nameOf(el),
      css: cssPath(el).slice(0, 240),
      xpath: xPath(el).slice(0, 240)
    };
  };

  var settled = {};
  var push = function (action) {
    var log = window.__demo_actions;
    if (log.length >= MAX) return;
    if (action.kind === 'change') {
      // A field fires input as it is typed and change AGAIN when it loses
      // focus — which happens when the operator clicks the next control, so the
      // echo lands after that control and reads as them going back to it. Same
      // field, same value, nothing happened: it is not a step in the navigation.
      if (settled[action.css] === action.value) return;
      settled[action.css] = action.value;
    }
    var prev = log[log.length - 1];
    // And a field fires input per keystroke. Only the value they settled on is
    // the setting; the rest is a transcript of someone typing.
    if (prev && action.kind === 'change' && prev.kind === 'change' && prev.css === action.css) {
      log[log.length - 1] = action;
      return;
    }
    log.push(action);
  };

  // What the operator MEANT to click. A click landing on the span inside a
  // button is a click on the button, and a click on the page background is not
  // an action at all — dropping those is most of what keeps a replay short
  // enough to read. A div styled as a button is caught by its cursor.
  var acted = function (target) {
    if (!target || target.nodeType !== 1) return null;
    var el = target.closest(INTERACTIVE);
    if (el) return el;
    try {
      if (getComputedStyle(target).cursor === 'pointer') return target;
    } catch (e) { }
    return null;
  };

  document.addEventListener('click', function (event) {
    try {
      var el = acted(event.target);
      if (el) push(Object.assign({kind: 'click'}, describe(el)));
    } catch (err) { }
  }, true);

  // 'input' as well as 'change', because a text field only fires change when it
  // loses focus — so a date range typed and then submitted with the keyboard
  // recorded nothing at all, and one typed and then clicked away from recorded
  // only by luck of the blur order. Both are the same fact ("they set this field
  // to X"), and push() collapses the keystrokes into the value they settled on.
  var onSet = function (event) {
    try {
      var el = event.target;
      if (!el || el.nodeType !== 1) return;
      var action = Object.assign({kind: 'change'}, describe(el));
      if (secret(el)) {
        action.value = '<redacted: password field>';
      } else if (el.tagName === 'SELECT') {
        var option = el.options[el.selectedIndex];
        action.value = String(el.value || '').slice(0, 80);
        // The label is what they picked; the value is what the request carries.
        // A scraper needs both to turn a SETTINGS choice back into a request.
        action.option_label = option ? clean(option.text).slice(0, 80) : '';
      } else if (el.type === 'checkbox' || el.type === 'radio') {
        action.value = el.checked ? 'true' : 'false';
      } else {
        action.value = String(el.value || '').slice(0, 80);
      }
      push(action);
    } catch (err) { }
  };
  document.addEventListener('change', onSet, true);
  document.addEventListener('input', onSet, true);
})()
"""

_STATIC_SUFFIXES = (".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg",
                    ".woff", ".woff2", ".ttf", ".ico", ".map")

# A demonstration has to FIT. Every request kept here is context the agent pays
# for on every turn, and the harness runs against whatever model the operator
# chose — including a local one on a single machine's memory. Giving all fifteen
# candidates a 12,000-char body produced a 49,000-token demonstration: more than
# twice the budget the run actually had, so the build died on the model server's
# memory guard before it ever opened the file. Unreadable is worse than clipped.
#
# The bodies are not all worth the same, either. The agent is looking for ONE
# endpoint — the one whose response holds the rows — and to find it, it needs
# enough of each body to recognise the shape and enough of the RIGHT body to
# read every column off it. So the largest few (sorted first, and the data
# payload is almost always among them) keep a full sample; the rest keep a probe
# that answers "is this the one?" and nothing more. URLs and headers stay
# complete for all of them — those are what a wrong guess is corrected against,
# and they are cheap.
_MAX_BODY = 12000       # the likely data payloads
_PROBE_BODY = 600       # enough to identify the rest
_FULL_BODY_REQUESTS = 2

# The same budget applies to the clicks, and richer actions cost more each. A
# demonstration is minutes of a human being thorough, so the tail — the filters
# and the Generate click — is the part a replay is written from, while the head
# is how they got to the screen. The middle is where the re-reading and the
# second-guessing live, so the middle is what goes.
_MAX_ACTIONS = 120
_HEAD_ACTIONS = 15


def record(service_key: str, url: str = "", out_dir: Path = DEMO_DIR,
           max_wait_s: float = 20 * 60) -> Path:
    """Open a headed browser; the operator logs in, sets filters, and clicks
    Generate/Search so the data renders, then CLOSES THE WINDOW. Returns the path
    to a demonstration JSON artifact for the scraper-builder agent.

    Closing the window is the finish signal — this used to block on input() at a
    terminal, which no GUI can answer, and the GUI is the only front-end now.

    `url` is optional: with none, the browser opens blank and the operator
    navigates to the portal themselves. The harness then LEARNS the URL from the
    demonstration rather than asking for it up front — one less thing to type,
    and what they actually visited beats what they meant to type.

    Because the page can't be read once it's closed, the state the agent needs
    (recorded actions, the rendered table) is snapshotted on every poll while the
    window is still open; the last good snapshot is what gets written.
    """
    from core.tools.browser_session import close_context, wait_until_browsing_done

    out_dir.mkdir(parents=True, exist_ok=True)
    profile = PROFILE_ROOT / service_key
    profile.mkdir(parents=True, exist_ok=True)
    har_path = out_dir / f"{service_key}-demo.har"

    latest: dict = {"actions": {}, "structure": {}, "final_url": url, "urls": []}

    def snapshot(pages) -> None:
        """Snapshot every open tab. The demonstration is whatever the operator
        did across all of them — a portal will open its report in a new tab, and
        recording only the tab we opened would miss the actual data."""
        focused = None
        for page in pages:
            for source in _action_sources(page):
                try:
                    actions = source.evaluate("() => window.__demo_actions || []")
                except Exception:
                    continue    # mid-navigation; it'll be readable next poll
                _remember(actions, id(source), latest["actions"])
            try:
                if page.evaluate("() => document.visibilityState") == "visible":
                    focused = page
            except Exception:
                pass
            current = page.url
            if current and current not in latest["urls"]:
                latest["urls"].append(current)

        target = focused or (pages[-1] if pages else None)
        if target is None:
            return
        structure = _page_structure(target)
        # Don't let a last-second navigation to a table-less page throw away the
        # rendered data the operator worked to bring up.
        if structure.get("tables") or not latest["structure"].get("tables"):
            latest["structure"] = structure
        if target.url:
            latest["final_url"] = target.url

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(profile),
            headless=False,
            record_har_path=str(har_path),
            record_har_content="embed",
        )
        context.add_init_script(_ACTION_LOGGER)
        page = context.pages[0] if context.pages else context.new_page()
        if url:
            page.goto(url, wait_until="domcontentloaded")
        reason = wait_until_browsing_done(context, profile, max_wait_s, on_poll=snapshot)
        print(f"[demo_recorder] demonstration for '{service_key}' finished: {reason}", flush=True)
        # Closing flushes the HAR — the network trace is the best material the
        # scraper builder gets, so give it room before resorting to a kill.
        close_context(context, profile, timeout_s=20.0)

    if reason == "deadline":
        raise RuntimeError(
            f"Gave up waiting for the '{service_key}' demonstration after "
            f"{int(max_wait_s / 60)} minutes. Nothing was captured — start it again and "
            "close the browser window once your data is on screen."
        )

    structure = latest["structure"]
    # One demonstration, however many tabs and frames it took, in the order the
    # operator actually did it.
    actions, dropped = _merge_actions(latest["actions"])
    demo = {
        "service_key": service_key,
        # What they actually visited, not what they meant to type.
        "start_url": url or (latest["urls"][0] if latest["urls"] else ""),
        "final_url": latest["final_url"],
        "visited_urls": latest["urls"],
        "title": structure.get("title", ""),
        "recorded_actions": actions,
        "candidate_requests": _extract_requests(har_path),
        "final_page": structure,
        "har_file": str(har_path),
    }
    if dropped:
        # Said out loud, because a replay written from a gapped transcript that
        # looked complete is worse than one written from an honest gap.
        demo["actions_omitted"] = (
            f"{dropped} action(s) from the middle of the demonstration were omitted to keep "
            "this file readable. The sign-in and the report-generating steps are both present."
        )
    demo_path = out_dir / f"{service_key}-demonstration.json"
    demo_path.write_text(json.dumps(demo, indent=2))
    return demo_path


def _action_sources(page):
    """Every document the operator could have clicked in: the page and its frames.

    The init script runs in all of them, but `page.evaluate` only ever reaches
    the main frame — so a portal that renders its report inside an iframe (they
    do; a report viewer is the classic case) recorded the navigation TO the
    iframe and not one thing that happened inside it. `page.frames` includes the
    main frame, so this is the page itself plus the rest.
    """
    try:
        frames = list(page.frames)
    except Exception:
        frames = []
    return frames or [page]


def _signature(action: dict, index: int, source_key: int):
    """What makes this action the same action when we read the array again.

    Every poll re-reads the whole array, and a hard navigation empties it, so
    the actions have to accumulate across polls without counting twice. A
    timestamp does that on its own. Without one — an action from a listener too
    old to stamp them — position in its own source's array is all there is, which
    accumulates to exactly the longest array that source ever held.
    """
    stamp = action.get("t")
    if stamp is None:
        return (source_key, index)
    return (stamp, action.get("kind"), action.get("css"), action.get("value"))


def _remember(actions, source_key: int, store: dict) -> None:
    """Fold one source's action array into everything seen so far.

    This replaces keeping the LONGEST array per tab, which quietly lost work: an
    SPA empties the array on a hard navigation, so every click after the
    navigation was discarded until the new array grew past the old one's length —
    and the ones it did keep were the wrong ones. Accumulating keeps both sides
    of the navigation, which is what the timestamps are for.
    """
    if not isinstance(actions, list):
        return
    for index, action in enumerate(actions):
        if isinstance(action, dict):
            store.setdefault(_signature(action, index, source_key), action)


def _merge_actions(store: dict) -> tuple[list[dict], int]:
    """The operator's actions in the order they happened, trimmed to a budget.

    Returns (actions, dropped). Ordering is by timestamp — tabs interleave, and
    the order the tab objects happen to be listed in is not the order the human
    worked in. Unstamped actions sort first and keep the order they arrived in.
    """
    actions = sorted(store.values(), key=lambda action: action.get("t") or 0)
    if len(actions) <= _MAX_ACTIONS:
        return actions, 0
    tail = _MAX_ACTIONS - _HEAD_ACTIONS
    return actions[:_HEAD_ACTIONS] + actions[-tail:], len(actions) - _MAX_ACTIONS


def _extract_requests(har_path: Path) -> list[dict]:
    """Pull the likely DATA requests out of the HAR — XHR/fetch calls returning
    JSON/HTML, skipping static assets. These are what the agent inspects to find
    the endpoint 'Generate' fired."""
    try:
        har = json.loads(har_path.read_text())
    except Exception:
        return []
    out: list[dict] = []
    for entry in har.get("log", {}).get("entries", []):
        req = entry.get("request", {})
        resp = entry.get("response", {})
        url = req.get("url", "")
        rtype = (entry.get("_resourceType") or "").lower()
        mime = (resp.get("content", {}) or {}).get("mimeType", "")
        if any(url.lower().split("?")[0].endswith(s) for s in _STATIC_SUFFIXES):
            continue
        is_data = rtype in ("xhr", "fetch") or "json" in mime or "html" in mime
        if not is_data or resp.get("status") not in (200, 201):
            continue
        body = (resp.get("content", {}) or {}).get("text", "") or ""
        out.append({
            "method": req.get("method", ""),
            "url": url,
            "resource_type": rtype,
            "request_headers": _request_headers(req),
            "request_body": _clip((req.get("postData", {}) or {}).get("text", ""), 4000),
            "status": resp.get("status"),
            "response_mime": mime,
            "response_body_sample": _clip(body, _MAX_BODY),
            "response_bytes": (resp.get("content", {}) or {}).get("size", len(body)),
        })
    # Biggest responses first — the data payload is usually the largest.
    out.sort(key=lambda r: r.get("response_bytes") or 0, reverse=True)
    out = out[:15]
    # Now that they're ranked, spend the body budget where it can be used.
    for request in out[_FULL_BODY_REQUESTS:]:
        request["response_body_sample"] = _clip(
            request["response_body_sample"], _PROBE_BODY
        )
    return out


# Headers the browser sets for itself. Playwright's own request context sets the
# same ones, so repeating them to the agent is noise it has to read past.
_BROWSER_MANAGED_HEADERS = frozenset({
    "accept-encoding", "accept-language", "connection", "content-length", "host",
    "priority", "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
    "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site", "sec-fetch-user",
    "upgrade-insecure-requests", "user-agent",
})

# Names whose value is a bearer secret in its own right. A value that turns out
# to be a cookie's is annotated instead (see below) — this is the fallback for
# one we can't source, where the NAME is the useful half and the value is not
# ours to copy into a prompt.
_SECRETISH = ("authorization", "token", "secret", "api-key", "apikey", "auth")


def _request_headers(req: dict) -> dict[str, str]:
    """The request headers an API call actually needs, minus the noise.

    A portal's data endpoint is rarely just a URL. Q2 online banking wants its
    `q2token` echoed from cookie into header, Buildium wants `XSRF-TOKEN` the
    same way, and plenty of gateways 403 without `x-requested-with`. A recording
    that captured only the URL and the response therefore described a request
    that CANNOT be reproduced, and the agent — with no way to see what it was
    missing — read the resulting 403 as "the operator isn't logged in" and went
    looking for a session bug that wasn't there.

    Cookies are not listed: the browser context replays them on its own. But
    they ARE cross-referenced, because the header that matters is usually a
    cookie's value copied across (double-submit CSRF). Saying so by name tells
    the agent to READ THAT COOKIE — the code it needs to write — rather than
    baking in a literal token that dies with the session.
    """
    cookies: dict[str, str] = {}
    for header in req.get("headers", []) or []:
        if (header.get("name") or "").lower() != "cookie":
            continue
        for part in (header.get("value") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name and value:
                cookies.setdefault(value, name)  # keyed BY value, to look up below

    out: dict[str, str] = {}
    for header in req.get("headers", []) or []:
        name = (header.get("name") or "").strip()
        value = (header.get("value") or "").strip()
        lowered = name.lower()
        if not name or name.startswith(":"):  # HTTP/2 pseudo-headers
            continue
        if lowered == "cookie" or lowered in _BROWSER_MANAGED_HEADERS:
            continue
        source_cookie = cookies.get(value)
        if source_cookie:
            out[name] = f"<the value of the {source_cookie!r} cookie>"
        elif any(hint in lowered for hint in _SECRETISH):
            out[name] = "<redacted secret — read it from the session, don't hardcode>"
        else:
            out[name] = _clip(value, 200)
    return out


def _page_structure(page) -> dict:
    """Final rendered page: title, url, and each table's headers + a few rows."""
    tables = page.eval_on_selector_all(
        "table",
        """els => els.map(t => {
            const rows = Array.from(t.querySelectorAll('tr'));
            const cells = tr => Array.from(tr.querySelectorAll('th,td'))
                .map(c => (c.innerText||'').replace(/\\s+/g,' ').trim());
            return {
                th_headers: Array.from(t.querySelectorAll('th')).map(h => (h.innerText||'').trim()).filter(Boolean),
                header_row: rows.length ? cells(rows[0]) : [],
                sample_rows: rows.slice(1, 8).map(cells),
                row_count: rows.length,
            };
        })""",
    )
    return {"title": page.title(), "url": page.url, "tables": tables}


def _clip(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + f"\n...[clipped {len(text) - limit} chars]"
