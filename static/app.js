/* olliellama — client-side glue.
 *
 * Everything in here used to live in inline <script> blocks or hx-on::*
 * attributes scattered across the templates. Consolidating buys us:
 *   - readable multi-line handlers (no &quot;-escaped HTML in attrs)
 *   - one place to grep for client behaviour
 *   - room for new features without growing more inline JS
 *
 * The one piece that MUST stay inline (in base.html) is the synchronous
 * IIFE that reads localStorage and sets <html data-theme> before any
 * paint — running it from a deferred external script would re-introduce
 * the white flash on dark-mode load.
 *
 * Loaded as `type="module"` in base.html, so:
 *   - Defer is implicit (executes after DOM parse, before DOMContentLoaded).
 *   - Top-level `function` and `const` declarations stay scoped to the
 *     module — none of them land on `window`.
 *   - Strict mode is on by default.
 * The two HTMX bundles still load as classic `defer` scripts; their
 * globals (`htmx`, etc.) remain accessible to us because classic globals
 * are visible to modules, modules just don't add to them.
 *
 * All listeners are delegated on `document.body` or `document` so
 * HTMX-swapped content picks them up without a re-bind.
 */

// ---------------------------------------------------------------------
// Theme toggle
// ---------------------------------------------------------------------
// The inline IIFE in base.html already set data-theme before paint.
// Our job here is to wire the button click and to sync the icon glyph
// to match (the icon shows the theme the click will switch TO — moon =
// currently light, sun = currently dark).

function syncThemeIcon() {
  const icon = document.querySelector('.theme-toggle__icon');
  if (!icon) return;
  const theme = document.documentElement.getAttribute('data-theme');
  icon.textContent = theme === 'dark' ? 'light_mode' : 'dark_mode';
}

function toggleTheme() {
  const html = document.documentElement;
  const next = html.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  html.setAttribute('data-theme', next);
  localStorage.setItem('theme', next);
  syncThemeIcon();
}

// ---------------------------------------------------------------------
// Sidebar aria-current updater
// ---------------------------------------------------------------------
// The server sets aria-current="page" on the active chat row only for
// full-page renders. HTMX swaps replace #main but leave the sidebar
// alone, so without this delegated handler the highlighted row would
// drift out of sync with the panel content.

// Phase 17b: the sidebar's #projects-list reuses .chat-item classes for
// each project row (alongside the .project-item marker). The selectors
// here cover both chat rows (legacy sidebar before phase 17b) and the
// project rows that replaced them in the unified sidebar.

function clearSidebarAriaCurrent() {
  document
    .querySelectorAll('#chats-list .chat-item[aria-current], #projects-list .chat-item[aria-current]')
    .forEach((li) => li.removeAttribute('aria-current'));
}

document.addEventListener('click', (e) => {
  const sidebarLink = e.target.closest(
    '#chats-list .chat-item a, #projects-list .chat-item a'
  );
  if (sidebarLink) {
    // On mobile the sidebar is an overlay drawer; navigating should close it
    // so the freshly-swapped #main isn't hidden behind it.
    closeSidebarDrawer();
    clearSidebarAriaCurrent();
    sidebarLink.closest('.chat-item').setAttribute('aria-current', 'page');
    // When clicking a chat from the project body (not the sidebar), keep the
    // active project row highlighted — clearSidebarAriaCurrent() just removed it.
    if (!sidebarLink.closest('#projects-list')) {
      const projectPage = document.querySelector('.project-page[data-project-id]');
      if (projectPage) {
        const projectRow = document.querySelector(
          `#projects-list [data-project-id="${projectPage.dataset.projectId}"]`
        );
        if (projectRow) projectRow.setAttribute('aria-current', 'page');
      }
    }
    return;
  }
  if (e.target.closest('.sidebar__new-chat, .sidebar__settings')) {
    closeSidebarDrawer();
    clearSidebarAriaCurrent();
  }
});

