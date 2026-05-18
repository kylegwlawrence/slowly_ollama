# Phase 9 — Frontend bug fixes

## Context

Phase 8 (Google Chat aesthetic) shipped a redesigned UI. Browser testing
surfaced five issues that the unit tests' substring assertions can't see.
Each fix is small and isolated — this phase is bug fixes only, no scope
expansion. The previous Phase 9 (full test suite) becomes Phase 10.

## The five bugs

1. **POST /chats doesn't open the new chat panel.** Returning a chat row
   to prepend to the sidebar isn't enough — the user has to click the
   new row to actually load the chat. Should auto-open and update the URL.
2. **Enter doesn't send; Shift+Enter doesn't add a newline.** Default
   textarea behavior makes both keys insert a newline; user has to click
   the round Send button. Should follow chat-app convention.
3. **Typed text in the chat textarea is too light.** No `color` rule on
   `.message-form textarea` in style.css — Pico's default applies and
   it's too muted.
4. **Deleting the currently-viewed chat doesn't clear the panel.** The
   existing inline JS check on `window.location.pathname` is brittle
   (likely the element is detached before the after-request fires).
5. **Renaming a chat doesn't work.** Symptom unclear from static
   analysis — could be the kebab popup → click flow, the edit form
   submission, or the PATCH response swap. Must investigate live.

## Critical files

- `app/routes.py` — `create_chat_endpoint` (Bug 1), `delete_chat_endpoint` (Bug 4)
- `templates/_chat_panel.html` — textarea keydown (Bug 2)
- `static/style.css` — textarea `color` (Bug 3)
- `templates/_chat_item.html` — remove brittle inline JS (Bug 4)
- `templates/_chat_item.html` and `templates/_chat_item_edit.html` — verify rename flow (Bug 5)
- `docs/plans/PLAN.md` — renumber Phase 9 → 10

---

## Commit 1 — `docs: renumber PLAN phase 9 → 10, insert frontend-fixes`

Edit `docs/plans/PLAN.md`: replace the "Phase 9 — full test suite" block
with a "Phase 9 — frontend bug fixes" block describing the five fixes,
and renumber the test-suite block to "Phase 10".

---

## Commit 2 — Fix Bug 1: POST /chats opens the new chat panel

`app/routes.py` `create_chat_endpoint`: return the new sidebar `<li>`
(main response, prepended into `#chats-list` by the form's
`hx-swap="afterbegin"`) AND an out-of-band
`<div id="main" hx-swap-oob="innerHTML">…</div>` containing the new
chat panel. Add `HX-Push-Url: /chats/{id}` header so the URL syncs.

Test impact: `test_create_chat_returns_201_with_chat_item` only
substring-matches `'class="chat-item"'` — preserved in the new
response. Tests stay green.

---

## Commit 3 — Fix Bug 2: Enter sends, Shift+Enter adds a newline

`templates/_chat_panel.html`: add a second `hx-on` to the textarea:

```html
hx-on:keydown="if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); this.form.requestSubmit(); }"
```

- `!event.shiftKey` lets Shift+Enter fall through to a newline.
- `!event.isComposing` skips IME composition (Japanese / Chinese / Korean).
- `this.form.requestSubmit()` triggers HTMX submit the same way the
  Send button does.

Test impact: none.

---

## Commit 4 — Fix Bug 3: Set explicit textarea text color

`static/style.css`: add `color: var(--text-primary);` to the
`.message-form textarea` rule so Pico's muted default doesn't bleed
through.

Test impact: none.

---

## Commit 5 — Fix Bug 4: Delete clears the panel when viewing the chat

`app/routes.py` `delete_chat_endpoint`: read the `Referer` header; if
it ends with `/chats/{conversation_id}`, set `HX-Location: /` on the
response so HTMX navigates the page to the index.

`templates/_chat_item.html`: remove the brittle inline-JS
`hx-on::after-request="if (...) window.location = '/'"` from the
delete button.

`tests/test_routes.py`: drop the `assert "window.location" in
response.text` line from `test_chat_item_has_delete_button` and add
two new tests:

- `test_delete_chat_emits_hx_location_when_viewing_deleted_chat` —
  Referer ends with the deleted chat's path → response has
  `HX-Location: /`.
- `test_delete_chat_omits_hx_location_when_viewing_different_chat` —
  Referer is `/` → response has no `HX-Location` header.

End state: 92 tests passing (was 91; +1 net from the test split).

---

## Commit 6 — Fix Bug 5: Renaming works end-to-end

Cause not determinable from static analysis. Live-debug:

```bash
source .venv/bin/activate && uvicorn main:app --port 8000
```

Open http://localhost:8000 and step through Click kebab → Rename →
edit form → submit. Inspect Network tab for the GET /chats/{id}/edit
and PATCH /chats/{id} requests. Identify which step fails.

Likely candidates:

- **A: Kebab popup hides before Rename click registers.** Some
  browsers don't focus buttons reliably on mouseup;
  `:focus-within` may briefly fail. Switch to a
  `popovertarget`/`popover` pair (Chrome 114+, Safari 17+,
  Firefox 125+).
- **B: Edit form appears but submit doesn't fire.** Pressing Enter
  in the input should submit; if not, check for stopPropagation.
- **C: PATCH succeeds but the swap doesn't apply.** Inspect the
  response; verify the form's `hx-target` matches the row's id.
- **D: HTMX PATCH body encoding.** Check
  `htmx.config.methodsThatUseUrlParams`.

After fixing, add a round-trip test that exercises GET-edit →
PATCH and asserts the rename took effect:

```python
def test_rename_round_trip_via_edit_and_patch(make_client):
    with make_client(_ollama_unreachable) as client:
        chat_id = _create_chat_and_get_id(client, "Original")
        edit = client.get(f"/chats/{chat_id}/edit")
        assert edit.status_code == 200
        assert "chat-item--editing" in edit.text
        assert 'value="Original"' in edit.text

        patch = client.patch(f"/chats/{chat_id}", data={"name": "Renamed"})
        assert patch.status_code == 200
        assert "Renamed" in patch.text
        assert "chat-item--editing" not in patch.text
```

End state: 93 tests passing.

---

## Verification

After all commits land, manual walkthrough:

```bash
source .venv/bin/activate && uvicorn main:app --port 8000
```

1. **Create a new chat** → new row prepends to sidebar AND the empty
   chat panel opens in `#main`; URL updates to `/chats/{id}`. (Bug 1)
2. **Send a message with Enter** → message sends. **Shift+Enter** →
   newline. (Bug 2)
3. **Typed text** is the same dark gray as message content. (Bug 3)
4. **Delete the currently-viewed chat** → row gone from sidebar,
   panel reverts to empty state, URL → `/`. (Bug 4)
5. **Delete a different chat** → row gone; current chat's panel
   untouched, URL stays. (Bug 4)
6. **Rename a chat** → input appears with current name, Enter or
   click ✓ saves; Cancel reverts. (Bug 5)

---

## What's NOT in this phase

- No new features (auto-naming, model switching, multi-tab safety, etc.).
- No CSS framework changes (Pico stays).
- No new dependencies.
- No backend schema changes.
- Phase 10 (full test suite) remains the next phase after this one.
