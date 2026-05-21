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

function clearChatAriaCurrent() {
  document
    .querySelectorAll('#chats-list .chat-item[aria-current]')
    .forEach((li) => li.removeAttribute('aria-current'));
}

document.addEventListener('click', (e) => {
  const chatLink = e.target.closest('#chats-list .chat-item a');
  if (chatLink) {
    clearChatAriaCurrent();
    chatLink.closest('.chat-item').setAttribute('aria-current', 'page');
    return;
  }
  if (e.target.closest('.sidebar__new-chat, .sidebar__settings')) {
    clearChatAriaCurrent();
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

document.body.addEventListener('htmx:afterSwap', (e) => {
  const target = e.detail.target;
  if (!(target instanceof Element)) return;
  // The replaced `hx-on::after-swap` lived on #messages itself, so it
  // fired for ANY swap inside #messages via event bubbling: streaming
  // tokens into the assistant-stream placeholder, OOB-replacing that
  // placeholder with the persisted bubble, OOB-appending tool-rows,
  // freezing tool-rows, etc. Match that scope here with
  // `target.closest('#messages')`. The `#main` branch covers the
  // separate case where the chat panel itself was just mounted.
  if (
    target.id === 'main' ||
    target.id === 'messages' ||
    target.closest('#messages')
  ) {
    scrollMessagesToBottom();
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

// Phase 15: composer tool chips — toggle on/off state client-side.
// The underlying checkbox carries the value to POST /chats as
// `enabled_tools`. Chat-panel chips use hx-post instead (no JS needed).
document.addEventListener('click', function (e) {
  const chip = e.target.closest('.tool-chip[data-tool]');
  if (!chip) return;
  const cb = chip.querySelector('.tool-chip__checkbox');
  if (!cb) return;
  cb.checked = !cb.checked;
  chip.classList.toggle('tool-chip--on', cb.checked);
  chip.classList.toggle('tool-chip--off', !cb.checked);
  chip.querySelector('.tool-chip__check').textContent = cb.checked ? '✓' : '✕';
});