// ---------------------------------------------------------------------
// Mobile sidebar drawer
// ---------------------------------------------------------------------
// On viewports <= 768px the sidebar is hidden off-canvas (see the
// @media block in style.css). The fixed hamburger button toggles
// `.layout--sidebar-open`, which slides it in; a full-screen scrim dims
// #main and closes the drawer on tap. These handlers are inert on
// desktop because the toggle button + scrim are display:none there, so
// they're never clicked and the open class is never applied.
//
// The toggle/scrim/sidebar elements live in index.html as siblings of
// #main, so they survive its HTMX innerHTML swaps and these delegated
// listeners keep working without a re-bind.

function setSidebarDrawer(open) {
  const layout = document.querySelector('.layout');
  if (!layout) return;
  layout.classList.toggle('layout--sidebar-open', open);
  const scrim = layout.querySelector('.layout__scrim');
  if (scrim) scrim.hidden = !open;
  const toggle = layout.querySelector('.layout__menu-toggle');
  if (toggle) toggle.setAttribute('aria-expanded', String(open));
}

function closeSidebarDrawer() {
  setSidebarDrawer(false);
}

document.addEventListener('click', (e) => {
  if (e.target.closest('.layout__menu-toggle')) {
    const layout = document.querySelector('.layout');
    setSidebarDrawer(!layout?.classList.contains('layout--sidebar-open'));
    return;
  }
  if (e.target.closest('.layout__scrim')) {
    closeSidebarDrawer();
    return;
  }
  // The header gear (mobile) reveals/hides the per-chat controls panel.
  const gear = e.target.closest('.chat-panel__controls-toggle');
  if (gear) {
    const panel = document.getElementById(
      gear.getAttribute('aria-controls')
    );
    if (panel) {
      const open = panel.classList.toggle('chat-panel__controls--open');
      gear.setAttribute('aria-expanded', String(open));
    }
  }
});

// Escape closes the drawer (mirrors the scrim tap on touch).
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeSidebarDrawer();
});

// ---------------------------------------------------------------------
// Chat panel: scroll to bottom on mount / on new content
// ---------------------------------------------------------------------
// Two trigger paths:
//   - Initial page load (server-rendered chat panel inside #main).
//   - HTMX swap of #main when the user picks a chat from the sidebar,
//     OR swap into #messages when a new message/token lands.

function scrollMessagesToBottom() {
  const m = document.getElementById('messages');
  if (m) m.scrollTop = m.scrollHeight;
}

document.addEventListener('DOMContentLoaded', () => {
  syncThemeIcon();
  const themeBtn = document.querySelector('.theme-toggle');
  if (themeBtn) themeBtn.addEventListener('click', toggleTheme);
  scrollMessagesToBottom();
  // Typeset math in the server-rendered chat panel present on first paint.
  typesetMath(document.body);
});

// One delegated htmx:afterSwap handler with two responsibilities:
//  1. Scroll the messages region to the bottom whenever new content
//     lands inside it (or the chat panel itself was just mounted).
//     The replaced `hx-on::after-swap` lived on #messages itself, so
//     it fired for ANY swap inside #messages via event bubbling:
//     streaming tokens, OOB-replacing the placeholder with the
//     persisted bubble, appending tool-rows, freezing tool-rows, etc.
//     Match that scope here with `target.closest('#messages')`.
//  2. Re-select a <select>'s saved value (data-default) after HTMX
//     populates its options lazily. Used by the composer and project-
//     settings model dropdowns once /models has replied.
document.body.addEventListener('htmx:afterSwap', (e) => {
  const target = e.detail.target;
  if (!(target instanceof Element)) return;

  if (
    target.id === 'main' ||
    target.id === 'messages' ||
    target.closest('#messages')
  ) {
    scrollMessagesToBottom();
    // Typeset math in whatever just landed: the mounted chat panel (#main)
    // or a re-rendered #messages (compact). A no-op on plain token swaps
    // (marked already rendered their math). The persisted bubble the `done`
    // event swaps in arrives out-of-band (hx-swap-oob), which fires
    // htmx:oobAfterSwap, not this event — handled separately below.
    typesetMath(target);
  }

  if (target instanceof HTMLSelectElement && target.dataset.default) {
    const want = target.dataset.default;
    for (const opt of target.options) {
      if (opt.value === want) {
        target.value = want;
        break;
      }
    }
  }

  // Phase 25: once the composer model options land (initial load or host
  // switch), show/hide the Think chip for whatever model is now selected.
  if (target instanceof HTMLSelectElement && target.id === 'composer-model') {
    syncComposerThink();
  }
});

