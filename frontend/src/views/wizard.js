// The source-add wizard — the first concrete extraction out of legacy/app.js,
// demonstrating the pattern the remaining ~140 functions there will follow
// as modularization continues (see docs/frontend-migration notes / the
// promotion-log entry on the frontend+desktop split). Behavior is unchanged
// from the monolith — only where the code lives. It talks to the shared
// kernel (state, DOM helpers, callTool, renderIngest) still in app.js via
// ordinary ES module imports; app.js imports addSourceHTML back (renderIngest
// calls it to splice the wizard into the Ingest panel), so this and app.js
// are mutually dependent modules — fine under ES modules as long as neither
// side calls the other at module-EVALUATION time, only from event handlers
// after the page has loaded. (Verified by tests/test_web_dashboard.py and a
// real browser load, not just by this comment.)

import {
  wiz, methodsCache, STAGING, ingestOpen, emailRouteCache, demoState, credCache,
  emailCache, bs, esc, el, callTool, toast, clearToast, renderIngest, forgetSource,
} from "../legacy/app.js";

export function openWizard() { wiz.open = true; renderIngest(); }

export function closeWizard() {
    Object.assign(wiz, { open: false, step: 1, method: null, learning: false, preview: null,
                         label: '', fork: 'new', busy: false, error: '', carrier: null,
                         inbox: { from: '', subject: '', suffix: '.pdf', days: '' } });
    renderIngest();
  }

export function wizBack() {
    if (wiz.step === 3) { wiz.step = 2; wiz.preview = null; wiz.error = ''; }
    else { wiz.step = 1; wiz.method = null; wiz.error = ''; }
    renderIngest();
  }

  /** Clicking the box commits to that route and moves on — no second click. */
export function chooseMethod(id) {
    wiz.method = id; wiz.step = 2; wiz.preview = null; wiz.error = '';
    renderIngest();
  }

export function wizMethod() { return (methodsCache || []).find(m => m.id === wiz.method); }

export function addSourceHTML(sources) {
    if (!wiz.open) {
      return '<div class="row" style="margin-top:18px">' +
        '<button class="btn" onclick="openWizard()">+ Add a data source</button></div>';
    }
    const body = wiz.step === 1 ? wizStep1()
               : wiz.step === 2 ? wizStep2()
               : wizStep3(sources);
    return '<div class="build" style="margin-top:18px"><div class="build-h">' +
      '<span class="caret open">&#8250;</span>Add a data source' +
      '<button class="btn" style="margin-left:auto;font-size:12px;padding:4px 9px" ' +
      'onclick="closeWizard()">Cancel</button></div>' +
      '<div class="build-b">' + wizSteps() + body + '</div></div>';
  }

export function wizSteps() {
    const labels = [[1, 'How it arrives'], [2, 'Let it learn the source'], [3, 'Check it and name it']];
    return '<div class="wsteps">' + labels.map(([n, t], i) =>
      '<span class="wstep' + (wiz.step === n ? ' on' : (wiz.step > n ? ' done' : '')) + '">' +
      '<span class="n">' + (wiz.step > n ? '&#10003;' : n) + '</span>' + esc(t) + '</span>' +
      (i < 2 ? '<span class="warrow">&#8250;</span>' : '')).join('') + '</div>';
  }

  /** Step 1 — the three doors. Each says what it will cost you every month,
   *  which is the actual difference between them. */
export function wizStep1() {
    const methods = methodsCache || [];
    if (!methods.length) return '<div class="empty">Couldn’t load the ways in.</div>';
    return methods.map(m =>
      '<div class="wopt" onclick="chooseMethod(\'' + m.id + '\')">' +
        '<span class="wico">' + esc(m.icon) + '</span>' +
        '<span><div class="t">' + esc(m.label) + '</div><div class="d">' + esc(m.detail) + '</div></span>' +
        '<span class="w">' + esc(m.cost) + ' &#8250;</span></div>').join('');
  }

  /** Step 2 — hand it the source and let it look. Nothing is named yet. */
