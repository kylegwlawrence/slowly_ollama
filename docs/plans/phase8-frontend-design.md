# Phase 8 — Frontend polish (Google Chat aesthetic)

## Context

The frontend that landed in Phase 7 is functional but plain: Pico classless
gives sensible defaults but no visual identity, message bubbles look like
generic boxes, sidebar actions are unicode glyphs (× ✎), and the new-chat
form sits permanently expanded in the sidebar. The app works but doesn't
feel finished.

This phase reshapes the UI to a simple, modern Google Chat aesthetic:
pill-shaped message bubbles, a "Compose"-style button at the top of the
sidebar, kebab menus for per-chat actions, Material Symbols icons, a pill
input with an inline send button, and animated typing dots while a
response is loading. All purely CSS + HTMX + tiny inline JS. No new
runtime dependencies; one new vendored asset (Material Symbols font).

The previous Phase 8 (full test suite) becomes Phase 9.

## Design decisions (from question rounds)

| Aspect | Decision |
|---|---|
| Reference | Google Chat |
| Color mode | Light only |
| Sidebar | 256px fixed, always visible |
| Font | System font stack (no download) |
| Accent | Mostly grayscale; blue `#1a73e8` for Send button + focus rings; user bubble uses a tonal blue (`#e8f0fe`) |
| Buttons | Material 3 mixed — filled primary + text secondary |
| Message input | Pill input with inline circular send icon; auto-grows |
| Density | Comfortable (Material default) |
| New chat | Big filled "Compose" button at sidebar top; click expands form below via `<details>` |
| Messages | Bubbles, user right (tonal blue), assistant left (white with border) |
| Bubble grouping | Google Chat-style — consecutive same-sender bubbles share their adjacent corner |
| Row actions | Kebab (⋮) menu opening a popover with Rename + Delete |
| Loading state | Animated typing dots in the empty placeholder bubble |
| Icons | Material Symbols (vendored Outlined font) |
| CSS base | Keep Pico + override heavily via new `static/style.css` |

## Critical files

**New:**
- `/Users/kyle/Projects/ollama_slowly/static/style.css`
- `/Users/kyle/Projects/ollama_slowly/static/material-symbols.woff2`
- `/Users/kyle/Projects/ollama_slowly/static/material-symbols.css`

**Modified:**
- `/Users/kyle/Projects/ollama_slowly/docs/plans/PLAN.md`
- `/Users/kyle/Projects/ollama_slowly/templates/base.html`
- `/Users/kyle/Projects/ollama_slowly/templates/_new_chat_form.html`
- `/Users/kyle/Projects/ollama_slowly/templates/_chat_item.html`
- `/Users/kyle/Projects/ollama_slowly/templates/_chat_item_edit.html`
- `/Users/kyle/Projects/ollama_slowly/templates/_chat_panel.html`
- `/Users/kyle/Projects/ollama_slowly/templates/_message.html`
- `/Users/kyle/Projects/ollama_slowly/templates/_assistant_placeholder.html`
- `/Users/kyle/Projects/ollama_slowly/templates/index.html`

**Test file (no changes expected, but verify):**
- `/Users/kyle/Projects/ollama_slowly/tests/test_routes.py` — every assertion is substring-matched. The plan preserves all pinned substrings; tests should remain 91 passing.

---

## Commit 1 — `docs: renumber PLAN.md phases`

### Edit `docs/plans/PLAN.md`

Find this block (currently at lines 99–106):

```
### Phase 7 — frontend
- HTMX + Jinja layout: sidebar with conversation list, main chat panel
- Streaming responses appended to the chat panel via SSE (`htmx-ext-sse`)
- Model dropdown, rename/delete controls, regenerate button

### Phase 8 — full test suite
- Round out tests across all layers
- Decide on Ollama mocking strategy for tests (likely: mock httpx for unit tests; optional integration tests against a real Ollama instance)
```

Replace with:

```
### Phase 7 — frontend
- HTMX + Jinja layout: sidebar with conversation list, main chat panel
- Streaming responses appended to the chat panel via SSE (`htmx-ext-sse`)
- Model dropdown, rename/delete controls, regenerate button

### Phase 8 — frontend polish (Google Chat aesthetic)
- Vendor Material Symbols Outlined font under `static/`
- Single hand-written `static/style.css` layered over Pico classless
- Pill-shaped message bubbles, user-right (tonal blue) / assistant-left (white)
- Google Chat-style bubble grouping via CSS sibling selectors
- "Compose" disclosure (`<details>`) for the new-chat form
- Kebab popover (`:focus-within`) for per-row Rename / Delete
- Pill message input with inline circular send icon; auto-grows via `field-sizing`
- Animated typing dots via `:empty::before` while a response is loading
- Blue accent reserved for Send button + focus rings; everything else grayscale

### Phase 9 — full test suite
- Round out tests across all layers
- Decide on Ollama mocking strategy for tests (likely: mock httpx for unit tests; optional integration tests against a real Ollama instance)
```

### Verify and commit