// The `done` SSE event (and the synthetic done on reload-mid-generation)
// delivers the persisted, server-rendered assistant bubble as an out-of-band
// swap (hx-swap-oob="outerHTML", see templates/_message.html) that replaces the
// streaming placeholder. htmx fires htmx:oobAfterSwap — NOT htmx:afterSwap — for
// OOB swaps, firing it on the swapped-IN element, so the handler above never
// sees the new bubble. Without this, the server bubble's raw \(...\)/\[...\]
// (in .arithmatex elements) replaces the marked-typeset stream and reverts to
// literal text until a reload. Re-typeset the element that just landed. Safe on
// the tool-card OOB swaps that share this event: no math, and renderMathInElement
// is a no-op on already-rendered KaTeX.
document.body.addEventListener('htmx:oobAfterSwap', (e) => {
  const swapped = e.target;
  if (swapped instanceof Element && swapped.closest('#messages')) {
    typesetMath(swapped);
  }
});

// ---------------------------------------------------------------------
// Tool-card live tick driver
// ---------------------------------------------------------------------
// Updates the elapsed text on every live-ticking tool row once per
// second. A row is "live ticking" when it has data-elapsed-start and
// no data-elapsed-final — the moment the server OOB-replaces a row
// with a frozen variant (data-elapsed-final set), the selector stops
// matching it and the timer freezes. setInterval lives for the page's
// lifetime; the matcher is cheap when there are no live rows.

function tickToolRows() {
  const now = Date.now();
  document
    .querySelectorAll('.tool-row[data-elapsed-start]:not([data-elapsed-final])')
    .forEach((row) => {
      const elapsedMs = now - Number(row.dataset.elapsedStart);
      const s = Math.floor(elapsedMs / 1000);
      const display = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
      const el = row.querySelector('.tool-row__elapsed');
      if (el) el.textContent = display;
    });
}

setInterval(tickToolRows, 1000);

// ---------------------------------------------------------------------
// Input pill: autogrow + Enter-to-submit
// ---------------------------------------------------------------------
// Chrome 123+ handles autogrow via `field-sizing: content` in style.css;
// this is the fallback for Safari / Firefox. Both the empty-state
// composer and the in-chat message form share `_input_pill.html`, and
// delegation on document.body picks them up regardless of which one
// HTMX last swapped in.
//
// Enter submits, Shift+Enter inserts a newline, IME composition
// (`event.isComposing`) is respected so CJK input isn't cut short.

document.body.addEventListener('input', (e) => {
  const ta = e.target;
  if (!(ta instanceof HTMLTextAreaElement)) return;
  if (!ta.matches('.input-pill textarea')) return;
  ta.style.height = 'auto';
  ta.style.height = ta.scrollHeight + 'px';
});

document.body.addEventListener('keydown', (e) => {
  if (!(e.target instanceof HTMLTextAreaElement)) return;
  if (!e.target.matches('.input-pill textarea')) return;
  if (e.key !== 'Enter' || e.shiftKey || e.isComposing) return;
  e.preventDefault();
  e.target.form?.requestSubmit();
});

// ---------------------------------------------------------------------
// HTMX form lifecycle handlers
// ---------------------------------------------------------------------
// htmx:beforeRequest / htmx:afterRequest fire on the element that
// initiated the request. We delegate on document.body and branch by
// form class so two unrelated forms (.message-form, .rag-server-form)
// can share the same pair of listeners.