export function wizStep2() {
    const m = wizMethod();
    if (!m) return '';
    const err = wiz.error ? '<div class="verdict fail">' + esc(wiz.error) + '</div>' : '';

    if (wiz.learning) {
      return '<div class="doing"><span class="spin"></span>' + esc(m.doing) + '</div>' +
        (wiz.method === 'website'
          ? '<p class="sub" style="margin:10px 0 0">Closing the window is how you say you’re done — ' +
            'and it keeps whatever you actually visited, so there’s no address to type.</p>' : '');
    }

    if (wiz.method === 'email') return err + wizEmailStep(m);

    return err + '<p class="note" style="margin:0 0 4px">' + esc(m.doing) + '</p>' +
      (wiz.method === 'website'
        ? '<p class="sub" style="margin:0 0 12px">No address to type: it records where you go.</p>'
        : '<p class="sub" style="margin:0 0 12px">It works out the format itself — you don’t have to say.</p>') +
      '<input id="wiz-sample" type="file" style="display:none" accept=".pdf,.csv,text/csv,application/pdf" ' +
        'onchange="handleWizardSample(event)">' +
      '<div class="row"><button class="btn" onclick="wizBack()">Back</button>' +
      '<button class="btn primary" onclick="' +
        (wiz.method === 'website' ? 'startWizardDemo()' : 'chooseWizardSample()') + '">' +
        esc(m.act) + '</button></div>';
  }

  /** An inbox signs in with Google, and that lives in Settings — the same inbox
   *  can feed several sources, so it isn't owned by this flow. Here we only
   *  search one that's already connected, and read what it carries. */
export function wizEmailStep(m) {
    const inboxes = (credCache || []).filter(c => c.kind === 'inbox');
    const connected = inboxes.filter(c => (emailCache[c.key] || {}).connected);
    const caveat = m.caveat
      ? '<div class="recovery-why" style="margin-bottom:12px"><b>Worth knowing.</b> ' + esc(m.caveat) + '</div>' : '';

    if (!connected.length) {
      return caveat + '<p class="note" style="margin:0 0 12px">No inbox is signed in yet. An inbox signs in ' +
        'with Google rather than a password, and that lives in <b>Settings</b> — the same inbox can feed ' +
        'more than one source, so it isn’t owned by this flow.</p>' +
        '<div class="row"><button class="btn" onclick="wizBack()">Back</button>' +
        '<button class="btn primary" onclick="closeWizard();show(\'settings\')">Open Settings</button></div>';
    }

    const chosen = wiz.carrier || connected[0].key;
    return caveat +
      '<div class="field"><label for="wiz-inbox">Which inbox</label>' +
      '<select id="wiz-inbox" onchange="wiz.carrier=this.value">' +
      connected.map(c => '<option value="' + esc(c.key) + '"' + (c.key === chosen ? ' selected' : '') + '>' +
        esc((emailCache[c.key] || {}).account_email || c.label) + '</option>').join('') + '</select></div>' +
      '<div class="field"><label for="wiz-from">From address <span class="sub">(optional)</span></label>' +
      '<input type="text" id="wiz-from" value="' + esc(wiz.inbox.from) + '" placeholder="statements@example.com"></div>' +
      '<div class="field"><label for="wiz-subj">Subject contains <span class="sub">(optional)</span></label>' +
      '<input type="text" id="wiz-subj" value="' + esc(wiz.inbox.subject) + '" placeholder="Statement"></div>' +
      '<div class="field"><label for="wiz-suffix">Attachment ends with</label>' +
      '<input type="text" id="wiz-suffix" value="' + esc(wiz.inbox.suffix) + '"></div>' +
      '<div class="row"><button class="btn" onclick="wizBack()">Back</button>' +
      '<button class="btn primary" onclick="lookInInbox()">Look in that inbox</button></div>';
  }

  /** Step 3 — what it found, then (and only then) what to call it. */