```bash
git add docs/plans/PLAN.md
git commit -m "docs: renumber Phase 8 to Phase 9, insert frontend-polish phase

Bumps the planned 'full test suite' to Phase 9 and adds a new Phase 8
covering the Google Chat aesthetic redesign (Material Symbols,
custom style.css, pill bubbles, kebab menus, typing dots).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Commit 2 — `feat(static): vendor Material Symbols + add style.css skeleton`

### Step 2.1 — Vendor Material Symbols Outlined

The Google Fonts CSS API resolves to a CSS file pointing at a woff2 font.
Fetch both:

```bash
# Inside the project root
curl -sLo /tmp/ms.css \
  "https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined" \
  -H "User-Agent: Mozilla/5.0"

# Inspect /tmp/ms.css to find the woff2 URL — looks like:
# https://fonts.gstatic.com/s/materialsymbolsoutlined/vXXX/.../...woff2
# Curl that file:
WOFF_URL=$(grep -oE 'https://[^)]*\.woff2' /tmp/ms.css | head -1)
curl -sLo static/material-symbols.woff2 "$WOFF_URL"
```

### Step 2.2 — Create `static/material-symbols.css`

Write to `/Users/kyle/Projects/ollama_slowly/static/material-symbols.css`:

```css
/* Material Symbols Outlined — vendored from Google Fonts.
   Pair with the woff2 in the same directory.
   Use via: <span class="material-symbols-outlined">edit</span> */

@font-face {
  font-family: 'Material Symbols Outlined';
  font-style: normal;
  font-weight: 100 700;
  src: url('/static/material-symbols.woff2') format('woff2');
}

.material-symbols-outlined {
  font-family: 'Material Symbols Outlined';
  font-weight: normal;
  font-style: normal;
  font-size: 20px;
  line-height: 1;
  letter-spacing: normal;
  text-transform: none;
  display: inline-block;
  white-space: nowrap;
  word-wrap: normal;
  direction: ltr;
  font-feature-settings: 'liga';
  -webkit-font-smoothing: antialiased;
  font-variation-settings:
    'FILL' 0,
    'wght' 400,
    'GRAD' 0,
    'opsz' 24;
}
```

### Step 2.3 — Create `static/style.css`

Write to `/Users/kyle/Projects/ollama_slowly/static/style.css`:

```css
/* ollama_slowly — Google Chat-inspired visual layer.
   Loaded after Pico classless; overrides Pico defaults where needed. */

/* ===== Tokens ============================================================ */

:root {
  --bg: #ffffff;
  --surface: #f8f9fa;
  --surface-hover: #f1f3f4;
  --border: #e8eaed;
  --text-primary: #202124;
  --text-secondary: #5f6368;
  --accent: #1a73e8;
  --accent-hover: #185abc;
  --accent-tonal: #e8f0fe;
  --accent-tonal-text: #185abc;
  --danger: #d93025;

  --space-xs: 4px;
  --space-sm: 8px;
  --space-md: 12px;
  --space-lg: 16px;
  --space-xl: 24px;

  --radius-sm: 4px;
  --radius-md: 8px;
  --radius-lg: 16px;
  --radius-pill: 9999px;

  --shadow-sm: 0 1px 2px rgba(60, 64, 67, 0.08);
  --shadow-md: 0 2px 6px rgba(60, 64, 67, 0.15);
}

/* ===== Base ============================================================== */

body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto,
               'Helvetica Neue', Arial, sans-serif;
  color: var(--text-primary);
  background: var(--bg);
  margin: 0;
  padding: 0;
}

:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

/* ===== Layout ============================================================ */

.layout {
  display: flex;
  height: 100vh;
  overflow: hidden;
}

.sidebar {
  width: 256px;
  flex-shrink: 0;
  background: var(--bg);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  padding: var(--space-md);
  gap: var(--space-md);
}

.sidebar h1 {
  font-size: 18px;
  font-weight: 500;
  color: var(--text-primary);
  margin: 0;
  padding: var(--space-sm) var(--space-md);
}

#main {
  flex: 1;
  background: var(--surface);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.empty-state {
  margin: auto;
  color: var(--text-secondary);
  text-align: center;
  padding: var(--space-xl);
}

/* ===== Compose disclosure (<details> wrapping new-chat-form) ============= */

.compose { /* the <details> element */ }

.compose__button {
  /* the <summary> styled as a filled button */
  list-style: none;
  background: var(--accent);
  color: white;
  border-radius: var(--radius-pill);
  padding: var(--space-md) var(--space-lg);
  font-weight: 500;
  font-size: 14px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--space-sm);
  user-select: none;
  transition: background 0.15s ease;
}

.compose__button::-webkit-details-marker { display: none; }
.compose__button:hover { background: var(--accent-hover); }
.compose[open] .compose__button { background: var(--accent-hover); }

.new-chat-form {
  display: flex;
  flex-direction: column;
  gap: var(--space-sm);
  padding: var(--space-md) 0;
}

.new-chat-form label {
  display: flex;
  flex-direction: column;
  gap: var(--space-xs);
  font-size: 12px;
  color: var(--text-secondary);
}