function setHealthIcon(container, state) {
  // state: 'ok' | 'fail'.
  // Building the glyph via DOM APIs rather than innerHTML keeps us
  // safe from accidental injection if the state ever comes from data.
  if (!container) return;
  container.replaceChildren();
  const span = document.createElement('span');
  span.className = `material-symbols-outlined health-icon__${state}`;
  span.setAttribute('aria-label', state === 'ok' ? 'Healthy' : 'Failed');
  span.textContent = state === 'ok' ? 'check_circle' : 'cancel';
  container.appendChild(span);
}

document.body.addEventListener('htmx:beforeRequest', (e) => {
  if (!(e.target instanceof Element)) return;
  if (e.target.matches('.rag-server-form')) {
    // Clear any ✓/✗ from the previous submit so the icon slot doesn't
    // briefly show stale state while the new probe runs.
    const icon = document.getElementById('health-check-icon');
    if (icon) icon.replaceChildren();
  }
});

document.body.addEventListener('htmx:afterRequest', (e) => {
  const form = e.target;
  if (!(form instanceof HTMLFormElement)) return;

  if (form.matches('.message-form')) {
    // Reset only on success — a failed POST (e.g. 404 because the
    // conversation was deleted in another tab) keeps the typed
    // message so the user can see what didn't go through.
    if (!e.detail.successful) return;
    form.reset();
    const ta = form.querySelector('textarea');
    // Clear the inline height the autogrow handler set, so the now-
    // empty textarea collapses back to one row on Safari/Firefox.
    if (ta) ta.style.height = '';
    return;
  }

  if (form.matches('.rag-server-form')) {
    const err = document.getElementById('rag-server-form-error');
    const icon = document.getElementById('health-check-icon');
    if (e.detail.successful) {
      form.reset();
      if (err) {
        err.hidden = true;
        err.textContent = '';
      }
      setHealthIcon(icon, 'ok');
    } else {
      // 4xx / 5xx bodies are plain text ("Server name 'x' already in
      // use.") — textContent is the right escape boundary here.
      if (err) {
        err.textContent =
          e.detail.xhr.responseText || 'Failed to add server.';
        err.hidden = false;
      }
      setHealthIcon(icon, 'fail');
    }
  }

  if (form.matches('.rag-server__edit')) {
    // On failure (4xx/5xx) HTMX leaves the edit form in place; surface
    // the plain-text reason in the inline error div so the user knows
    // what to fix.  On success HTMX swaps the whole <li> back to view
    // mode, so no cleanup is needed here.
    if (!e.detail.successful) {
      const err = form.querySelector('.rag-server__edit-error');
      if (err) {
        err.textContent =
          e.detail.xhr.responseText || 'Failed to save server.';
        err.hidden = false;
      }
    }
  }
});

// ---------------------------------------------------------------------
// Composer: machine picker drives the single model dropdown
// ---------------------------------------------------------------------
// The composer has ONE model <select> shared by every machine. When the user
// switches machines, re-fetch that machine's installed models into the same
// dropdown and pre-select the machine's default model. The host <option>s
// carry data-default-model (see _host_select.html); we copy the selected
// one onto the model select's data-default so the existing htmx:afterSwap
// handler (responsibility #2) re-selects it once the options land. Delegated
// on document.body so it survives the composer being HTMX-swapped in.
document.body.addEventListener('change', (e) => {
  const host = e.target;
  if (!(host instanceof HTMLSelectElement) || host.id !== 'composer-host') return;
  const modelSelect = document.getElementById('composer-model');
  if (!modelSelect) return;
  const opt = host.selectedOptions[0];
  modelSelect.dataset.default = (opt && opt.dataset.defaultModel) || '';
  modelSelect.innerHTML = '<option value="">Loading models…</option>';
  htmx.ajax('GET', '/models?host=' + encodeURIComponent(host.value), {
    target: modelSelect,
    swap: 'innerHTML',
  });
});

