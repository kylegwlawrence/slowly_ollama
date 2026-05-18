# Phase 11 — UI improvements

## Context

The Google-Chat-inspired UI from phases 8–9 works, but feels dense and
clunky: a "Compose" disclosure that hides the new-chat form, blue Google
accent colors, no theme switcher, and human-written chat names that
quickly turn into uninformative defaults like "My chat". The user wants
to modernize the feel along four axes — each shipping as its own commit,
its own sub-phase, and each tested before moving on (per the working
rules in `docs/plans/PLAN.md`).

Decisions captured up front (so we don't re-litigate later):

- **Look:** Minimal & airy, Claude-like.
- **Accent:** Sage green (`#9ccaa6`), used on Send button + focus rings,
  user message bubbles, active chat highlight, and model badge.
- **Composer:** Empty-state IS the composer. A small `+ New chat` link
  sits at the top of the sidebar; clicking it returns to the empty state.
- **Model selector:** Required pre-chat; locked after first message;
  displayed as a read-only badge in the chat header (current behaviour).
- **Theme:** Manual toggle, default light, persisted in `localStorage`.
  Sun/moon icon button in the sidebar footer.
- **Auto-titles:** Hardcode `tinyllama:1.1b-chat-v1-fp16`. Fire after the
  1st, 2nd, and 3rd assistant responses complete (max 3 refinements).
  Manual rename locks future auto-titles. If the model is missing, fail
  silently server-side and surface a one-time, non-blocking warning in
  the UI.

## Conventions every sub-phase must follow

Per `CLAUDE.md` and `PLAN.md`:

- **Google-style docstrings + type hints** on every new function:
  `set_name_auto`, `count_assistant_messages`, `generate_title`,
  `new_chat_endpoint`, `_ensure_name_locked_column`. Include
  `Args:`, `Returns:`, `Raises:` sections.
- **Inline comments for non-obvious code** — the SSE-tail title flow,
  the OOB-swap-by-id mechanism, the Pico-bleed-through mitigation, and
  the `name_locked` UPDATE-with-WHERE-clause race-condition guard all
  warrant comments explaining *why*, not what.
- **Database access uses `with conn:`** (native sqlite3 context
  manager), never `with closing(conn)`. The existing `queries.py`
  helpers already follow this; new helpers (`set_name_auto`,
  `count_assistant_messages`) must too.
- **Ask before each commit.** Even with all four sub-phases approved,
  pause between commits and confirm the diff is acceptable before
  running `git commit`.
- **Run the phase's tests before declaring it done** and before asking
  to commit (per PLAN.md working rule).

---

## Sub-phase 11a — Modern visual refresh + sage green accent (commit 1)

Goal: replace the Google-blue tokens and tighten the visual layer to feel
more like Claude — more whitespace, softer borders, larger type scale,
sage accent everywhere the blue currently sits.

Critical files:
- `static/style.css` — the only place the visual tokens live.
- `templates/_chats_list.html` and `templates/_chat_item.html` — need an
  "active chat" affordance (currently no `.chat-item--active` style or
  marker exists).
- `app/routes.py` — `get_chat_panel_endpoint` and `index_endpoint` pass
  `conversation` into the template; we'll forward the active chat id
  into `_chats_list.html` so the matching `<li>` can render with
  `aria-current="page"` (CSS will target `[aria-current="page"]`).

Concrete changes:
1. In `static/style.css` `:root`:
   - Replace `--accent` (#1a73e8 → `#9ccaa6`), `--accent-hover`
     (#185abc → `#7fb38c`), `--accent-tonal` (#e8f0fe → `#e8f2eb`),
     `--accent-tonal-text` (#185abc → `#3d6549`).
   - Soften borders: `--border: #ececec` (lighter than `#e8eaed`).
   - Bump spacing scale: keep names, widen values
     (`--space-md: 14px`, `--space-lg: 20px`, `--space-xl: 28px`).
   - Drop shadows toward 0: `--shadow-sm` to `0 1px 0 rgba(0,0,0,.04)`,
     keep `--shadow-md` only for the kebab popover.
   - Typography token: add `--font-size-base: 15px` and bump
     `.message__content` + form inputs to use it; bump
     `.chat-panel__name` to 18px.
2. Layout breathing room:
   - `.sidebar { width: 280px; padding: var(--space-lg); }`
   - `.messages { padding: var(--space-xl) var(--space-xl); gap: var(--space-sm); }`
   - `.message { max-width: 75%; padding: var(--space-md) var(--space-lg);
      border-radius: 18px; }` (slightly larger pill).
   - `.message-form { padding: var(--space-md) var(--space-xl) var(--space-xl); }`
3. Active sidebar row:
   - Pass `active_chat_id` into `_chats_list.html` from both
     `index_endpoint` and `get_chat_panel_endpoint` (use
     `conversation.id if conversation else None`).
   - In `_chat_item.html`, conditionally set `aria-current="page"` on the
     `<li>` when `chat.id == active_chat_id`. **Use
     `active_chat_id|default(none)`** — the template is also rendered
     standalone by `list_chats_endpoint`, `create_chat_endpoint`,
     `rename_chat_endpoint`, and `get_chat_item_endpoint`, none of which
     pass that variable. Without `|default` Jinja raises
     `UndefinedError`.
   - `_chats_list.html` must also forward the var:
     `{% include "_chat_item.html" with context %}` already inherits, but
     callers of `list_chats_endpoint` (GET /chats) don't have a
     conversation in scope — explicitly pass `active_chat_id=None` so the
     variable exists.
   - CSS: `.chat-item[aria-current="page"] { background: var(--accent-tonal);
      color: var(--accent-tonal-text); }` plus the link inherits the text
      color.
4. Sage everywhere it should appear (no other code changes — these are
   already token-driven):
   - Send button & hover: already use `--accent` / `--accent-hover`. ✓
   - Focus ring: already uses `--accent`. ✓
   - User bubble: already uses `--accent-tonal`. ✓
   - Model badge: already uses `--accent-tonal` / `--accent-tonal-text`. ✓
   - Active sidebar row: added in step 3.
5. Remove or soften the `.compose__button` accent fill — it stays a
   filled button only until 11b removes it entirely.

Verification:
- Run `uvicorn main:app --reload`, open `http://localhost:8000`, click
  through: empty state, click a chat, send a message. Visually confirm
  greens and that the active chat row is highlighted.
- Re-run `pytest tests/test_routes.py` — any test substring-matching
  `#1a73e8` would fail; check first. (Inspection of phase 8's tests
  showed no token-string assertions, but verify before committing.)
- `pytest tests/test_routes.py::test_get_chat_item_returns_display_fragment`
  exercises the standalone `_chat_item.html` render path — confirms the
  `active_chat_id|default(none)` guard works.

---

## Sub-phase 11b — Empty-state composer; remove "Compose" (commit 2)

Goal: starting a new chat happens by typing into a centered composer on
the empty-state main panel, with a model picker as a toolbar directly
below the input. The sidebar gets a compact `+ New chat` link at the top
to return to that empty state from inside an existing chat. The
`<details class="compose">` disclosure disappears.

Critical files:
- `templates/index.html` — empty state currently shows
  `<div class="empty-state">…</div>`. Replace with the composer.
- `templates/_new_chat_form.html` — DELETE (no longer used) OR repurpose
  as a `_composer.html` partial included by `index.html`.
- `templates/_chat_item.html` (already added an "active" affordance in
  11a; no changes here).
- `app/routes.py` — extend `POST /chats` so the first user message is
  accepted in the same submission, the chat is created, the user message
  is appended, and the response wires up the chat panel + streaming
  placeholder in one round-trip.
- `static/style.css` — add `.composer`, `.composer__pill`,
  `.composer__toolbar`, `.sidebar__new-chat` styles.

Concrete changes:

1. New partial `templates/_composer.html` (used by the empty state):
   ```html
   {# Composer shown when no chat is open. Submitting creates a chat
      AND sends the first message in one round trip. #}
   <section class="composer">
     <h1 class="composer__greeting">What's on your mind?</h1>
     <form class="composer__form"
           hx-post="/chats"
           hx-target="#main"
           hx-swap="innerHTML"
           hx-push-url="true">
       <div class="composer__pill">
         <textarea name="content" required
                   placeholder="Message Ollama…"
                   rows="1"
                   hx-on:input="this.style.height='auto'; this.style.height=this.scrollHeight+'px'"
                   hx-on:keydown="if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); this.form.requestSubmit(); }"></textarea>
         <button type="submit" aria-label="Send">
           <span class="material-symbols-outlined">arrow_upward</span>
         </button>
       </div>
       <div class="composer__toolbar">
         <select name="model" required
                 hx-get="/models"
                 hx-trigger="load"
                 hx-target="this"
                 hx-swap="innerHTML">
           <option value="">Loading models…</option>
         </select>
       </div>
     </form>
   </section>
   ```

2. `templates/index.html`: replace the `{% else %}` empty-state block
   with `{% include "_composer.html" %}`. Remove the
   `{% include "_new_chat_form.html" %}` call. Add a sidebar header row
   that holds the `+ New chat` link:
   ```html
   <aside class="sidebar">
     <div class="sidebar__header">
       <h1>ollama_slowly</h1>
       <a class="sidebar__new-chat"
          href="/"
          hx-get="/new"
          hx-target="#main"
          hx-swap="innerHTML"
          hx-push-url="/"
          aria-label="New chat">
         <span class="material-symbols-outlined">add</span>
         New chat
       </a>
     </div>
     {% include "_chats_list.html" %}
     {# 11c will add a footer with the theme toggle here. #}
   </aside>
   ```
   Expose a tiny new fragment route `GET /new` that returns only the
   composer (so HTMX can swap `#main` without re-rendering the whole
   page):
   ```python
   @router.get("/new", response_class=HTMLResponse)
   def new_chat_endpoint(request: Request) -> Response:
       """Return the empty-state composer fragment (used by sidebar link)."""
       return templates.TemplateResponse(
           request=request, name="_composer.html", context={}
       )
   ```

3. Delete `templates/_new_chat_form.html` and remove every other
   include of it. Remove the `.compose` and `.compose__button` CSS rules
   in `static/style.css`. Any test that references the Compose disclosure
   (search for `compose` in `tests/`) must be updated or removed.

4. Extend `POST /chats` in `app/routes.py`:
   - Accept a new `content: Annotated[str, Form()]` parameter (the first
     user message). Drop the existing `name` parameter — we'll generate
     a placeholder name server-side (`"New chat"`).
   - Body:
     ```python
     chat = queries.create_conversation(db, name="New chat", model=model)
     user_message = queries.append_message(db, chat.id, "user", content)
     ```
   - Render `_chat_panel.html` for the new chat **with the assistant
     placeholder inlined inside `#messages`** (see step 4a). Return with
     `HX-Push-Url: /chats/{id}` so the URL updates and reload restores
     the same chat.
   - Also fire an OOB swap inserting the new sidebar row at the top of
     `#chats-list`. **Do not wrap in a fake `<ul>`** — HTMX's bare
     `hx-swap-oob="afterbegin"` only works when the element IS the
     target; it doesn't reach into a parent. Use selector syntax on the
     row itself:
     ```python
     item_html = templates.get_template("_chat_item.html").render(
         chat=chat, active_chat_id=chat.id, oob_position="afterbegin:#chats-list"
     )
     body = panel_with_placeholder + item_html
     ```
     where `_chat_item.html` renders
     `{% if oob_position %}hx-swap-oob="{{ oob_position }}"{% endif %}`
     on the root `<li>`. The default `hx-swap-oob` selector syntax is
     `swap-style:selector` (HTMX docs §"Selecting Content To Swap"), so
     `"afterbegin:#chats-list"` prepends the `<li>` to the existing list.

4a. **`_chat_panel.html` must accept an optional `pending_stream_url`**
   so the assistant placeholder renders INSIDE `#messages` (not OOB,
   which would race against `#main` not yet existing). Add:
   ```jinja
   {% for message in messages %}
     {% include "_message.html" %}
   {% endfor %}
   {% if pending_stream_url %}
     {% with conversation_id=conversation.id, stream_url=pending_stream_url %}
       {% include "_assistant_placeholder.html" %}
     {% endwith %}
   {% endif %}
   ```
   (Jinja2 doesn't support `with` as shown above natively — use a macro
   or just set variables in a `{% set %}` block at the top of the
   include, or render the placeholder template separately and pass its
   HTML in as a string variable. Pick whichever is least invasive when
   implementing; the existing pattern of passing rendered HTML in via
   `get_template().render()` and inserting as a string variable is
   already used in `create_chat_endpoint`.)

   POST /chats then becomes:
   ```python
   chat = queries.create_conversation(db, name="New chat", model=model)
   queries.append_message(db, chat.id, "user", content)
   messages = queries.list_messages(db, chat.id)
   panel_with_placeholder = templates.get_template("_chat_panel.html").render(
       conversation=chat,
       messages=messages,
       pending_stream_url=f"/chats/{chat.id}/stream",
       active_chat_id=chat.id,
   )
   ```

5. CSS additions in `static/style.css`:
   ```css
   .sidebar__header { display: flex; align-items: center;
     justify-content: space-between; gap: var(--space-md); }
   .sidebar__new-chat {
     display: inline-flex; align-items: center; gap: var(--space-xs);
     color: var(--text-secondary); text-decoration: none;
     padding: var(--space-xs) var(--space-sm); border-radius: var(--radius-md);
     font-size: 13px;
   }
   .sidebar__new-chat:hover { background: var(--surface-hover);
     color: var(--text-primary); }

   .composer { margin: auto; width: min(720px, 90%);
     display: flex; flex-direction: column; gap: var(--space-lg); }
   .composer__greeting { font-size: 28px; font-weight: 500;
     color: var(--text-primary); text-align: center; margin: 0; }
   .composer__form { display: flex; flex-direction: column;
     gap: var(--space-sm); margin: 0; }
   .composer__pill { /* same pill styling as .message-form__pill */
     display: flex; align-items: flex-end; gap: var(--space-sm);
     background: var(--bg); border: 1px solid var(--border);
     border-radius: 24px;
     padding: var(--space-sm) var(--space-sm) var(--space-sm) var(--space-lg);
   }
   .composer__pill:focus-within {
     border-color: var(--accent);
     box-shadow: 0 0 0 1px var(--accent);
   }
   .composer__pill textarea { /* same as .message-form textarea */ }
   .composer__pill button[type="submit"] { /* same as .message-form button */ }
   .composer__toolbar { display: flex; align-items: center; gap: var(--space-sm);
     padding-left: var(--space-md); }
   .composer__toolbar select {
     border: 1px solid var(--border); border-radius: var(--radius-md);
     padding: var(--space-xs) var(--space-md); background: var(--bg);
     color: var(--text-primary); font-size: 13px;
   }
   ```
   (To avoid duplicating the pill rules, refactor: rename
   `.message-form__pill` → `.input-pill`, apply it to both the composer
   and the in-chat message form. Choose this when implementing — easier
   to maintain than two near-identical blocks.)

Verification:
- Open `/`. Confirm no Compose disclosure; centered composer visible.
- Pick a model, type a message, hit Enter. The chat should appear in
  the sidebar, URL should update to `/chats/{id}`, assistant should
  stream a reply.
- Reload the page on `/chats/{id}` — chat panel renders with history
  intact.
- From inside the chat, click `+ New chat` in the sidebar — composer
  returns; URL goes to `/`.
- **Tests requiring rewrite** (audited against current `tests/test_routes.py`):
  - `test_create_chat_returns_201_with_chat_item` (L148) — drop `name`
    from the POST body; assert composer-to-panel transition response
    shape instead.
  - `test_index_includes_new_chat_form` (L198) — replace with
    `test_index_includes_composer`; assert `class="composer"`,
    `name="content"`, `name="model"` on the new form.
  - `test_index_renders_layout_with_empty_main` (L180) — currently
    asserts `"empty-state" in response.text`. After 11b the empty-state
    classname is gone; assert `class="composer"` instead.
  - `test_new_chat_form_model_dropdown_auto_loads_from_models` (L217) —
    rename selectors but the underlying behavior (model select with
    `hx-get="/models"`) is preserved in the composer.
  - `_create_chat_and_get_id` helper (L167) — must now POST
    `{"model": "llama3", "content": "hi"}` and parse the chat id from
    the chat-item OOB fragment in the response.
  - `test_chat_url_direct_hit_renders_full_page_with_panel` (L247) —
    still asserts `"empty-state" not in response.text`; remains valid
    (string is just gone).
  - ~12 other tests rely on `_create_chat_and_get_id`; they need no
    body changes but they all need the helper to keep working.
- `pytest tests/test_routes.py tests/test_integration.py`.

---

## Sub-phase 11c — Light/dark mode toggle (commit 3)

Goal: a manual light/dark toggle in the sidebar footer, default light,
persisted in `localStorage`. No OS-preference detection.

Critical files:
- `templates/base.html` — add the inline bootstrap script that reads
  `localStorage` and sets `data-theme` on `<html>` *before* the page
  paints (avoids a flash of light-mode content for users pinned to
  dark).
- `templates/index.html` — add a `<button>` toggle in the sidebar
  footer.
- `static/style.css` — add `[data-theme="dark"]` token overrides.

Concrete changes:

1. `templates/base.html` (inside `<head>`, *before* the stylesheet
   `<link>`s). **Always set `data-theme` explicitly** — see step 4
   below for why deleting the attribute would let Pico's
   `prefers-color-scheme: dark` rules leak through on Macs in OS-dark.
   ```html
   <script>
     // Apply persisted theme synchronously to avoid FOUC and to suppress
     // Pico's OS-driven dark-mode rules (which only fire when no
     // data-theme is set).
     try {
       const t = localStorage.getItem("theme") === "dark" ? "dark" : "light";
       document.documentElement.dataset.theme = t;
     } catch (e) {
       // localStorage unavailable — fall back to explicit light.
       document.documentElement.dataset.theme = "light";
     }
   </script>
   ```

2. `templates/index.html` — add a sidebar footer:
   ```html
   <div class="sidebar__footer">
     <button type="button" class="theme-toggle" aria-label="Toggle theme"
             onclick="
               const html = document.documentElement;
               const next = html.dataset.theme === 'dark' ? 'light' : 'dark';
               html.dataset.theme = next;
               try { localStorage.setItem('theme', next); } catch (e) {}
             ">
       <span class="material-symbols-outlined theme-toggle__sun">light_mode</span>
       <span class="material-symbols-outlined theme-toggle__moon">dark_mode</span>
     </button>
   </div>
   ```
   CSS will hide the inactive icon: in light mode show the moon (action:
   "switch to dark"); in dark mode show the sun.

3. `static/style.css` — add dark tokens:
   ```css
   [data-theme="dark"] {
     --bg: #1a1a1a;
     --surface: #232323;
     --surface-hover: #2c2c2c;
     --border: #2f2f2f;
     --text-primary: #ececec;
     --text-secondary: #a0a0a0;
     --accent: #9ccaa6;          /* same sage — readable on both */
     --accent-hover: #b3d8bb;    /* lighter on dark, not darker */
     --accent-tonal: #2a3a30;    /* deep sage surface */
     --accent-tonal-text: #cfe9d4;
     --shadow-sm: 0 1px 0 rgba(0,0,0,.35);
     --shadow-md: 0 4px 12px rgba(0,0,0,.45);
   }
   .sidebar__footer { margin-top: auto; padding-top: var(--space-md);
     border-top: 1px solid var(--border); }
   .theme-toggle { background: none; border: none; cursor: pointer;
     color: var(--text-secondary); padding: var(--space-sm);
     border-radius: var(--radius-md); }
   .theme-toggle:hover { background: var(--surface-hover);
     color: var(--text-primary); }
   .theme-toggle__moon { display: inline; }
   .theme-toggle__sun  { display: none; }
   [data-theme="dark"] .theme-toggle__moon { display: none; }
   [data-theme="dark"] .theme-toggle__sun  { display: inline; }
   ```

4. **Pico bleed-through is real but avoidable.** Confirmed by grep
   against `static/pico.classless.min.css`: Pico's dark rules are
   scoped to `:host(:not([data-theme])),:root:not([data-theme])`. So as
   long as we ALWAYS set `data-theme` (either `light` or `dark`) on
   `<html>`, Pico's `@media (prefers-color-scheme: dark)` block is
   suppressed. That's why step 1's bootstrap script never deletes the
   attribute and always writes one of the two values.
   - Smoke test by setting macOS to dark mode and visiting `/` with no
     `theme` in localStorage; the app must paint light. Then toggle to
     dark; reload; must paint dark. Toggle back; reload; must paint
     light again.

Verification:
- With OS in light mode, toggle to dark → entire app flips. Reload →
  stays dark. Toggle back → flips and persists.
- With OS in dark mode, toggle to light → app stays light (no Pico
  bleed-through).
- All four screens (empty composer, chat panel, sidebar with active
  row, kebab popover) render legibly in both themes.
- No new tests required; this is template + CSS only.

---

## Sub-phase 11d — Auto-generated chat titles (commit 4)

Goal: after the 1st, 2nd, and 3rd assistant responses complete, fire a
non-blocking title-generation request to `tinyllama:1.1b-chat-v1-fp16`
and replace the placeholder name in the sidebar. Manual renames lock
future auto-titles. If the model isn't installed, surface a one-time
warning and skip silently thereafter.

Critical files:
- `app/db.py` — schema change: add `name_locked INTEGER NOT NULL DEFAULT 0`
  to `conversations`. Schema runs through `CREATE TABLE IF NOT EXISTS`;
  add a follow-up `ALTER TABLE ... ADD COLUMN` guarded by a
  `PRAGMA table_info(...)` check so existing databases pick it up.
- `app/queries.py` — surface `name_locked` on the `Conversation`
  dataclass; new helper `set_name_auto(conn, id, new_name)` that updates
  the name *only if* `name_locked = 0` (single `UPDATE ... WHERE
  name_locked = 0` and check `rowcount`); update `rename_conversation`
  to set `name_locked = 1`.
- `app/ollama.py` — add `async def generate_title(client, history)`:
  one-shot, non-streaming `POST /api/chat` with `stream=False`, returning
  the trimmed reply. Reuses the existing `OllamaUnavailable` /
  `OllamaProtocolError` taxonomy. If Ollama returns 404 on the model
  (model not installed), raise a new `OllamaModelMissing(model)`.
- `app/routes.py` —
  - After `_stream_assistant_reply` finishes saving the assistant
    message, count assistant messages in this conversation. If
    `1 <= count <= 3` AND `not conversation.name_locked`, schedule
    title generation. (Run it inline at the tail of the SSE generator,
    *after* the `done` event has been yielded — this keeps the user-
    visible response time unchanged. It does mean the SSE stream stays
    open a couple seconds longer; acceptable for a single-user app.)
  - On title success, yield a final SSE event `event: title` carrying
    HTML for the renamed `<li>` *with* `hx-swap-oob="true"` so HTMX
    swaps it in the sidebar — and update the page's `<title>` via a
    second OOB swap target if desired (defer; not required for v1).
  - On `OllamaModelMissing`, yield an SSE event `event: title-warning`
    carrying a banner fragment. The fragment targets a `#title-warning`
    div in the sidebar footer; a small JS snippet sets a
    `sessionStorage` flag so subsequent fires don't re-render the
    banner.
- `templates/_chat_item.html` — already has `id="chat-{{ chat.id }}"`,
  so OOB swap by id works without changes.
- `templates/_assistant_placeholder.html` — wire `sse-swap` to listen
  for `title` and `title-warning` events too (or rely on default-event
  handling).
- New template `templates/_title_warning.html` — the one-time banner.

Concrete changes:

1. **Schema migration** in `app/db.py`:
   ```python
   _SCHEMA_SQL = """
   ...
   CREATE TABLE IF NOT EXISTS conversations (
       id           INTEGER PRIMARY KEY,
       name         TEXT NOT NULL,
       model        TEXT NOT NULL,
       name_locked  INTEGER NOT NULL DEFAULT 0,
       created_at   TEXT NOT NULL,
       updated_at   TEXT NOT NULL
   );
   ...
   """

   def _ensure_name_locked_column(conn: sqlite3.Connection) -> None:
       """Add name_locked to existing databases that pre-date 11d."""
       cols = {row[1] for row in conn.execute(
           "PRAGMA table_info(conversations);"
       )}
       if "name_locked" not in cols:
           conn.execute(
               "ALTER TABLE conversations"
               " ADD COLUMN name_locked INTEGER NOT NULL DEFAULT 0;"
           )

   def initialize_database(...):
       ...
       with sqlite3.connect(target) as conn:
           conn.execute("PRAGMA foreign_keys = ON;")
           conn.executescript(_SCHEMA_SQL)
           _ensure_name_locked_column(conn)
       return target
   ```

2. **`app/queries.py`** updates:
   - `Conversation` gains `name_locked: bool`.
   - `_row_to_conversation` reads `bool(row["name_locked"])`.
   - All SELECTs include `name_locked` in the column list.
   - `create_conversation` inserts `name_locked = 0` explicitly (so the
     value is visible in code, not relying solely on the DEFAULT).
   - `rename_conversation` adds `, name_locked = 1` to its SET clause.
   - New:
     ```python
     def set_name_auto(conn, conversation_id, new_name) -> Conversation | None:
         """Auto-set the name iff the chat hasn't been manually renamed."""
         with conn:
             row = conn.execute(
                 "UPDATE conversations SET name = ?, updated_at = ?"
                 " WHERE id = ? AND name_locked = 0"
                 " RETURNING id, name, model, name_locked, created_at, updated_at;",
                 (new_name, _now_iso(), conversation_id),
             ).fetchone()
         return _row_to_conversation(row) if row else None
     ```
   - New:
     ```python
     def count_assistant_messages(conn, conversation_id: int) -> int:
         row = conn.execute(
             "SELECT COUNT(*) FROM messages"
             " WHERE conversation_id = ? AND role = 'assistant';",
             (conversation_id,),
         ).fetchone()
         return row[0]
     ```

3. **`app/ollama.py`** additions:
   ```python
   TITLE_MODEL = "tinyllama:1.1b-chat-v1-fp16"

   class OllamaModelMissing(Exception):
       """The requested model isn't installed in Ollama."""

   async def generate_title(client, history) -> str:
       """One-shot non-streaming title generation."""
       prompt = (
           "Summarize this conversation in 3-6 words as a concise title."
           " Reply with only the title, no quotes, no punctuation at the end."
       )
       messages = [
           *history,
           {"role": "user", "content": prompt},
       ]
       payload = {"model": TITLE_MODEL, "messages": messages, "stream": False}
       try:
           response = await client.post("/api/chat", json=payload)
       except (httpx.HTTPError, httpx.InvalidURL) as e:
           raise OllamaUnavailable(f"Title request failed: {e}") from e
       if response.status_code == 404:
           # Ollama returns 404 when the model isn't installed.
           raise OllamaModelMissing(TITLE_MODEL)
       try:
           response.raise_for_status()
       except httpx.HTTPStatusError as e:
           raise OllamaUnavailable(f"Title request failed: {e}") from e
       try:
           text = response.json()["message"]["content"].strip()
       except (json.JSONDecodeError, KeyError, TypeError) as e:
           raise OllamaProtocolError(
               f"Ollama returned an unexpected /api/chat shape: {e}"
           ) from e
       # Strip surrounding quotes the model might add despite instructions.
       return text.strip(' "“”\'').splitlines()[0][:80]
   ```

4. **`app/routes.py`** — at the bottom of `_stream_assistant_reply`,
   after the existing `yield _sse(final_html, event="done")`:
   ```python
   if on_complete != "append":
       return  # Only the "send new message" flow triggers titles.
   conversation = queries.get_conversation(db, conversation_id)
   if conversation.name_locked:
       return
   count = queries.count_assistant_messages(db, conversation_id)
   if not 1 <= count <= 3:
       return
   try:
       full_history = queries.list_messages(db, conversation_id)
       title = await ollama.generate_title(
           client,
           [{"role": m.role, "content": m.content} for m in full_history],
       )
   except ollama.OllamaModelMissing:
       yield _sse(
           templates.get_template("_title_warning.html").render(),
           event="title-warning",
       )
       return
   except (OllamaUnavailable, OllamaProtocolError):
       return  # Silent fail per spec.
   updated = queries.set_name_auto(db, conversation_id, title)
   if updated is None:
       return  # User renamed concurrently; respect the lock.
   # Render the row WITH hx-swap-oob="true" baked into its own root <li>.
   # Wrapping in an extra <li> would produce invalid nested-li HTML.
   row_html = templates.get_template("_chat_item.html").render(
       chat=updated,
       active_chat_id=updated.id,
       oob_swap=True,
   )
   yield _sse(row_html, event="title")
   ```
   `_chat_item.html` needs a one-line addition on its root `<li>`:
   `{% if oob_swap %} hx-swap-oob="true"{% endif %}`. The existing
   `id="chat-{{ chat.id }}"` is what HTMX matches on.

5. **`templates/_assistant_placeholder.html`** — extend `sse-swap` to
   include `title,title-warning` so the events reach the DOM. The
   current template (verified) is a single-line element with
   `sse-swap="token,done,error" hx-swap="beforeend"`. Add the two new
   event names:
   ```html
   <div id="assistant-stream-{{ conversation_id }}"
        class="message message--assistant message--streaming"
        data-role="assistant"
        hx-ext="sse"
        sse-connect="{{ stream_url }}"
        sse-swap="token,done,title,title-warning,error"
        hx-swap="beforeend"></div>
   ```
   Both `title` and `title-warning` fragments carry their own
   `hx-swap-oob` attributes, so HTMX takes them out-of-band and the
   placeholder's `hx-swap="beforeend"` is a no-op for those events
   (good — we don't want the title row appended into the chat bubble).

6. **`templates/_title_warning.html`** (new). MUST carry
   `hx-swap-oob="true"` on its root or the SSE swap would dump it into
   the streaming assistant placeholder (which is the element that
   triggered the SSE connection):
   ```html
   <div id="title-warning" class="title-warning" hx-swap-oob="true"
        hx-on:htmx:load="if (sessionStorage.getItem('title_warning_seen')) this.remove();
                          else sessionStorage.setItem('title_warning_seen', '1');">
     Auto-titles disabled — install <code>tinyllama:1.1b-chat-v1-fp16</code> to enable.
     <button type="button" onclick="this.parentElement.remove()" aria-label="Dismiss">
       <span class="material-symbols-outlined">close</span>
     </button>
   </div>
   ```
   Mount point: add `<div id="title-warning"></div>` inside
   `.sidebar__footer` in `index.html`. HTMX matches the id between the
   OOB fragment and the existing slot, replacing the empty placeholder
   with the warning. (Use the same id on both sides — no `-slot`
   suffix.)

7. **Tests** (in `tests/test_queries.py` and `tests/test_routes.py`):
   - `test_rename_sets_name_locked` — after `rename_conversation`,
     `Conversation.name_locked is True`.
   - `test_set_name_auto_respects_lock` — calling `set_name_auto` after
     a manual rename returns `None` and leaves the name unchanged.
   - `test_count_assistant_messages` — 0/1/2/3 after stepwise appends.
   - Integration: monkey-patch `ollama.generate_title` to return a known
     string; POST a message via the chat endpoint, drain the SSE stream,
     assert the sidebar row carries the new name.
   - Integration: monkey-patch `generate_title` to raise
     `OllamaModelMissing`; assert the SSE stream contains the warning
     fragment.

Verification:
- Send a first message in a brand-new chat. Watch the sidebar row name
  change from "New chat" to the model-generated title within a couple
  seconds.
- Send a second and third message; confirm the title refreshes each
  time.
- Manually rename the chat via the kebab → Rename flow. Send a 4th (or
  earlier) message; confirm the title does NOT change.
- Temporarily set `TITLE_MODEL = "definitely-not-installed:0"` in
  `app/ollama.py`, restart, send a message; confirm the warning banner
  appears in the sidebar footer and does NOT reappear on the next
  message in the same session.
- `pytest` across the full suite.

---

## Cross-cutting verification (after all four sub-phases)

1. Confirm Ollama is running locally (the app does not start it).
2. `pytest` — full suite must pass; phase 9/10 tests will need
   maintenance for the Compose removal and the new
   `Conversation.name_locked` field.
3. `uvicorn main:app --reload` and walk the full happy path:
   - Land on `/` → empty composer.
   - Pick a model, send a message → chat panel + streaming reply.
   - Sidebar shows the new chat; title regenerates within ~2s.
   - Reload `/chats/{id}` → state intact.
   - Toggle dark mode, reload → stays dark.
   - Rename a chat → no further auto-title runs.
4. Commit each sub-phase separately, with an explicit ask before each
   commit (per `PLAN.md`'s working rules).