.new-chat-form input,
.new-chat-form select {
  padding: var(--space-sm) var(--space-md);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  font-size: 14px;
  background: var(--bg);
  color: var(--text-primary);
}

.new-chat-form button[type="submit"] {
  background: var(--accent);
  color: white;
  border: none;
  padding: var(--space-sm) var(--space-md);
  border-radius: var(--radius-md);
  cursor: pointer;
  font-size: 14px;
  font-weight: 500;
  align-self: flex-end;
}

.new-chat-form button[type="submit"]:hover { background: var(--accent-hover); }

/* ===== Chats list ======================================================== */

.chats-list {
  list-style: none;
  margin: 0;
  padding: 0;
  overflow-y: auto;
  flex: 1;
}

.chat-item {
  display: flex;
  align-items: center;
  gap: var(--space-xs);
  padding: var(--space-sm);
  border-radius: var(--radius-md);
  margin-bottom: 2px;
  transition: background 0.15s ease;
}

.chat-item:hover { background: var(--surface-hover); }

.chat-item > a {
  flex: 1;
  color: var(--text-primary);
  text-decoration: none;
  font-size: 14px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  padding: var(--space-xs) var(--space-sm);
}

/* Kebab menu wrapper (replaces the inline rename/delete buttons) */
.chat-item__menu {
  position: relative;
}

.chat-item__kebab {
  background: none;
  border: none;
  cursor: pointer;
  padding: var(--space-xs);
  color: var(--text-secondary);
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
}

.chat-item__kebab:hover { background: var(--surface-hover); }

.chat-item__menu-popup {
  display: none;
  position: absolute;
  top: 100%;
  right: 0;
  background: var(--bg);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-md);
  border: 1px solid var(--border);
  min-width: 140px;
  padding: var(--space-xs) 0;
  z-index: 10;
}

.chat-item__menu:focus-within .chat-item__menu-popup {
  display: block;
}

.chat-item__menu-popup button,
.chat-item__menu-popup a {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  width: 100%;
  background: none;
  border: none;
  padding: var(--space-sm) var(--space-md);
  text-align: left;
  cursor: pointer;
  font-size: 14px;
  color: var(--text-primary);
  text-decoration: none;
  box-sizing: border-box;
}

.chat-item__menu-popup button:hover { background: var(--surface-hover); }
.chat-item__delete { color: var(--danger); }

/* Editing state */
.chat-item--editing { background: var(--surface-hover); }

.chat-item__edit-form {
  display: flex;
  gap: var(--space-xs);
  flex: 1;
  align-items: center;
}

.chat-item__edit-form input {
  flex: 1;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: var(--space-xs) var(--space-sm);
  font-size: 14px;
}

.chat-item__edit-form button {
  background: none;
  border: none;
  cursor: pointer;
  color: var(--text-secondary);
  padding: var(--space-xs);
  border-radius: 50%;
}

.chat-item__edit-form button:hover { background: var(--surface-hover); }

/* ===== Chat panel ======================================================== */

.chat-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
}

.chat-panel__header {
  display: flex;
  align-items: center;
  gap: var(--space-md);
  padding: var(--space-md) var(--space-xl);
  background: var(--bg);
  border-bottom: 1px solid var(--border);
}

.chat-panel__name {
  margin: 0;
  font-size: 16px;
  font-weight: 500;
  color: var(--text-primary);
}

.chat-panel__model {
  display: inline-block;
  background: var(--accent-tonal);
  color: var(--accent-tonal-text);
  padding: 2px var(--space-sm);
  border-radius: var(--radius-pill);
  font-size: 12px;
  font-weight: 500;
}

.messages {
  flex: 1;
  overflow-y: auto;
  padding: var(--space-lg) var(--space-xl);
  display: flex;
  flex-direction: column;
  gap: var(--space-xs);
}

/* ===== Message bubbles =================================================== */

.message {
  max-width: 70%;
  padding: var(--space-sm) var(--space-md);
  border-radius: var(--radius-lg);
  word-wrap: break-word;
  position: relative;
}

.message__content {
  white-space: pre-wrap;
  line-height: 1.5;
  font-size: 14px;
}

.message--user {
  align-self: flex-end;
  background: var(--accent-tonal);
  color: var(--text-primary);
}

.message--assistant {
  align-self: flex-start;
  background: var(--bg);
  border: 1px solid var(--border);
}

/* Bubble grouping: consecutive same-sender bubbles share their adjacent
   corner. Right-side bubbles (user) share their right corners; left-side
   bubbles (assistant) share their left corners. */
.message--user + .message--user {
  border-top-right-radius: var(--radius-sm);
}
.message--user:has(+ .message--user) {
  border-bottom-right-radius: var(--radius-sm);
}
.message--assistant + .message--assistant {
  border-top-left-radius: var(--radius-sm);
}
.message--assistant:has(+ .message--assistant) {
  border-bottom-left-radius: var(--radius-sm);
}

/* Extra space when the sender changes */
.message--user + .message--assistant,
.message--assistant + .message--user {
  margin-top: var(--space-md);
}