// Phase 25: gate the composer Think chip on the selected model's capability.
// The /models options carry data-thinking (see _model_options.html); when the
// user picks a model whose option says data-thinking="true" we reveal the
// Think select, otherwise we hide it and reset it to 'default' so a stale
// 'off' from a previous model can't ride along on submit. Delegated on
// document.body so it survives the composer being HTMX-swapped in.
function syncComposerThink() {
  const modelSelect = document.getElementById('composer-model');
  const chip = document.getElementById('composer-think-chip');
  if (!modelSelect || !chip) return;
  const opt = modelSelect.selectedOptions[0];
  const canThink = !!(opt && opt.dataset.thinking === 'true');
  chip.hidden = !canThink;
  if (!canThink) {
    const thinkSelect = document.getElementById('composer-think-mode');
    if (thinkSelect) thinkSelect.value = 'default';
  }
}

document.body.addEventListener('change', (e) => {
  if (e.target instanceof HTMLSelectElement && e.target.id === 'composer-model') {
    syncComposerThink();
  }
});

// (data-default re-selection lives inside the single htmx:afterSwap
// handler above — see responsibility #2.)

// ---------------------------------------------------------------------
// LaTeX math typesetting (KaTeX)
// ---------------------------------------------------------------------
// Assistant text reaches the DOM by two routes, and the LaTeX is in a
// different shape on each:
//
//   1. Persisted bubbles (full page load + the `done` OOB swap) are rendered
//      server-side by pymdownx.arithmatex, which normalises every delimiter
//      style ($...$, $$...$$, \(...\), \[...\]) to \(...\) / \[...\] wrapped in
//      `.arithmatex` elements. We typeset those in the browser with KaTeX
//      auto-render (`renderMathInElement`) — see typesetMath() + its calls in
//      the afterSwap / DOMContentLoaded handlers.
//   2. The streaming buffer is rendered client-side by `marked`. We teach
//      marked about math (below) so the LaTeX survives its tokenizer — without
//      this, `$x_i$` becomes `$x<em>i</em>$` — and renders straight to KaTeX
//      HTML on every token.
//
// Both routes call into the SAME vendored katex.min.js, so a given expression
// looks identical whether it's mid-stream or reloaded from the DB.

const KATEX_OPTS = { throwOnError: false };

// Backslash forms first: arithmatex normalises every WELL-FORMED math span to
// these for persisted bubbles. We add `$$...$$` (display only) as a fallback so
// display blocks arithmatex's BLOCK processor skips — `$$` that doesn't start
// its own block (no blank line before it, or indented inside a list item) — get
// typeset in place rather than leaking raw `$$` onto the page. Single `$` stays
// OUT so a stray dollar in prose ("it costs $5") is never mistaken for math;
// double `$$` is far less likely to occur literally. renderMathInElement skips
// <code>/<pre> by default, so fenced math is untouched either way.
const KATEX_DELIMITERS = [
  { left: '\\(', right: '\\)', display: false },
  { left: '\\[', right: '\\]', display: true },
  { left: '$$', right: '$$', display: true },
];

// Typeset any \(...\)/\[...\] left as raw text inside `el` (the arithmatex
// span output). A no-op on already-rendered KaTeX (its HTML contains no raw
// delimiters) and on the streaming buffer (marked rendered its math already),
// so it's safe to call on every swap.
function typesetMath(el) {
  if (!(el instanceof Element)) return;
  if (typeof renderMathInElement === 'undefined') return;
  try {
    renderMathInElement(el, { delimiters: KATEX_DELIMITERS, ...KATEX_OPTS });
  } catch {
    // A malformed expression shouldn't throw out of a swap handler and blank
    // the message; leave the raw \(...\) text visible instead.
  }
}

