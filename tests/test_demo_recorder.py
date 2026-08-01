"""Capturing a portal demonstration without a terminal.

The recorder used to end with input() — "press Enter when your data is on
screen". The GUI is the only front-end now, and it has no Enter to press, so the
finish signal is closing the browser window, exactly like login recovery.

That change creates two hazards worth pinning:

* A page can't be read once it's closed. Everything the agent needs (the clicks,
  the rendered table, the URL the operator actually reached) has to be
  snapshotted WHILE the window is open, or the demonstration comes back empty
  and the operator is asked to do it twice.
* The demonstration is not one tab. The operator opens a new tab to reach the
  portal, or the portal opens its report in one; tying the session's life to the
  tab the recorder happened to hold ended demonstrations seconds after they
  began ("finished: page closed" while the browser was still on screen).
"""

import json

import pytest

import core.tools.browser_session as bs
import core.tools.demo_recorder as dr


class _FakePage:
    """A tab that becomes real at `opens_at` and closes at `closes_after`.

    Poll number is counted by is_closed(), which the wait loop calls once per
    page per poll. A tab still showing about:blank is not a page the operator
    is working in — the recorder must ignore it, not finish on it.
    """

    def __init__(self, url="https://portal.example.com/reports",
                 opens_at=2, closes_after=None, visible=True):
        self._target = url
        self._opens_at = opens_at
        self._closes_after = closes_after
        self.visible = visible
        self.url = "about:blank"
        self.checks = 0
        self.evaluated = 0

    def is_closed(self):
        self.checks += 1
        if self.checks >= self._opens_at:
            self.url = self._target
        return self._closes_after is not None and self.checks >= self._closes_after

    def goto(self, *a, **k):
        pass

    def evaluate(self, script):
        if "visibilityState" in script:
            return "visible" if self.visible else "hidden"
        self.evaluated += 1
        return [{"kind": "click", "text": f"Generate {self.evaluated}"}] * self.evaluated


class _FakeContext:
    def __init__(self, *pages):
        self.pages = list(pages)
        self.closed = False

    def new_page(self):
        return self.pages[0]

    def add_init_script(self, *a, **k):
        pass

    def close(self):
        self.closed = True


@pytest.fixture
def recorder(monkeypatch, tmp_path):
    """Drive record() against a fake browser, in a temp working directory."""
    monkeypatch.setattr(bs, "_find_pids", lambda pattern: [4242])
    monkeypatch.setattr(bs.time, "sleep", lambda s: None)
    monkeypatch.setattr(dr, "_extract_requests", lambda har: [{"url": "/api/transactions"}])
    monkeypatch.setattr(dr, "_page_structure",
                        lambda page: {"title": f"Reports {page.url}",
                                      "tables": [{"headers": ["Date", "Amount"]}]})
    monkeypatch.setattr(dr, "PROFILE_ROOT", tmp_path / "profiles")

    def run(*pages, url="", out_dir=None, max_wait_s=60.0):
        context = _FakeContext(*pages)

        class _PW:
            class chromium:
                @staticmethod
                def launch_persistent_context(*a, **k):
                    return context

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(dr, "sync_playwright", lambda: _PW())
        path = dr.record("portal", url, out_dir or (tmp_path / "demos"), max_wait_s=max_wait_s)
        return json.loads(path.read_text()), context

    return run


def test_closing_the_window_finishes_the_recording(recorder):
    """It must not wait on stdin — under pytest, input() would raise outright,
    which is exactly the regression this guards."""
    demo, context = recorder(_FakePage(closes_after=3))

    assert context.closed, "the context is closed so the HAR is flushed"
    assert demo["candidate_requests"] == [{"url": "/api/transactions"}]


def test_the_page_is_captured_while_it_is_still_open(recorder):
    """Read it after the close and there's nothing left to read."""
    demo, _ = recorder(_FakePage(closes_after=4))

    assert demo["recorded_actions"], "the operator's clicks survived the close"
    assert demo["final_page"]["title"].startswith("Reports")


