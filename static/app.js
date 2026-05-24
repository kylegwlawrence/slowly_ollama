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
    clearSidebarAriaCurrent();
    sidebarLink.closest('.chat-item').setAttribute('aria-current', 'page');
    return;
  }
  if (e.target.closest('.sidebar__new-chat, .sidebar__settings')) {
    clearSidebarAriaCurrent();
  }
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
});

// ---------------------------------------------------------------------
// Phase 16: empty-state composer — grey the model dropdown when an agent
// is picked
// ---------------------------------------------------------------------
// Each agent runs on its own assigned model, so the model dropdown is
// irrelevant while a non-Normal agent is selected. We lock it VISUALLY
// (.composer__model--locked) but keep it ENABLED so the form still
// submits a Normal model — a disabled <select> submits nothing, and the
// chat persists that model for when the user switches back to Normal.
// Delegated on document.body so a freshly /new-swapped composer is covered
// without a re-bind. The in-chat picker has no #composer-agent id, so it
// falls through to its own hx-post.

function syncComposerModelLock() {
  const agent = document.getElementById('composer-agent');
  const model = document.getElementById('composer-model');
  if (!agent || !model) return;
  const locked = agent.value !== '';
  model.classList.toggle('composer__model--locked', locked);
  model.setAttribute('aria-disabled', locked ? 'true' : 'false');
}

document.body.addEventListener('change', (e) => {
  if (e.target instanceof Element && e.target.id === 'composer-agent') {
    syncComposerModelLock();
  }
});

// Phase 15: composer tool chips — keep the visual --on/--off class in
// sync with the underlying checkbox state. The chip's <label> wraps a
// visually hidden <input type="checkbox">, so the browser handles the
// toggle natively when the user clicks anywhere in the label; we just
// listen for the resulting `change` event and refresh the chip's class
// + check glyph. The checkbox carries the value to POST /chats as
// `enabled_tools` (data-tool) or `enabled_rag_servers` (data-rag-server).
// Chat-panel chips render as buttons that hx-post instead — no
// checkbox, so the guard below skips them.
document.body.addEventListener('change', function (e) {
  const cb = e.target;
  if (!(cb instanceof HTMLInputElement)) return;
  if (!cb.matches('.tool-chip__checkbox')) return;
  const chip = cb.closest('.tool-chip');
  if (!chip) return;
  chip.classList.toggle('tool-chip--on', cb.checked);
  chip.classList.toggle('tool-chip--off', !cb.checked);
  const check = chip.querySelector('.tool-chip__check');
  if (check) check.textContent = cb.checked ? '✓' : '✕';
});

// (data-default re-selection lives inside the single htmx:afterSwap
// handler above — see responsibility #2.)
