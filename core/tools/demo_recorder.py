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
  - ACTIONS (fallback): the operator's clicks/changes (best-effort, via an injected
    listener) so the agent can replay the navigation if there's no clean API.
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

# Injected before any page script, on every document — records clicks/changes to a
# window array. Idempotent so it survives an Angular SPA's in-app navigations.
_ACTION_LOGGER = """
() => {
  if (window.__demo_hooked) return;
  window.__demo_hooked = true;
  window.__demo_actions = [];
  const desc = el => el ? {
    tag: el.tagName,
    id: el.id || '',
    name: (el.getAttribute && el.getAttribute('name')) || '',
    cls: (el.className && el.className.toString().slice(0, 60)) || '',
    text: (el.innerText || el.value || '').toString().trim().slice(0, 40),
    type: el.type || ''
  } : {};
  document.addEventListener('click', e =>
    window.__demo_actions.push({kind: 'click', ...desc(e.target)}), true);
  document.addEventListener('change', e =>
    window.__demo_actions.push({kind: 'change', ...desc(e.target),
      value: (e.target.value || '').toString().slice(0, 80)}), true);
}
"""

_STATIC_SUFFIXES = (".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg",
                    ".woff", ".woff2", ".ttf", ".ico", ".map")
_MAX_BODY = 12000  # per response body kept for the agent


def record(service_key: str, url: str, out_dir: Path = DEMO_DIR) -> Path:
    """Open a headed browser at `url`; the operator logs in, sets filters, and
    clicks Generate/Search so the data renders, then presses Enter. Returns the
    path to a demonstration JSON artifact for the scraper-builder agent."""
    out_dir.mkdir(parents=True, exist_ok=True)
    profile = PROFILE_ROOT / service_key
    profile.mkdir(parents=True, exist_ok=True)
    har_path = out_dir / f"{service_key}-demo.har"

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(profile),
            headless=False,
            record_har_path=str(har_path),
            record_har_content="embed",
        )
        context.add_init_script(_ACTION_LOGGER)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(url, wait_until="domcontentloaded")
        input(
            "\nA browser is open. Log in if needed, set your filters/dropdowns, and click "
            "Generate/Search so YOUR data is on screen. Then press Enter here to finish "
            "recording... "
        )
        try:
            actions = page.evaluate("() => window.__demo_actions || []")
        except Exception:
            actions = []
        structure = _page_structure(page)
        final_url = page.url
        context.close()  # flushes the HAR to disk

    demo = {
        "service_key": service_key,
        "start_url": url,
        "final_url": final_url,
        "title": structure.get("title", ""),
        "recorded_actions": actions,
        "candidate_requests": _extract_requests(har_path),
        "final_page": structure,
        "har_file": str(har_path),
    }
    demo_path = out_dir / f"{service_key}-demonstration.json"
    demo_path.write_text(json.dumps(demo, indent=2))
    return demo_path


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
            "request_body": _clip((req.get("postData", {}) or {}).get("text", ""), 4000),
            "status": resp.get("status"),
            "response_mime": mime,
            "response_body_sample": _clip(body, _MAX_BODY),
            "response_bytes": (resp.get("content", {}) or {}).get("size", len(body)),
        })
    # Biggest responses first — the data payload is usually the largest.
    out.sort(key=lambda r: r.get("response_bytes") or 0, reverse=True)
    return out[:15]


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