def test_the_longest_run_of_clicks_wins(recorder):
    """A hard navigation resets the in-page action array; taking the newest
    snapshot blindly would throw away everything they did before it."""
    page = _FakePage(closes_after=5)
    demo, _ = recorder(page)

    assert len(demo["recorded_actions"]) == max(1, page.evaluated)


def test_it_learns_the_url_instead_of_asking_for_one(recorder):
    """The wizard has no URL field: whatever they navigated to is the answer."""
    demo, _ = recorder(_FakePage(closes_after=4), url="")

    assert demo["final_url"] == "https://portal.example.com/reports"
    assert demo["start_url"] == "https://portal.example.com/reports"
    assert "about:blank" not in demo["visited_urls"]


def test_a_given_url_is_still_honoured(recorder):
    demo, _ = recorder(_FakePage(closes_after=3), url="https://portal.example.com/login")
    assert demo["start_url"] == "https://portal.example.com/login"


def test_a_snapshot_that_blows_up_does_not_end_the_session(recorder, monkeypatch):
    """A transient evaluate() failure mid-navigation is normal; ending the
    operator's demonstration over one would not be."""
    monkeypatch.setattr(dr, "_page_structure",
                        lambda page: (_ for _ in ()).throw(RuntimeError("navigating")))
    page = _FakePage(closes_after=4)

    demo, _ = recorder(page)

    assert page.checks >= 4, "it kept polling"
    assert demo["final_page"] == {}


def test_closing_the_first_tab_does_not_end_the_demonstration(recorder):
    """THE reported bug: 'finished: page closed' arrived while the browser was
    still open, because the operator had opened a second tab (to reach the
    portal, or the portal opened its report in one) and closed the first."""
    first = _FakePage(url="https://portal.example.com/login", closes_after=4)
    second = _FakePage(url="https://portal.example.com/gl-report",
                       opens_at=3, closes_after=8)

    demo, _ = recorder(first, second)

    assert second.checks >= 8, "the session lived as long as the tab in use"
    assert demo["final_url"] == "https://portal.example.com/gl-report"
    assert demo["final_page"]["title"].endswith("gl-report"), "it read the tab they were in"


def test_the_tab_in_front_is_the_one_that_counts(recorder):
    """With two tabs open, the data is on the one they're looking at."""
    background = _FakePage(url="https://portal.example.com/login",
                           closes_after=6, visible=False)
    foreground = _FakePage(url="https://portal.example.com/gl-report",
                           closes_after=6, visible=True)

    demo, _ = recorder(background, foreground)

    assert demo["final_url"] == "https://portal.example.com/gl-report"


def test_clicks_from_every_tab_reach_the_agent(recorder):
    """The demonstration is what they did, wherever they did it."""
    first = _FakePage(url="https://portal.example.com/login", closes_after=5)
    second = _FakePage(url="https://portal.example.com/gl-report",
                       opens_at=2, closes_after=5)

    demo, _ = recorder(first, second)

    assert len(demo["recorded_actions"]) == first.evaluated + second.evaluated


def test_it_gives_up_rather_than_waiting_forever(recorder, monkeypatch):
    ticks = iter(range(0, 100_000, 100))
    monkeypatch.setattr(bs.time, "monotonic", lambda: next(ticks))

    with pytest.raises(RuntimeError, match="Gave up waiting"):
        recorder(_FakePage(closes_after=None), max_wait_s=30.0)


# ── what the agent is told about a request ────────────────────────────
#
# Field failure this guards: the recording described the DFCU data endpoint by
# URL and response only. The real call also carried `q2token` (the session
# cookie, echoed into a header — Q2's double-submit CSRF) and
# `x-requested-with`. Without them the endpoint returns 403, so the scraper the
# agent wrote could never work; and because a 403 reads as "not signed in", the
# agent spent its whole run looking for a session bug, concluded the 403s were
# "credential issues, not code bugs", and shipped the broken call.