/* ===== Typing dots (placeholder is :empty until first token arrives) ===== */
.message--streaming {
  min-height: 32px;
}

.message--streaming:empty::before {
  content: '•••';
  font-size: 24px;
  letter-spacing: 4px;
  color: var(--text-secondary);
  animation: typing-pulse 1.4s infinite;
  display: inline-block;
  line-height: 1;
}

@keyframes typing-pulse {
  0%, 100% { opacity: 0.3; }
  50% { opacity: 1; }
}

/* ===== Regenerate button =================================================
   The visibility logic (`display: none` by default, override on
   `:last-child.message--assistant:not(.message--streaming)`) MUST remain
   inline in base.html — tests substring-match those rules. The rules
   below are just visual polish.                                          */

.message__regenerate {
  align-items: center;
  gap: var(--space-xs);
  background: none;
  border: none;
  cursor: pointer;
  font-size: 12px;
  color: var(--text-secondary);
  padding: var(--space-xs) var(--space-sm);
  border-radius: var(--radius-md);
  margin-top: var(--space-sm);
}

.message__regenerate:hover {
  background: var(--surface-hover);
  color: var(--accent);
}

/* ===== Message input (pill with inline send) ============================ */

.message-form {
  padding: var(--space-md) var(--space-xl) var(--space-lg);
}

.message-form__pill {
  display: flex;
  align-items: flex-end;
  gap: var(--space-sm);
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 24px;
  padding: var(--space-sm) var(--space-sm) var(--space-sm) var(--space-lg);
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}

.message-form__pill:focus-within {
  border-color: var(--accent);
  box-shadow: 0 0 0 1px var(--accent);
}

.message-form textarea {
  flex: 1;
  border: none;
  outline: none;
  resize: none;
  font-family: inherit;
  font-size: 14px;
  line-height: 1.5;
  background: transparent;
  field-sizing: content;
  min-height: 24px;
  max-height: 160px;
  padding: var(--space-xs) 0;
}

.message-form button[type="submit"] {
  background: var(--accent);
  color: white;
  border: none;
  border-radius: 50%;
  width: 36px;
  height: 36px;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  flex-shrink: 0;
  transition: background 0.15s ease;
}