// marked-katex-extension handles `$...$` / `$$...$$`. It does NOT cover the
// backslash delimiters, so this small companion extension tokenizes \(...\)
// (inline) and \[...\] (display) and renders them with KaTeX too. Inline-level
// is enough: a standalone \[ ... \] block is a single paragraph to marked, so
// the inline tokenizer still sees and consumes it. marked runs extension
// tokenizers before its built-in `escape` rule, so `\(` isn't pre-eaten into
// a literal `(`.
const backslashKatex = {
  extensions: [
    {
      name: 'inlineParenKatex',
      level: 'inline',
      start(src) {
        const i = src.indexOf('\\(');
        return i < 0 ? undefined : i;
      },
      tokenizer(src) {
        const m = /^\\\(([\s\S]+?)\\\)/.exec(src);
        if (m) return { type: 'inlineParenKatex', raw: m[0], text: m[1].trim() };
      },
      renderer(token) {
        return katex.renderToString(token.text, { ...KATEX_OPTS, displayMode: false });
      },
    },
    {
      name: 'inlineBracketKatex',
      level: 'inline',
      start(src) {
        const i = src.indexOf('\\[');
        return i < 0 ? undefined : i;
      },
      tokenizer(src) {
        const m = /^\\\[([\s\S]+?)\\\]/.exec(src);
        if (m) return { type: 'inlineBracketKatex', raw: m[0], text: m[1].trim() };
      },
      renderer(token) {
        return katex.renderToString(token.text, { ...KATEX_OPTS, displayMode: true });
      },
    },
  ],
};

if (typeof marked !== 'undefined') {
  if (typeof markedKatex !== 'undefined') marked.use(markedKatex(KATEX_OPTS));
  if (typeof katex !== 'undefined') marked.use(backslashKatex);
}

// ---------------------------------------------------------------------
// Incremental markdown rendering for streaming assistant tokens
// ---------------------------------------------------------------------
// Default htmx-ext-sse behaviour for the assistant placeholder's
// `sse-swap="token,..."` is a `beforeend` append of each chunk's
// html-escaped text. That means the user sees plain text accumulating
// during the stream, then a sudden plain→formatted flip when `done`
// swaps in the markdown-rendered bubble.
//
// We intercept `token` events before htmx swaps them: keep an in-memory
// raw-markdown buffer keyed off the placeholder element, re-render with
// marked on each chunk, and write the rendered HTML into the placeholder.
// Other SSE events (done, error, title, tool-call, tool-result) pass
// through htmx unmodified — the htmx:sseBeforeMessage handler bails out
// for anything other than `token` on a streaming placeholder.
//
// The buffer lives in a WeakMap so it's GC'd when the placeholder is
// replaced by the persisted bubble on `done` (outerHTML OOB swap drops
// the element from the DOM).

const streamBuffers = new WeakMap();

function unescapeHtml(s) {
  // Inverse of Python's html.escape() that generation.py runs on each
  // chunk before emitting it as the `token` event payload.
  return s
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#x27;/g, "'");
}

document.body.addEventListener('htmx:sseBeforeMessage', (e) => {
  const elt = e.target;
  if (!(elt instanceof Element)) return;
  if (!elt.classList.contains('message--streaming')) return;
  const sseEvent = e.detail;
  if (!sseEvent || sseEvent.type !== 'token') return;

  const prev = streamBuffers.get(elt) || '';
  const next = prev + unescapeHtml(sseEvent.data);
  streamBuffers.set(elt, next);

  // Wrap in `.message__content` so the same CSS rules that style the
  // persisted assistant bubble (paragraph margins, code block surfaces,
  // table borders, etc. — all scoped to `.message--assistant
  // .message__content`) apply during streaming too. Without the
  // wrapper the streaming markdown would render unstyled and then
  // restyle when `done` swaps the persisted bubble in.
  let rendered;
  if (typeof marked !== 'undefined') {
    try {
      rendered = marked.parse(next);
    } catch {
      // A partial chunk can leave markdown in an unparseable state
      // (e.g. an unclosed code fence). Fall back to plain text so the
      // user still sees something coherent until the next chunk lands.
      elt.textContent = next;
      e.preventDefault();
      return;
    }
  } else {
    elt.textContent = next;
    e.preventDefault();
    return;
  }
  elt.innerHTML = `<div class="message__content">${rendered}</div>`;
  e.preventDefault();
});