def _har_request(headers):
    return {"headers": [{"name": n, "value": v} for n, v in headers]}


def test_the_headers_a_request_needs_reach_the_agent():
    """Anything the gateway checks is part of the request, so it must survive."""
    headers = dr._request_headers(_har_request([
        ("x-requested-with", "XMLHttpRequest"),
        ("accept", "application/json"),
        ("referer", "https://online.example.com/"),
    ]))

    assert headers["x-requested-with"] == "XMLHttpRequest"
    assert headers["accept"] == "application/json"
    assert headers["referer"] == "https://online.example.com/"


def test_a_header_copied_from_a_cookie_says_which_cookie():
    """The agent must write "read this cookie", not paste a dead token.

    The recorded token expires with the recording session, so a literal copy
    would pass its own test and fail every real run.
    """
    headers = dr._request_headers(_har_request([
        ("cookie", "languageCode=en-US; q2token=abc123xyz; themeName=dfcu"),
        ("q2token", "abc123xyz"),
    ]))

    assert headers["q2token"] == "<the value of the 'q2token' cookie>"
    assert "abc123xyz" not in json.dumps(headers)


def test_cookies_are_not_replayed_as_a_header():
    """The browser context sends them; repeating them would bake in a session."""
    headers = dr._request_headers(_har_request([
        ("cookie", "q2token=abc123xyz"),
        ("accept", "application/json"),
    ]))

    assert "cookie" not in {k.lower() for k in headers}


def test_a_secret_we_cannot_source_keeps_its_name_but_not_its_value():
    """Name tells the agent something is required; the value isn't ours to ship."""
    headers = dr._request_headers(_har_request([
        ("authorization", "Bearer sk-live-9f3a"),
    ]))

    assert "authorization" in headers
    assert "sk-live-9f3a" not in headers["authorization"]


def test_headers_the_browser_sets_itself_are_left_out():
    """A local model reads every token it is given; noise costs it attention."""
    headers = dr._request_headers(_har_request([
        ("user-agent", "Mozilla/5.0"),
        ("sec-ch-ua-platform", '"macOS"'),
        ("accept-encoding", "gzip"),
        (":authority", "online.example.com"),
        ("x-requested-with", "XMLHttpRequest"),
    ]))

    assert list(headers) == ["x-requested-with"]


# ── the demonstration has to fit in the model's context ───────────────
#
# Field failure this guards: fifteen candidate requests each carrying a
# 12,000-char body made a 49,000-token demonstration. The harness runs against
# whatever model the operator chose, and that one died on its server's memory
# guard at 23,000 — so the build failed before the agent ever opened the file it
# was told to read first. A demonstration too big to read is worth nothing.


def _har_entries(n, body_size):
    return {"log": {"entries": [
        {
            "_resourceType": "xhr",
            "request": {"method": "GET", "url": f"https://portal.example.com/api/{i}",
                        "headers": [{"name": "x-requested-with", "value": "XMLHttpRequest"}]},
            "response": {"status": 200,
                         "content": {"mimeType": "application/json",
                                     "text": "x" * (body_size - i),
                                     "size": body_size - i}},
        }
        for i in range(n)
    ]}}


def test_only_the_likeliest_payloads_keep_a_full_body(tmp_path):
    """The agent is looking for ONE endpoint. The rest need to be identifiable,
    not transcribed."""
    har = tmp_path / "demo.har"
    har.write_text(json.dumps(_har_entries(10, 40_000)))

    requests = dr._extract_requests(har)

    full = [r for r in requests if len(r["response_body_sample"]) > dr._PROBE_BODY * 2]
    assert len(full) == dr._FULL_BODY_REQUESTS