export function wizStep3(sources) {
    const p = wiz.preview || {};
    const targets = sources.filter(s => !s.is_trigger);
    const trouble = p.trouble
      ? '<div class="recovery-why" style="margin-top:10px">' + esc(p.trouble) + '</div>' : '';

    const cols = (p.columns || []).length
      ? (p.columns || []).map(c => '<span class="tag">' + esc(c) + '</span>').join(' ')
      : '<span style="color:var(--faint)">none read yet</span>';

    const found = '<div class="lbl">What it found</div>' +
      '<div class="wfound">' +
      wfLine('Where it came from', esc(p.where || '—')) +
      wfLine('How it gets it', '<span style="color:var(--muted)">' + esc(p.how || '—') + '</span>') +
      // A document's row count is the model's estimate from one look, not a
      // parse — say so, rather than let it read as a counted fact.
      wfLine('What it read', (p.rows_estimated ? 'about ' : '') + (p.rows || 0) + ' rows' +
        (p.span ? ' · ' + esc(p.span) : '') +
        (p.rows_estimated ? '<div class="sub">from one quick look — the parser counts them properly</div>' : '')) +
      wfLine('Its columns', cols) +
      wfLine('Afterwards', '<span style="color:var(--muted)">' +
        (p.unattended ? 'can run unattended' : 'needs you to hand it each file') + '</span>') +
      '</div>' + trouble;

    const nameHint = p.name_source === 'model'
      ? 'Suggested from what it just read — change it to whatever you call it.'
      : 'Suggested from the file name — change it to whatever you call it.';
    const nameField = '<div class="field" style="margin-top:14px"><label for="wiz-label">Call it</label>' +
      '<input type="text" id="wiz-label" value="' + esc(wiz.label) + '" autocomplete="off" ' +
      'oninput="wiz.label=this.value"><span class="sub">' + nameHint + '</span></div>';

    // The question that stops a second door being modelled as a second source.
    const fork = '<div class="lbl">Is this new, or another way into something you have?</div>' +
      forkOptHTML('new', 'A new source', 'Nothing here covers this data yet.') +
      targets.map(t => forkOptHTML(t.key, 'Another way into ' + t.label,
        (t.transports || []).filter(r => r.available).map(r => r.label.toLowerCase()).join(', ') ||
        'no working route yet')).join('');

    const isNew = wiz.fork === 'new';
    const err = wiz.error ? '<div class="verdict fail">' + esc(wiz.error) + '</div>' : '';
    return err + found + (isNew ? nameField : '') + fork +
      '<div class="row"><button class="btn" onclick="wizBack()">Back</button>' +
      '<button class="btn primary"' + (wiz.busy ? ' disabled' : '') + ' onclick="saveWizard()">' +
      (wiz.busy ? 'Saving…' : (isNew ? 'Save this source' : 'Add this route')) + '</button></div>';
  }

export function wfLine(k, v) {
    return '<div class="wfline"><span class="k">' + esc(k) + '</span><span class="v">' + v + '</span></div>';
  }

export function forkOptHTML(key, title, detail) {
    return '<div class="wopt' + (wiz.fork === key ? ' on' : '') + '" onclick="wiz.fork=\'' + key + '\';renderIngest()">' +
      '<span class="wradio"></span><span><div class="t">' + esc(title) + '</div>' +
      '<div class="d">' + esc(detail) + '</div></span></div>';
  }

  /* ── step 2 actions: hand it the source, let it look ── */

export function chooseWizardSample() { const i = el('wiz-sample'); if (i) i.click(); }

export async function handleWizardSample(event) {
    const input = event && event.target;
    const file = input && input.files && input.files[0];
    if (!file) return;
    wiz.learning = true; wiz.error = ''; clearToast();
    await renderIngest();
    const form = new FormData();
    form.append('file', file);
    try {
      // Staged under a placeholder key: the source has no name yet — working out
      // what to call it is the point of reading the document.
      const res = await fetch('/api/upload_sample/' + STAGING, { method: 'POST', body: form });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error((data.detail && data.detail.message) || data.detail || 'upload failed');
      wiz.preview = await callTool('preview_document', { sample_path: data.sample_path, filename: file.name });
      wiz.label = wiz.preview.suggested_name || '';
      wiz.step = 3;
    } catch (e) {
      wiz.error = 'Couldn’t read that document: ' + e.message;
    } finally {
      wiz.learning = false;
      if (input) input.value = '';
      await renderIngest();
    }
  }

export async function startWizardDemo() {
    wiz.learning = true; wiz.error = ''; clearToast();
    await renderIngest();
    try {
      await callTool('start_demo', { source_key: STAGING });
      pollWizardDemo();
    } catch (e) {
      wiz.learning = false; wiz.error = 'Couldn’t open the browser: ' + e.message;
      await renderIngest();
    }
  }

export function pollWizardDemo() {
    const tick = async () => {
      let d;
      try { d = await callTool('demo_status', { source_key: STAGING }); }
      catch (e) { wiz.learning = false; wiz.error = 'Lost contact with the demonstration: ' + e.message; await renderIngest(); return; }
      if (d.status === 'running') { setTimeout(tick, 2000); return; }
      if (d.status !== 'completed') {
        wiz.learning = false; wiz.error = d.message || 'The demonstration wasn’t captured.';
        await renderIngest();
        return;
      }
      try {
        wiz.preview = await callTool('preview_demo', { demo_path: d.demo_path });
        wiz.label = wiz.preview.suggested_name || '';
        wiz.step = 3;
      } catch (e) {
        wiz.error = 'Captured it, but couldn’t read it back: ' + e.message;
      }
      wiz.learning = false;
      await renderIngest();
    };
    setTimeout(tick, 2000);
  }