.message-form button[type="submit"]:hover { background: var(--accent-hover); }
```

### Step 2.4 — Update `templates/base.html`

Find the `<head>` section (after the Pico `<link>` and the htmx scripts).
The two inline CSS rules **must stay** because tests substring-match them.

Replace the current `<head>` interior:

```html
  <link rel="stylesheet" href="/static/pico.classless.min.css">
  {# htmx.min.js must load before htmx-ext-sse.js — the extension
     registers itself against the global `htmx` set up by core. Using
     `defer` on both keeps the document parsing without blocking,
     while still preserving order. #}
  <script src="/static/htmx.min.js" defer></script>
  <script src="/static/htmx-ext-sse.js" defer></script>

  {# Small custom rules layered on top of Pico's classless defaults.
     Move to static/style.css if this grows past a handful of rules. #}
  <style>
    /* While a response is streaming, soft-disable the message form's
       submit button. (...) */
    .chat-panel:has(.message--streaming) .message-form button {
      pointer-events: none;
      opacity: 0.5;
    }

    /* Regenerate is rendered on every assistant message bubble (...) */
    .message__regenerate { display: none; }
    .messages .message:last-child.message--assistant:not(.message--streaming) .message__regenerate {
      display: inline-block;
    }
  </style>
```

With:

```html
  <link rel="stylesheet" href="/static/pico.classless.min.css">
  <link rel="stylesheet" href="/static/material-symbols.css">
  <link rel="stylesheet" href="/static/style.css">
  {# htmx.min.js must load before htmx-ext-sse.js — the extension
     registers itself against the global `htmx` set up by core. Using
     `defer` on both keeps the document parsing without blocking,
     while still preserving order. #}
  <script src="/static/htmx.min.js" defer></script>
  <script src="/static/htmx-ext-sse.js" defer></script>

  {# These two rules MUST stay inline (tests substring-match them).
     The visual rest of the design lives in static/style.css. #}
  <style>
    /* Soft-disable the send button while a response streams (prevents
       double-fire opening two parallel SSE streams). */
    .chat-panel:has(.message--streaming) .message-form button {
      pointer-events: none;
      opacity: 0.5;
    }

    /* Regenerate button is rendered on every assistant bubble; show
       only on the most-recent finished assistant bubble. */
    .message__regenerate { display: none; }
    .messages .message:last-child.message--assistant:not(.message--streaming) .message__regenerate {
      display: inline-block;
    }
  </style>
```

### Step 2.5 — Run tests and commit

```bash
source .venv/bin/activate && python -m pytest tests/ -q
# Expect: 91 passed

git add static/style.css static/material-symbols.css static/material-symbols.woff2 templates/base.html
git commit -m "feat(static): vendor Material Symbols + add style.css

Vendors the Material Symbols Outlined variable font (~80KB) and
introduces static/style.css as the custom visual layer (color tokens,
layout, sidebar, chat panel, bubbles, pill input). Pico classless
stays loaded; style.css overrides defaults.

The two CSS rules tests substring-match (pointer-events:none on the
streaming-disabled button, and .message__regenerate display rules)
stay inline in base.html.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Commit 3 — `feat(templates): bubbles, pill input, typing dots`

### Step 3.1 — Update `templates/_chat_panel.html`

Replace the current form section (lines 22–29):

```html
  <form class="message-form"
        hx-post="/chats/{{ conversation.id }}/messages"
        hx-target="#messages"
        hx-swap="beforeend"
        hx-on::after-request="if (event.detail.successful) this.reset()">
    <textarea name="content" required placeholder="Message..."></textarea>
    <button type="submit">Send</button>
  </form>
```

With:

```html
  <form class="message-form"
        hx-post="/chats/{{ conversation.id }}/messages"
        hx-target="#messages"
        hx-swap="beforeend"
        hx-on::after-request="if (event.detail.successful) this.reset()">
    <div class="message-form__pill">
      <textarea name="content" required placeholder="Message Ollama…"
                rows="1"></textarea>
      <button type="submit" aria-label="Send">
        <span class="material-symbols-outlined">send</span>
      </button>
    </div>
  </form>
```

**Note:** `<button type="submit">` is preserved (the streaming-disable CSS
selector `.message-form button` still matches). `name="content"` and
`required` on the textarea are preserved (form parsing + browser
validation still work).

### Step 3.2 — Update `templates/_message.html`

Find this block (the regenerate button):

```html
  <button class="message__regenerate"
          type="button"
          aria-label="Regenerate response"
          hx-post="/chats/{{ message.conversation_id }}/regenerate"
          hx-target="closest .message"
          hx-swap="outerHTML">↻ Regenerate</button>
```

Replace with:

```html
  <button class="message__regenerate"
          type="button"
          aria-label="Regenerate response"
          hx-post="/chats/{{ message.conversation_id }}/regenerate"
          hx-target="closest .message"
          hx-swap="outerHTML">
    <span class="material-symbols-outlined">refresh</span>
    Regenerate
  </button>
```

**Preserved:** `class="message__regenerate"`, `hx-post=…/regenerate`,
`hx-target="closest .message"` (all substring-matched by tests).

### Step 3.3 — Fix `templates/_assistant_placeholder.html` to be genuinely `:empty`

The CSS typing-dots rule uses `:empty::before`. The current template has
whitespace between the opening and closing tags, so `:empty` doesn't
match. Make the div literally empty.

Replace:

```html
<div id="assistant-stream-{{ conversation_id }}"
     class="message message--assistant message--streaming"
     data-role="assistant"
     hx-ext="sse"
     sse-connect="{{ stream_url }}"
     sse-swap="token,done,error"
     hx-swap="beforeend">
</div>
```

With (note: no whitespace between `>` and `</div>`):

```html
<div id="assistant-stream-{{ conversation_id }}" class="message message--assistant message--streaming" data-role="assistant" hx-ext="sse" sse-connect="{{ stream_url }}" sse-swap="token,done,error" hx-swap="beforeend"></div>
```

This is a single line on purpose — Jinja2 doesn't preserve trailing
whitespace inside `>...</`, so anything on the next line would break
`:empty`. Keep the comment block above the div for documentation.

### Step 3.4 — Run tests and commit

```bash
source .venv/bin/activate && python -m pytest tests/ -q
# Expect: 91 passed

git add templates/_chat_panel.html templates/_message.html templates/_assistant_placeholder.html
git commit -m "feat(templates): pill input, regenerate icon, empty placeholder

- Wraps the chat panel's textarea + submit in .message-form__pill so
  the CSS in style.css can render it as a single pill with an inline
  circular send icon. Replaces the 'Send' text with a Material
  Symbols 'send' glyph.
- Replaces the regenerate button's '↻ Regenerate' unicode glyph with
  a Material Symbols 'refresh' icon next to the label.
- Collapses _assistant_placeholder.html to a single line so the div
  is genuinely :empty until the first token swaps in; the typing-dots
  CSS rule in style.css uses :empty::before to render an animated
  ellipsis until streaming starts.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Commit 4 — `feat(templates): compose disclosure for new chat`

### Step 4.1 — Replace `templates/_new_chat_form.html`

Replace the entire file content with:

```html
{# Sidebar "Compose" disclosure + new-chat form.

   The <details> element provides native expand/collapse: the
   <summary> is styled as the big filled Compose button at the top of
   the sidebar; clicking it expands the form below. The form's
   `hx-on::after-request` resets fields AND collapses the <details>
   on a successful submission.

   Two HTMX flows run inside the form:

   1. The <select name="model"> auto-loads its <option> tags from
      GET /models the moment the form lands in the DOM
      (`hx-trigger="load"`). The placeholder option is replaced as
      soon as Ollama responds.

   2. The <form> itself posts to /chats. The response is one <li>
      (the new chat row) which HTMX prepends to `#chats-list` via
      `hx-swap="afterbegin"`. After a successful response the form
      resets and the disclosure collapses. #}
<details class="compose">
  <summary class="compose__button">
    <span class="material-symbols-outlined">edit</span>
    Compose
  </summary>
  <form class="new-chat-form"
        hx-post="/chats"
        hx-target="#chats-list"
        hx-swap="afterbegin"
        hx-on::after-request="if (event.detail.successful) { this.reset(); this.closest('details').open = false; }">
    <label>
      Name
      <input type="text" name="name" required placeholder="My chat">
    </label>
    <label>
      Model
      <select name="model" required
              hx-get="/models"
              hx-trigger="load"
              hx-target="this"
              hx-swap="innerHTML">
        <option value="">Loading models…</option>
      </select>
    </label>
    <button type="submit">New chat</button>
  </form>
</details>
```

**Preserved substrings** (test pins):
- `class="new-chat-form"`
- `hx-post="/chats"`
- `hx-target="#chats-list"`
- `hx-swap="afterbegin"`
- `name="name"`, `name="model"`
- `<select` (note: tests check `<select` not `<select ` so this matches)
- `hx-get="/models"`, `hx-trigger="load"`
- `Loading models` (the placeholder text)

### Step 4.2 — Run tests and commit

```bash
source .venv/bin/activate && python -m pytest tests/ -q
# Expect: 91 passed

git add templates/_new_chat_form.html
git commit -m "feat(templates): compose disclosure for new-chat form

Wraps the sidebar's new-chat form in <details><summary>...</summary>
so the form is collapsed by default. The <summary> is styled as the
big filled 'Compose' button (Material Symbols edit icon + label).
Clicking it expands the form; submitting collapses it again via the
form's after-request handler.

Trade-off: the model dropdown's hx-trigger='load' fires when the
<select> first enters the DOM. With <details> closed by default,
the dropdown is in the DOM but hidden — HTMX still processes the
trigger and loads the options eagerly. Confirmed by visual QA.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

**Behavior note for the executor:** verify in the browser that the model
dropdown is populated when the user first expands the form. `<details>`
keeps its content in the DOM even when collapsed (just `display: none`),
so `hx-trigger="load"` should still fire on page load. If it doesn't,
change to `hx-trigger="load, toggle from:closest details"`.

---

## Commit 5 — `feat(templates): kebab menu for chat row actions`

### Step 5.1 — Replace `templates/_chat_item.html`

Replace the entire file content with:

```html
{# One sidebar row.

   Two parallel navigation paths so the page works with or without
   HTMX:
   - With HTMX loaded: `hx-get` on the link intercepts the click,
     swaps the panel into #main, and `hx-push-url="true"` updates the
     URL to `/chats/{id}` so reload restores the same view.
   - Without HTMX: the browser follows `href` normally; the server's
     `GET /chats/{id}` route renders the full index page with this
     chat preloaded.

   Per-row actions (rename + delete) live inside a kebab popover.
   The popover uses CSS-only show/hide via `:focus-within` on the
   wrapping `.chat-item__menu`: clicking the kebab focuses it (button
   gets focus on click in modern browsers), CSS reveals the popup.
   Clicking an action inside fires its HTMX request — the resulting
   swap removes or replaces the row, naturally closing the popup.
   Clicking elsewhere blurs the kebab and hides the popup.

   The delete button still uses `hx-confirm` for the browser-native
   "are you sure?" prompt and `hx-swap="delete"` to remove the <li>
   on success. The inline JS on after-request redirects to `/` if the
   user happened to be viewing the deleted chat. #}
<li id="chat-{{ chat.id }}" class="chat-item" data-chat-id="{{ chat.id }}">
  <a href="/chats/{{ chat.id }}"
     hx-get="/chats/{{ chat.id }}"
     hx-target="#main"
     hx-swap="innerHTML"
     hx-push-url="true">{{ chat.name }}</a>
  <div class="chat-item__menu">
    <button class="chat-item__kebab"
            type="button"
            aria-label="Chat options"
            aria-haspopup="menu">
      <span class="material-symbols-outlined">more_vert</span>
    </button>
    <div class="chat-item__menu-popup" role="menu">
      <button class="chat-item__rename"
              type="button"
              aria-label="Rename chat"
              hx-get="/chats/{{ chat.id }}/edit"
              hx-target="#chat-{{ chat.id }}"
              hx-swap="outerHTML">
        <span class="material-symbols-outlined">edit</span>
        Rename
      </button>
      <button class="chat-item__delete"
              type="button"
              aria-label="Delete chat"
              hx-delete="/chats/{{ chat.id }}"
              hx-target="#chat-{{ chat.id }}"
              hx-swap="delete"
              hx-confirm="Delete '{{ chat.name }}'?"
              hx-on::after-request="if (event.detail.successful && window.location.pathname === '/chats/{{ chat.id }}') window.location = '/'">
        <span class="material-symbols-outlined">delete</span>
        Delete
      </button>
    </div>
  </div>
</li>
```

**Preserved substrings** (every test assertion):
- `id="chat-{{ chat.id }}"`, `class="chat-item"`, `data-chat-id`
- `href="/chats/{id}"`, `hx-get="/chats/{id}"`, `hx-target="#main"`,
  `hx-swap="innerHTML"`, `hx-push-url="true"`
- `class="chat-item__rename"`, `hx-get="/chats/{id}/edit"`,
  `hx-target="#chat-{id}"`, `hx-swap="outerHTML"` (rename)
- `class="chat-item__delete"`, `hx-delete="/chats/{id}"`,
  `hx-target="#chat-{id}"`, `hx-swap="delete"`, `hx-confirm=`,
  `window.location` (delete after-request)

### Step 5.2 — Update `templates/_chat_item_edit.html` (visual polish)

Replace the file content with:

```html
{# Sidebar row in edit mode — replaces the display version when the
   user clicks the rename action in the kebab menu. Submit
   (PATCH /chats/{id}) swaps back to the display version because the
   PATCH route returns _chat_item.html. Cancel reaches for
   GET /chats/{id}/item which re-renders the display version without
   saving. #}
<li id="chat-{{ chat.id }}"
    class="chat-item chat-item--editing"
    data-chat-id="{{ chat.id }}">
  <form class="chat-item__edit-form"
        hx-patch="/chats/{{ chat.id }}"
        hx-target="#chat-{{ chat.id }}"
        hx-swap="outerHTML">
    <input type="text" name="name" value="{{ chat.name }}" required autofocus>
    <button type="submit" aria-label="Save">
      <span class="material-symbols-outlined">check</span>
    </button>
    <button type="button"
            aria-label="Cancel"
            hx-get="/chats/{{ chat.id }}/item"
            hx-target="#chat-{{ chat.id }}"
            hx-swap="outerHTML">
      <span class="material-symbols-outlined">close</span>
    </button>
  </form>
</li>
```

**Preserved substrings**:
- `id="chat-{id}"`, `class="chat-item chat-item--editing"`, `data-chat-id`
- `hx-patch="/chats/{id}"`, `name="name"`, `value="{{ chat.name }}"`
- `hx-get="/chats/{id}/item"` (cancel button)

### Step 5.3 — Update `templates/index.html` (small empty-state polish)

Replace the `<main>` section:

```html
  <main id="main">
    {% if conversation %}
      {% include "_chat_panel.html" %}
    {% else %}
      <p class="empty-state">Select a chat from the sidebar, or create a new one.</p>
    {% endif %}
  </main>
```

With:

```html
  <main id="main">
    {% if conversation %}
      {% include "_chat_panel.html" %}
    {% else %}
      <div class="empty-state">
        <span class="material-symbols-outlined" style="font-size: 64px; opacity: 0.5;">chat</span>
        <p>Select a chat from the sidebar, or create a new one.</p>
      </div>
    {% endif %}
  </main>
```

**Preserved substrings**: `class="empty-state"`, the existing copy
("Select a chat..." — tests don't actually pin this string but keeping
it stable is courteous).

### Step 5.4 — Run tests + visual QA

```bash
source .venv/bin/activate && python -m pytest tests/ -q
# Expect: 91 passed

# Visual QA — start the server and open in browser:
source .venv/bin/activate && uvicorn main:app --port 8000
# Manually exercise the checklist in the Verification section below.
```

### Step 5.5 — Commit

```bash
git add templates/_chat_item.html templates/_chat_item_edit.html templates/index.html
git commit -m "feat(templates): kebab menu for chat row actions

Replaces the always-visible rename + delete buttons on each sidebar
row with a single kebab (⋮) button that opens a small popover
containing both actions. CSS uses :focus-within for show/hide — no
JS for the popover, and clicking an action inside fires HTMX which
naturally closes the popup as the swap removes/replaces the row.

Every hx-* attribute on the rename and delete buttons is preserved
(class names, targets, swaps, confirm message, after-request
redirect) so existing test substring assertions still match.

Also: Material Symbols icons in the edit form's save/cancel
buttons, and a soft Material Symbols 'chat' glyph in the empty
state.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Optional Commit 6 — Polish pass (only if needed)

If browser QA reveals gaps, candidate fixes:

- Sidebar overflow when many chats — already covered by
  `.chats-list { overflow-y: auto; flex: 1; }` in style.css.
- Auto-focus the input when expanding Compose (Pico's autofocus on
  `<input autofocus>` should work, since `<details>` reveal counts as
  a focus event in modern browsers).
- If the model dropdown doesn't load when Compose is closed, swap
  `hx-trigger="load"` to `hx-trigger="load, toggle from:closest details"`.
- If the kebab popover positioning bleeds outside the viewport on the
  right edge, change `right: 0` to a smarter value or use anchor
  positioning.

Each as its own small commit.

---

## Test breakage assessment

`tests/test_routes.py` has 91 tests, all substring-matching the rendered
HTML. The plan preserves **every** pinned substring. Specifically:

| Substring | Source test | Preserved in |
|---|---|---|
| `class="chat-item"`, `data-chat-id` | multiple | `_chat_item.html` `<li>` |
| `chat-item__rename`, `chat-item__delete` | implicit via attribute matches | inside the kebab popup |
| `hx-get="/chats/{id}/edit"` | `test_chat_item_has_rename_button` | rename button in popup |
| `hx-delete="/chats/{id}"`, `hx-swap="delete"`, `hx-confirm=`, `window.location` | `test_chat_item_has_delete_button` | delete button in popup |
| `href="/chats/{id}"`, `hx-push-url="true"` | `test_chat_item_link_carries_href_and_hx_push_url` | `<a>` element |
| `class="new-chat-form"`, `hx-post="/chats"`, `hx-target="#chats-list"`, `hx-swap="afterbegin"`, `name="name"`, `name="model"` | `test_index_includes_new_chat_form` | form inside `<details>` |
| `<select`, `hx-get="/models"`, `hx-trigger="load"`, `Loading models` | `test_new_chat_form_model_dropdown_auto_loads_from_models` | select inside form |
| `pointer-events: none`, `.message__regenerate { display: none; }`, `:last-child.message--assistant:not(.message--streaming)` | `test_base_disables_message_button_while_streaming`, `test_base_css_hides_regenerate_except_on_last_assistant` | **stay inline in `base.html`** |
| `scrollTop = this.scrollHeight`, `scrollTop = m.scrollHeight` | `test_chat_panel_auto_scrolls_to_bottom` | unchanged in `_chat_panel.html` |
| `event.detail.successful`, `this.reset()` | `test_chat_panel_form_only_resets_on_successful_response` | unchanged form attr in `_chat_panel.html` |
| `data-role="assistant"`, `data-role="user"`, `class="messages"`, `id="messages"`, `class="chat-panel"` | multiple | unchanged |
| `hx-swap-oob="outerHTML:#assistant-stream-{id}"` | `test_stream_endpoint_emits_token_and_done_events` | unchanged in routes |
| `message__regenerate`, `hx-post="/chats/{id}/regenerate"`, `hx-target="closest .message"` | `test_assistant_message_bubble_has_regenerate_button` | preserved in `_message.html` |
| `sse-connect="/chats/{id}/stream"` | `test_send_message_returns_user_bubble_and_placeholder` | unchanged in placeholder |
| `class="sidebar"`, `id="chats-list"`, `empty-state` | multiple | unchanged |

**Expected breakage: zero.** Run the full suite after each commit; if any
test fails, the executor should pause and inspect the substring assertion
vs. the new rendered HTML.

---

## Verification (end-to-end after Commit 5)

Browser test (Chrome 105+, Safari 15.4+, or Firefox 121+ — all support
`:has()`):

```bash
source .venv/bin/activate
uvicorn main:app --port 8000
# Open http://localhost:8000
```

Checklist:

1. **Sidebar visual** — 256px wide, white background, right border. App title
   at top, big filled blue Compose button below, chats list below that.
2. **Compose click** — Click Compose → form slides into view below the
   button. Model dropdown shows real options (Ollama running) or
   disabled "Ollama is unreachable" (Ollama down).
3. **Create chat** — Type a name, pick a model, click "New chat" → new
   row prepends to the list, form clears, Compose collapses.
4. **Click a chat row** — Chat panel loads into `#main`. URL updates to
   `/chats/{id}`. Reload restores the same view.
5. **Kebab menu** — Click ⋮ on a chat row → popover appears with Rename
   and Delete options. Click outside → popover hides.
6. **Rename** — Click Rename in popover → row swaps to edit mode (input +
   check + close buttons). Submit saves; cancel reverts.
7. **Delete** — Click Delete → browser confirm prompt → row disappears.
   If viewing that chat's URL, page navigates to `/`.
8. **Send message** — Type in the pill input (auto-grows as you type);
   click the send icon. User bubble appears on the right (tonal blue);
   assistant placeholder appears on the left with pulsing typing dots.
   Tokens stream in; placeholder is replaced by the final bubble.
9. **Bubble grouping** — Send two messages in a row. Consecutive bubbles
   should share their adjacent corner (less rounded on the touching side).
10. **Regenerate** — After streaming completes, hover the last assistant
    bubble — a Refresh icon button appears below it. Click it → bubble
    replaces with a new streaming placeholder, tokens stream, final
    bubble replaces it in place.
11. **Send button disabled while streaming** — During streaming, the
    circular send icon is semi-transparent and unclickable.
12. **Offline Ollama** — Stop Ollama, reload the page. Model dropdown
    shows the disabled "unreachable" option.

After visual QA, commit a Phase 8 retro to `docs/retros/phase8-frontend-polish.md`
following the format of the Phase 6 / Phase 7 retros.

---

## What's intentionally NOT in this phase

- No auto-naming of new chats (PLAN.md feature change — backend untouched)
- No model-switch mid-conversation (backend doesn't support it)
- No dark mode toggle (user picked light-only)
- No JS framework or build step
- No new Python dependencies
- No backend route changes
- No feature work — pure visual + interaction polish

These are deferrable to future phases or out of scope entirely.