def test_the_biggest_response_is_one_of_them(tmp_path):
    """Ranked biggest-first because the rows are usually the largest payload —
    so the budget has to be spent AFTER the ranking, not before it."""
    har = tmp_path / "demo.har"
    har.write_text(json.dumps(_har_entries(10, 40_000)))

    requests = dr._extract_requests(har)

    assert len(requests[0]["response_body_sample"]) > dr._PROBE_BODY * 2


def test_every_request_keeps_its_url_and_headers(tmp_path):
    """Cheap, and they're what a wrong guess gets corrected against — a probe
    body the agent can't act on is still a candidate it can rule out."""
    har = tmp_path / "demo.har"
    har.write_text(json.dumps(_har_entries(10, 40_000)))

    requests = dr._extract_requests(har)

    assert all(r["url"] for r in requests)
    assert all(r["request_headers"]["x-requested-with"] == "XMLHttpRequest" for r in requests)


def test_a_whole_demonstration_stays_readable(tmp_path):
    """The end the clipping exists to serve, pinned as a number: the worst case
    a portal can produce still has to fit alongside everything else in a turn."""
    har = tmp_path / "demo.har"
    har.write_text(json.dumps(_har_entries(30, 500_000)))

    size = len(json.dumps(dr._extract_requests(har)))

    assert size < 60_000, f"demonstration would be ~{size // 4:,} tokens"


# ── assembling the transcript ─────────────────────────────────────────
#
# The actions are read out of the page repeatedly, from several tabs and frames,
# while the operator is still working. Turning those overlapping reads back into
# one ordered transcript is this half's whole job — and getting it wrong is
# invisible, because a transcript with a hole in it looks exactly like a
# transcript of someone who did fewer things.


def _act(t, css, kind="click", **extra):
    return {"kind": kind, "t": t, "css": css, **extra}


def test_the_transcript_is_in_the_order_the_operator_worked():
    """Tabs interleave. The order the tab objects come back in is an artifact of
    the browser, not a record of what the human did — and a replay performed in
    that order sets the filters after clicking Generate."""
    store = {}
    dr._remember([_act(300, "#generate"), _act(400, "#export")], 1, store)
    dr._remember([_act(100, "#reports"), _act(200, "#basis")], 2, store)

    actions, _ = dr._merge_actions(store)

    assert [a["css"] for a in actions] == ["#reports", "#basis", "#generate", "#export"]


def test_re_reading_the_page_is_not_a_second_click():
    """Every poll re-reads the whole array; the operator clicked once."""
    store = {}
    for _ in range(5):
        dr._remember([_act(100, "#reports"), _act(200, "#basis")], 1, store)

    actions, _ = dr._merge_actions(store)

    assert len(actions) == 2


def test_a_navigation_that_empties_the_array_does_not_erase_the_clicks():
    """THE loss this replaces. The array lives in the document, so a hard
    navigation starts it over. Keeping the LONGEST array seen — the old rule —
    meant every click after the navigation was discarded until the new array
    grew past the old one's length, and the operator's real work happens after
    the navigation, on the report screen."""
    store = {}
    dr._remember([_act(100, "#login"), _act(200, "#user"), _act(300, "#submit")], 1, store)
    dr._remember([_act(400, "#generate")], 1, store)   # same frame, new document

    actions, _ = dr._merge_actions(store)

    assert [a["css"] for a in actions] == ["#login", "#user", "#submit", "#generate"]


def test_the_same_field_set_twice_is_two_actions():
    """Changing a value, navigating, and changing it back is a real sequence —
    only the timestamps tell it apart from one read twice."""
    store = {}
    dr._remember([_act(100, "#basis", "change", value="cash")], 1, store)
    dr._remember([_act(900, "#basis", "change", value="accrual")], 1, store)

    actions, _ = dr._merge_actions(store)

    assert [a["value"] for a in actions] == ["cash", "accrual"]