export async function lookInInbox() {
    const sel = el('wiz-inbox');
    wiz.carrier = sel ? sel.value : wiz.carrier;
    wiz.inbox = {
      from: el('wiz-from').value, subject: el('wiz-subj').value,
      suffix: el('wiz-suffix').value, days: '',
    };
    wiz.learning = true; wiz.error = ''; clearToast();
    await renderIngest();
    try {
      wiz.preview = await callTool('preview_inbox', {
        carrier_key: wiz.carrier, from_address: wiz.inbox.from,
        subject_contains: wiz.inbox.subject, attachment_suffix: wiz.inbox.suffix,
      });
      wiz.label = wiz.preview.suggested_name || '';
      wiz.step = 3;
    } catch (e) {
      wiz.error = 'Couldn’t search that inbox: ' + e.message;
    } finally {
      wiz.learning = false;
      await renderIngest();
    }
  }

  /* ── step 3 action: save ── */

export async function saveWizard() {
    const labelEl = el('wiz-label');
    if (labelEl) wiz.label = labelEl.value;
    wiz.busy = true; wiz.error = ''; clearToast();
    await renderIngest();

    const p = wiz.preview || {};
    try {
      // "Another way into X" adds a route to a source that already exists —
      // it must NOT create a second source for the same body of data.
      const emailArgs = wiz.method === 'email'
        ? { carrier: wiz.carrier, from_address: wiz.inbox.from,
            subject_contains: wiz.inbox.subject, attachment_suffix: wiz.inbox.suffix }
        : {};
      const key = wiz.fork === 'new'
        ? (await callTool('add_source', {
            label: wiz.label, method: wiz.method, adopt_staged: true,
            login_url: wiz.method === 'website' ? (p.where || '') : '',
            ...emailArgs,
          })).source_key
        : wiz.fork;

      // "Another way into X" opens the chosen door on the source that already
      // exists, so the Ingest screen shows it — without this it stayed hidden
      // even though a sample/demo had just been attached for it below.
      if (wiz.fork !== 'new') {
        if (wiz.method === 'email') {
          await callTool('save_email_search', { source_key: key, ...emailArgs });
        } else {
          await callTool('request_transport', { source_key: key, method: wiz.method });
        }
      }
      if (wiz.method === 'email') delete emailRouteCache[key];

      // Hand what it learned to that source's own panel, which is where code
      // gets built for every source — there is no second build flow in here.
      const st = bs(key);
      if (p.demo_path) demoState[key] = { status: 'completed', demo_path: p.demo_path,
                                          login_url: p.where, captured_requests: p.requests,
                                          recorded_actions: p.actions };
      if (p.sample_path) st.sample = { sample_path: p.sample_path, filename: p.where, bytes: 0 };

      // Read these BEFORE closing — closeWizard resets the state they live in.
      const label = wiz.label, wasNew = wiz.fork === 'new';
      closeWizard();
      ingestOpen.key = key;
      forgetSource(key);
      await renderIngest();
      toast(wasNew ? ('Added ' + label + '. Next: it writes the code, and you approve it.')
                   : 'Added another way into ' + key + '.', true);
    } catch (e) {
      wiz.busy = false; wiz.error = e.message;
      await renderIngest();
    }
  }

// ── Global exports for inline onclick/onchange handlers in index.html's
// dynamically-rendered wizard markup — see the equivalent block at the
// bottom of legacy/app.js for why this is necessary under ES modules.
//
// `wiz` itself is NOT exported here even though this file's markup mutates it
// directly (`onchange="wiz.carrier=this.value"`) — it's window-exported by
// legacy/app.js instead, where it's declared. Doing it here would read the
// imported `wiz` binding at THIS module's top-level evaluation time, and the
// app.js <-> wizard.js import cycle means app.js's own `const wiz = ...`
// hasn't necessarily run yet at that point — a real
// "Cannot access 'wiz' before initialization" crash, not a hypothetical one.
// Reachable-from-window is still covered: it's the same object either way,
// and tests/test_web_dashboard.py::test_every_inline_handler_is_reachable_from_window
// checks the union of every file's export block, not per-file completeness.
Object.assign(window, {
  openWizard, closeWizard, wizBack, chooseMethod, chooseWizardSample,
  handleWizardSample, startWizardDemo, lookInInbox, saveWizard,
});