def test_an_unstamped_action_still_accumulates_to_the_longest_read():
    """A listener too old to stamp its actions has only position to go on, which
    is exactly the rule this replaced — so an old capture degrades to the old
    behaviour instead of multiplying."""
    store = {}
    for length in (1, 2, 3):
        dr._remember([{"kind": "click"}] * length, 7, store)

    actions, _ = dr._merge_actions(store)

    assert len(actions) == 3


def test_both_ends_of_a_long_demonstration_survive():
    """The middle is where the re-reading and the second-guessing live. The
    Generate click is the last thing that happens and the one the replay exists
    to reach — losing the tail to a size cap would defeat the whole capture."""
    store = {}
    dr._remember([_act(i, f"#step{i}") for i in range(1, 501)], 1, store)

    actions, dropped = dr._merge_actions(store)

    assert len(actions) == dr._MAX_ACTIONS
    assert dropped == 500 - dr._MAX_ACTIONS
    assert actions[0]["css"] == "#step1", "how they got there"
    assert actions[-1]["css"] == "#step500", "what they came to do"


def test_a_gapped_transcript_says_so(recorder):
    """A replay written from a transcript that silently skipped a step is worse
    than one written from an honest gap."""
    class _Chatty(_FakePage):
        def evaluate(self, script):
            if "visibilityState" in script:
                return "visible"
            return [_act(i, f"#step{i}") for i in range(1, 501)]

    demo, _ = recorder(_Chatty(closes_after=3))

    assert "actions_omitted" in demo
    assert len(demo["recorded_actions"]) == dr._MAX_ACTIONS


def test_clicks_inside_an_iframe_reach_the_agent(recorder):
    """A portal that renders its report in an iframe recorded the navigation TO
    the frame and nothing that happened in it: page.evaluate only ever reaches
    the main frame, so every click the operator made in the report was lost."""
    class _Framed(_FakePage):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.frames = [self, _Inner()]

    class _Inner:
        url = "https://portal.example.com/report-frame"

        def evaluate(self, script):
            return [_act(500, "#in-frame-generate", in_frame=True)]

    demo, _ = recorder(_Framed(closes_after=3))

    inside = [a for a in demo["recorded_actions"] if a.get("in_frame")]
    assert [a["css"] for a in inside] == ["#in-frame-generate"]


def test_a_page_without_frames_is_still_read(recorder):
    """Playwright hands us frames; the fakes and any other caller need not."""
    assert dr._action_sources(object()) != []


# ── what a single action carries ──────────────────────────────────────
#
# Field failure this guards: an action used to be a DESCRIPTION of the click —
# `{tag: 'BUTTON', cls: 'btn btn-primary', text: 'Generate'}` — which reads like
# a locator and is not one. The agent had to invent a selector from it, and an
# invented selector is why nobody wanted the click-replay fallback: it fails on
# the first live run, on a page the agent can no longer see.
#
# These are browser-backed, and deliberately. Every property below is a property
# of a REAL DOM — CSS.escape, computed cursors, an id that is unique until it
# isn't, a listener inside an iframe. Asserted against a fake they would all pass
# while the recorder shipped selectors that match nothing. CI installs no
# browser, so they skip there; they run locally, where anyone editing the
# recorder already has Chromium.

@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    try:
        launched = playwright.chromium.launch(headless=True)
    except Exception as exc:                                    # pragma: no cover
        playwright.stop()
        pytest.skip(f"no Chromium to record in: {exc}")
    yield launched
    launched.close()
    playwright.stop()


@pytest.fixture
def recorded(browser):
    """Load `html` in a real page with the real listener installed, do `act`,
    and return (page, the actions it logged) — read the way record() reads them,
    through _action_sources, so iframes count.

    Served from a routed https origin rather than a data: URL, because an init
    script does not run on a data: URL — which would make every test here pass
    or fail for a reason that has nothing to do with the recorder.
    """
    contexts = []

    def run(html, act):
        context = browser.new_context()
        contexts.append(context)
        context.add_init_script(dr._ACTION_LOGGER)
        context.route("**/*", lambda route: route.fulfill(
            status=200, content_type="text/html", body=html))
        page = context.new_page()
        page.goto("https://portal.test/demonstration")
        assert page.evaluate("() => window.__demo_hooked"), "the listener did not install"
        act(page)
        actions = []
        for source in dr._action_sources(page):
            actions.extend(source.evaluate("() => window.__demo_actions || []"))
        return page, actions

    yield run
    for context in contexts:
        context.close()


# Three identical buttons in a table, which is what a portal's report screen
# actually looks like: nothing to anchor on but position.
_ROWS = """
<div class="wrap"><div class="panel"><table><tbody>
  <tr><td><button class="view">View</button></td></tr>
  <tr><td><button class="view">View</button></td></tr>
  <tr><td><button class="view" data-row="3">View</button></td></tr>
</tbody></table></div></div>
"""


def test_a_recorded_selector_finds_exactly_the_element_that_was_clicked(recorded):
    """The one property the whole capture exists for. Everything else here is
    about not throwing this away."""
    page, (action,) = recorded(_ROWS, lambda page: page.click('button[data-row="3"]'))

    assert page.eval_on_selector_all(action["css"], "els => els.length") == 1
    assert page.eval_on_selector(action["css"], "el => el.dataset.row") == "3"


def test_the_xpath_finds_it_too(recorded):
    """The fallback has to be a real fallback: css anchors move when the page is
    redesigned, and an xpath that was never checked is not a second chance."""
    page, (action,) = recorded(_ROWS, lambda page: page.click('button[data-row="3"]'))

    found = page.evaluate(
        "xp => document.evaluate(xp, document, null, 7, null).snapshotLength", action["xpath"])
    assert found == 1


def test_an_id_the_framework_generated_is_not_trusted(recorded):
    """Angular, React and friends mint a new id on every render, so a selector
    anchored to one works exactly once — on the recording. It has to be checked
    by resolving it, not by reading it: `#\\:r3\\:` looks perfectly good."""
    html = """<div id="ng-1"><button id=":r3:" class="go">Generate</button></div>"""
    page, (action,) = recorded(html, lambda page: page.click(".go"))

    assert ":r3:" not in action["css"]
    assert page.eval_on_selector_all(action["css"], "els => els.length") == 1


def test_a_stable_id_is_used_when_there_is_one(recorded):
    """The best selector a page offers, and the shortest."""
    html = """<div><span><button id="generate-report">Generate</button></span></div>"""
    _, (action,) = recorded(html, lambda page: page.click("#generate-report"))

    assert action["css"] == "button#generate-report"


def test_the_click_is_attributed_to_the_control_not_the_pixel(recorded):
    """Buttons are built out of spans and icons. A selector for the span inside
    the button is a selector that stops working the moment the icon changes."""
    html = """<button id="go"><span class="icon"></span><span class="label">Generate</span></button>"""
    _, (action,) = recorded(html, lambda page: page.click(".label"))

    assert action["css"] == "button#go"
    assert action["tag"] == "BUTTON"


def test_clicking_the_page_is_not_an_action(recorded):
    """Most clicks in a demonstration land on nothing — dismissing a menu,
    putting focus somewhere, missing. Recording them made the transcript long
    enough to be worth skimming, and a skimmed transcript is a guessed replay."""
    html = """<div id="bg" style="width:300px;height:300px"></div>"""
    _, actions = recorded(html, lambda page: page.click("#bg"))

    assert actions == []


def test_a_div_dressed_as_a_button_is_still_an_action(recorded):
    """The other half of that judgement. Portals build controls out of divs, and
    dropping a real click is a worse failure than keeping a stray one — so the
    cursor decides, the same way the operator decided it was clickable."""
    html = """<div id="fake-btn" style="cursor:pointer">Generate</div>"""
    _, (action,) = recorded(html, lambda page: page.click("#fake-btn"))

    assert action["css"] == "div#fake-btn"


def test_a_password_is_never_written_down(recorded):
    """A demonstration is written to disk and read aloud to a model. Signing in
    is browser_session plus the credential store; it is not a keystroke to
    replay, so the value has no reason to exist here at all."""
    html = """<form><input id="pw" type="password" placeholder="Password"></form>"""
    page, actions = recorded(html, lambda page: page.fill("#pw", "hunter2-real-password"))

    assert actions, "the field being set is still part of the navigation"
    assert "hunter2" not in json.dumps(actions)
    assert actions[-1]["value"] == "<redacted: password field>"


def test_a_dropdown_records_both_what_they_picked_and_what_it_sends(recorded):
    """These become the two halves of a SETTINGS choice — `label` is what the
    operator will be shown, `value` is what the request has to carry — and the
    agent cannot recover one from the other."""
    html = """<select id="basis">
      <option value="A">Accrual basis</option><option value="C">Cash basis</option>
    </select>"""
    _, (action,) = recorded(html, lambda page: page.select_option("#basis", "C"))

    assert action["value"] == "C"
    assert action["option_label"] == "Cash basis"


def test_a_field_typed_into_records_the_value_they_settled_on(recorded):
    """A date field fires change as it is typed. The setting is what it ended up
    as; the rest is a transcript of someone typing."""
    html = """<input id="from" type="text">"""
    _, actions = recorded(html, lambda page: (page.fill("#from", "01/01/2026"),
                                              page.fill("#from", "06/01/2026")))

    assert [a["value"] for a in actions] == ["06/01/2026"]


def test_leaving_a_field_is_not_setting_it_again(recorded):
    """A field fires change a second time when it loses focus — which is when
    the operator clicks the NEXT control, so the echo lands after that control
    and reads as them going back to a field they were finished with. A replay
    written from that goes back too, re-opening a datepicker over the button it
    is about to click."""
    html = """<input id="from" type="text"><select id="basis"><option>Cash</option></select>"""
    _, actions = recorded(html, lambda page: (page.fill("#from", "06/01/2026"),
                                              page.select_option("#basis", index=0)))

    assert [a["css"] for a in actions] == ["input#from", "select#basis"]


def test_an_action_says_what_the_operator_read_on_the_screen(recorded):
    """The most durable locator of the three when the name is distinctive, and
    the only one that survives a redesign. It has to come from the accessible
    name, not innerText, or an icon-only button records as ''."""
    html = """
      <button aria-label="Generate report"><span class="icon"></span></button>
      <label for="basis">Accounting basis</label><select id="basis"><option>Cash</option></select>
    """
    page, actions = recorded(html, lambda page: (page.click("button"),
                                                 page.select_option("#basis", index=0)))

    assert (actions[0]["role"], actions[0]["name"]) == ("button", "Generate report")
    assert (actions[1]["role"], actions[1]["name"]) == ("combobox", "Accounting basis")


def test_an_action_inside_an_iframe_says_so(recorded):
    """A report viewer is an iframe more often than not, and page.click will
    never find what is inside one. The agent has to be told to enter the frame,
    and told which frame."""
    html = """<iframe id="report" srcdoc="<button id='gen'>Generate</button>"></iframe>"""
    _, actions = recorded(
        html, lambda page: page.frame_locator("#report").locator("#gen").click())

    inside = [a for a in actions if a.get("in_frame")]
    assert [a["css"] for a in inside] == ["button#gen"]


def test_every_action_is_stamped_and_placed(recorded):
    """Ordering across tabs is done on these, and a replay in the wrong order
    sets the filters after clicking Generate."""
    _, (action,) = recorded(_ROWS, lambda page: page.click('button[data-row="3"]'))

    assert isinstance(action["t"], int) and action["t"] > 0
    assert action["href"] == "https://portal.test/demonstration"
