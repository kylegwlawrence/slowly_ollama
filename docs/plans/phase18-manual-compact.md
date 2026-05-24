# Phase 18 — Manual chat compaction

## Context

On a 16 GB M1, what matters for local inference is the **per-turn prompt size**,
not the row count in SQLite. Every turn re-sends the full conversation to
Ollama as the `messages` payload (`_build_history_payload` →
`stream_chat` / `maybe_tool_call`, `app/generation.py:392-464`). The KV cache
for that prompt shares RAM with the model weights — a long chat against a
7-8 B Q4 model leaves very little headroom before swap thrash, and once
`num_ctx` is exceeded Ollama silently drops the head of the conversation, so
the model "forgets" the user's earliest instructions with no UI signal.

Phase 17 already exposes `num_ctx` as a per-project setting (and surfaces
per-turn `prompt_tokens` from Ollama on each assistant row, `app/db.py:106-108`,
`app/queries/messages.py:23-77`), but neither lets the user *shorten* what gets
sent — they only choose how much of it Ollama has to chew on. This phase adds
a user-triggered **Compact** action that summarizes the earlier portion of a
chat into a single synthetic `summary` row, **soft-archives** the originals,
and the generation layer thereafter sends only the summary + recent turns.

The shape is deliberately **manual, not automatic** — same family as
phase 16's user-invoked agents. The user decides when context bloat is hurting
them, looks at the summary the model produced, and either keeps it or undoes
it. Auto-compaction (e.g. on hitting a token threshold) is out of scope; this
plan leaves the door open by keeping originals in the DB and gating on a
single endpoint.

## Decisions (locked with the user)

- **Soft-archive, not delete.** Originals get an `archived_at` stamp and are
  excluded from the prompt; rows stay in the DB so the action is reversible.
  Hard-delete is one bad summary away from losing turns the user wanted, which
  on a single-user local app is the worse failure mode.
- **One active summary at a time.** Re-compacting a chat that already has a
  summary archives the prior summary along with the newly-old turns and
  inserts a fresh summary that subsumes both. No "stacked summaries" view to
  reason about.
- **Keep the last K turns intact.** K = **4 messages** (= ~2 user/assistant
  pairs) by default. Captures the immediate working context — the active
  thread of thought — and is hardcoded for v1.
- **Summarize with the chat's own model.** It's already loaded in Ollama's
  memory from the last turn, so the summarization round-trip is cheap (no
  second model resident). A future enhancement can let the user pin a smaller
  "compaction model"; that's out of scope here.
- **Summary surfaces as a visible bubble**, not hidden behind a toggle. The
  user has to be able to see what the model knows in order to trust the
  feature. A collapsed "▸ Show N archived messages" affordance lets them
  audit the originals.
- **Compaction is synchronous.** A non-streaming `/api/chat` call against an
  already-warm model on a few hundred tokens of input completes in 1–5 s
  locally. No need for the SSE-producer machinery here.

## Non-goals (v1)

- **Auto-compact on threshold.** The "compact when `prompt_tokens` >
  0.75 × num_ctx" loop is mechanically a one-liner around the same endpoint;
  ship the manual flow first.
- **Picking a smaller compaction model.** No app_settings key, no UI. The
  chat's model is the summarizer.
- **Per-turn "uncompact"** UI button. The DB makes it possible (set
  `archived_at = NULL` on the targeted rows, delete the summary row); we just
  don't ship a button. The collapsed-originals viewer is a window into the
  rows, not a restore affordance.
- **Compaction during a live generation.** The endpoint 409s if
  `live_generations` has an in-flight task for the conversation.

---

## Part A — Schema + queries

### A1. New column on `messages`

Add a single nullable column:

```sql
-- in _SCHEMA_SQL (app/db.py)
archived_at TEXT  -- ISO 8601 UTC; NULL = active row, set = hidden from prompt
```

…and a migration helper mirroring `_ensure_messages_token_count_columns`
(`app/db.py:392-413`):

```python
def _ensure_messages_archived_at_column(conn: sqlite3.Connection) -> None:
    """Backfill `archived_at` on legacy messages tables."""
    columns = {row[1] for row in conn.execute(
        "PRAGMA table_info(messages);"
    )}
    if "archived_at" not in columns:
        conn.execute(
            "ALTER TABLE messages ADD COLUMN archived_at TEXT;"
        )
```

Call site in `initialize_database`, after the token-count migration and the
role-check drop:

```python
_ensure_messages_archived_at_column(conn)
```

Also add a partial index so the per-turn "list active messages" query stays
fast on chats with thousands of archived rows:

```sql
CREATE INDEX IF NOT EXISTS idx_messages_active
ON messages (conversation_id, created_at)
WHERE archived_at IS NULL;
```

### A2. New role: `summary`

Extend the `Role` literal (`app/queries/_models.py:18-23`):

```python
Role = Literal[
    "user",
    "assistant",
    "tool_call",
    "tool_result",
    "summary",   # synthetic; produced by the manual-compact endpoint
]
```

No DB-schema change: the `messages.role` CHECK was dropped in phase 12a
(`app/db.py:89-92`); the Python literal is the only gate.

A `summary` row's `content` is the model-generated summary text. It's an
**active** row (`archived_at IS NULL`) until the next compact archives it.

### A3. `Message` dataclass

Add a single field (`app/queries/_models.py`, the `Message` dataclass):

```python
archived_at: datetime | None = None
```

Update `_row_to_message` in `app/queries/messages.py:10-20` to map the column,
and update every `SELECT` in that file to include it. Three SELECTs to touch:
the two `RETURNING` clauses in `append_message` /
`replace_last_assistant_message`, and the `SELECT` in `list_messages`.

### A4. New query helpers

In `app/queries/messages.py`:

```python
def list_active_messages(
    conn: sqlite3.Connection, conversation_id: int
) -> list[Message]:
    """Return messages where archived_at IS NULL, oldest first.

    Used by the generation layer so archived turns are excluded from the
    prompt. Rendering still uses `list_messages` (which returns everything)
    so the chat panel can show a "▸ N archived messages" affordance.
    """
    rows = conn.execute(
        "SELECT id, conversation_id, role, content, created_at,"
        "  prompt_tokens, eval_tokens, archived_at"
        " FROM messages"
        " WHERE conversation_id = ? AND archived_at IS NULL"
        " ORDER BY created_at ASC, id ASC;",
        (conversation_id,),
    ).fetchall()
    return [_row_to_message(r) for r in rows]


def archive_messages_before(
    conn: sqlite3.Connection,
    conversation_id: int,
    cutoff_message_id: int,
) -> int:
    """Mark every active message in `conversation_id` with id < cutoff as archived.

    Returns the count of rows updated. Idempotent: re-running with the same
    cutoff is a no-op because the WHERE clause filters out already-archived
    rows. Bumps the conversation's `updated_at` in the same transaction so
    the sidebar reorder fires.
    """
    now = _now_iso()
    with conn:
        cursor = conn.execute(
            "UPDATE messages SET archived_at = ?"
            " WHERE conversation_id = ?"
            "   AND id < ?"
            "   AND archived_at IS NULL;",
            (now, conversation_id, cutoff_message_id),
        )
        if cursor.rowcount > 0:
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?;",
                (now, conversation_id),
            )
        return cursor.rowcount


def count_archived_messages(
    conn: sqlite3.Connection, conversation_id: int
) -> int:
    """Count rows where archived_at IS NOT NULL — used by the render-time pill."""
    row = conn.execute(
        "SELECT COUNT(*) FROM messages"
        " WHERE conversation_id = ? AND archived_at IS NOT NULL;",
        (conversation_id,),
    ).fetchone()
    return row[0]
```

`append_message` does NOT need an `archived_at` parameter — new rows are
always active. The compact endpoint inserts the synthetic `summary` row via
the existing `append_message(..., role="summary", ...)`, then calls
`archive_messages_before` to archive everything older than that row.

---

## Part B — Summarization (`app/ollama.py`)

Add a single helper. Mirrors `generate_title` (one-shot, non-streaming) but
keeps the model output raw — no quote-stripping, no word cap, no preamble
heuristics. The summarization prompt is plain enough that the model's reply
is usable as-is.

```python
async def summarize_conversation(
    client: httpx.AsyncClient,
    model: str,
    history: list[dict[str, str]],
    *,
    num_ctx: int | None = None,
) -> str:
    """Ask `model` to summarize `history` into a compact briefing.

    Single-shot non-streaming POST to /api/chat. The chat's own model is
    used — it's already warm from the previous turn, so the round-trip is
    cheap and we avoid loading a second model.

    Args:
        client: Async httpx client pointed at Ollama.
        model: Identifier of an installed model.
        history: Conversation rows in Ollama wire format. The caller is
            responsible for choosing what's "old enough to compact" — this
            function summarizes everything it's handed.
        num_ctx: Per-project context-window override; same plumbing as
            `stream_chat`. None → Ollama default.

    Returns:
        Stripped summary text. Empty string when the model returned nothing
        usable; the caller is expected to treat empty as "skip — don't
        archive anything."
    """
    instruction = {
        "role": "user",
        "content": (
            "Summarize the conversation above into a compact briefing that"
            " preserves what the assistant needs to keep responding well."
            " Keep:"
            "\n- the user's stated goals, constraints, and preferences"
            "\n- concrete facts, decisions, and conclusions"
            "\n- findings from tool calls (what was asked, what was found)"
            "\n- open questions and unresolved threads"
            "\n- any persona or style instructions the user gave"
            "\nOmit pleasantries, restated questions, and long verbatim"
            " quotes. Write a third-person briefing, not dialogue."
            " Under ~400 words. Begin directly — no preamble."
        ),
    }
    options: dict = {"temperature": 0.2}  # low-creativity for faithful recall
    if num_ctx is not None:
        options["num_ctx"] = num_ctx
    payload = {
        "model": model,
        "messages": [*history, instruction],
        "stream": False,
        "options": options,
    }
    try:
        response = await client.post(
            "/api/chat", json=payload, timeout=120.0
        )
        response.raise_for_status()
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        raise OllamaUnavailable(f"Compaction request failed: {e}") from e
    try:
        text = response.json()["message"]["content"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise OllamaProtocolError(
            f"Ollama returned an unexpected /api/chat shape: {e}"
        ) from e
    return text.strip()
```

`temperature=0.2` is hardcoded — compaction is a recall task, not a
generative one, and we don't want the chat's own (possibly creative)
temperature affecting it.

---

## Part C — Generation integration

### C1. Switch to active-only history

`_build_history_payload` (`app/generation.py:392-464`) gains one new branch
in the role walk:

```python
elif m.role == "summary":
    # Synthetic row produced by the manual-compact endpoint. Inject as a
    # system message so the model treats it as background context, not a
    # past turn it has to respond to. Multiple `system` messages are
    # acceptable in the Ollama wire format; this one falls AFTER the
    # per-turn system prompt (agent / project / tool nudge) because it
    # appears later in `history`, which is the right precedence — the
    # current turn's instructions win.
    out.append({
        "role": "system",
        "content": f"Earlier conversation summary:\n\n{m.content}",
    })
    skip_next_result = False
```

Place it between the existing `tool_result` and `user/assistant` branches.

Three call sites in `app/generation.py` switch from `queries.list_messages` to
`queries.list_active_messages`:

- `_run_generation` line **830** (`working_history = queries.list_messages(...)`
  inside the tool-call loop)
- The initial `history` passed to `start_generation` from
  `app/routes/chats.py` (send / regenerate / create endpoints — see C2)
- `_maybe_emit_title` line **493**
  (`full_history = queries.list_messages(...)`) — the auto-titler also wants
  to see only active rows so the title reflects the current effective context

### C2. Route call sites

Every route that builds `history` and passes it to `start_generation` must
use `list_active_messages` instead. Audit (`grep -n "list_messages" app/routes/chats.py`):

- `send_message_endpoint`
- `regenerate_endpoint`
- `create_chat_endpoint` (only relevant after first user turn — the post-message
  history is what gets passed)

The replacement is a one-call swap (`list_messages` → `list_active_messages`).
Type signatures don't change; both return `list[Message]`.

---

## Part D — The compact endpoint

New route in `app/routes/chats.py`, placed alongside the agent endpoint
(line 435):

```python
@router.post("/chats/{conversation_id}/compact", response_class=HTMLResponse)
async def compact_chat_endpoint(
    request: Request,
    conversation_id: int,
    db: DB,
    client: OllamaClient,
) -> Response:
    """Summarize the older portion of a chat into a single `summary` row.

    Keeps the most-recent KEEP_RECENT messages active; archives everything
    older (including any prior `summary` row); inserts a fresh `summary` row
    whose content is the model-generated briefing. Returns the re-rendered
    `#messages` div so the caller can swap it in via `outerHTML:#messages`.

    Raises:
        HTTPException 404: Unknown conversation.
        HTTPException 409: A generation is in flight for this chat.
        HTTPException 422: Nothing to compact (fewer than KEEP_RECENT + 2
            active messages, or the only active rows are already a summary +
            recent turns with nothing older).
        HTTPException 502: Ollama is unreachable or returned a bad response.
    """
    try:
        conversation = queries.get_conversation(db, conversation_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))

    # In-flight gate. Compacting mid-generation would race the producer's
    # `working_history = list_messages(...)` rebuild and corrupt the turn.
    state = live_generations.get(conversation_id)
    if state is not None and not state.done:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Cannot compact while a response is generating.",
        )

    active = queries.list_active_messages(db, conversation_id)
    # Find the cutoff: keep the last KEEP_RECENT *renderable* (user/assistant)
    # messages plus any trailing tool rows. Anything older becomes the
    # corpus we summarize.
    to_summarize, to_keep = _split_for_compact(active, KEEP_RECENT)
    if not to_summarize:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Nothing to compact yet.",
        )

    # Effective num_ctx for the summarization call: project override or
    # global default. Matches what generation uses.
    project = queries.get_project_for_conversation(db, conversation_id)
    num_ctx = queries.resolve_num_ctx_for_project(db, project.num_ctx)

    try:
        summary_text = await ollama.summarize_conversation(
            client,
            conversation.model,
            _build_history_payload(to_summarize),
            num_ctx=num_ctx,
        )
    except (OllamaUnavailable, OllamaProtocolError) as e:
        raise HTTPException(status.HTTP_BAD_GATEWAY, str(e))

    if not summary_text:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Compaction model returned empty text.",
        )

    # Insert the synthetic summary row, then archive everything strictly
    # older than it. Ordering matters: append first to get the new row's id,
    # then archive everything with id < that id (the summary's id is
    # auto-incremented, so it is guaranteed greater than every existing row).
    summary_row = queries.append_message(
        db, conversation_id, "summary", summary_text
    )
    queries.archive_messages_before(db, conversation_id, summary_row.id)

    # Re-render the whole messages div. Cheaper than crafting a precise OOB
    # delta for "the head N rows go away and a summary appears" — for a
    # one-off user action, simpler wins.
    messages = queries.list_messages(db, conversation_id)
    blocks = render.group_messages_for_render(messages)
    return templates.TemplateResponse(
        request=request,
        name="_messages_inner.html",  # new — extracted from _chat_panel.html
        context={
            "conversation": conversation,
            "blocks": blocks,
            "archived_count": queries.count_archived_messages(
                db, conversation_id
            ),
        },
        headers={"HX-Reswap": "outerHTML"},  # hits #messages, not its child
    )
```

…where `KEEP_RECENT = 4` and the helper:

```python
def _split_for_compact(
    active: list[Message], keep_recent: int
) -> tuple[list[Message], list[Message]]:
    """Return (rows-to-summarize, rows-to-keep).

    `keep_recent` is counted in user/assistant rows, not raw rows; tool_call
    / tool_result rows attached to a kept assistant turn stay with it
    (otherwise the kept turn references tool results the prompt no longer
    contains, and Ollama 400s on orphan tool rows).

    A prior `summary` row counts as a renderable message for splitting
    purposes — re-compacting subsumes it.
    """
    # Walk from the end; collect rows until we've seen `keep_recent`
    # user/assistant rows. Then the boundary lands at the start of that
    # window — but slide it FORWARD past any leading tool rows so we don't
    # start a kept window with an orphan tool_result.
    keep_idx = len(active)
    user_assistant_seen = 0
    for i in range(len(active) - 1, -1, -1):
        if active[i].role in ("user", "assistant", "summary"):
            user_assistant_seen += 1
            if user_assistant_seen >= keep_recent:
                keep_idx = i
                break
    # Slide forward past any leading tool_* rows.
    while keep_idx < len(active) and active[keep_idx].role in (
        "tool_call", "tool_result",
    ):
        keep_idx += 1
    return active[:keep_idx], active[keep_idx:]
```

Imports added at the top of the file: `from app.generation import (
live_generations, _build_history_payload)`.

`_build_history_payload` is currently module-private to `app/generation.py`.
Either rename it to `build_history_payload` and re-export from
`app.generation`, or duplicate the function shape here. Recommendation:
**promote it to public** (drop the leading underscore in
`app/generation.py`, update its three internal call sites, update tests). The
shape is stable and is genuinely shared infrastructure now.

### Constants placement

`KEEP_RECENT = 4` lives at module scope in `app/routes/chats.py` next to the
endpoint (single use site for v1; a future per-chat / per-project knob is the
"out of scope" thread). Don't reach for `app_settings` until there are two
callers.

---

## Part E — Rendering

### E1. New block type

In `app/render.py`, add a third block alongside `MessageBlock` and
`ToolBatchBlock`:

```python
@dataclass(frozen=True)
class SummaryBlock:
    """The current active summary row, rendered as a special bubble.

    Distinct from MessageBlock so the template can give it its own styling
    (lighter background, a "summary" badge, a "▸ Show N archived
    messages" disclosure that opens an OOB-fetched panel).
    """

    message: Message
    archived_count: int  # rows currently archived; 0 hides the disclosure
    kind: ClassVar[str] = "summary"
```

Extend `group_messages_for_render`:

- Treat `summary` as a renderable role (flushes any pending tool batch
  ahead of itself).
- Emit a `SummaryBlock` instead of a `MessageBlock` for `summary` rows.
- The function gains an `archived_count` parameter so the SummaryBlock can
  carry it; alternatively, the caller passes the count and the template
  binds it from context. **Recommendation:** keep `group_messages_for_render`
  pure (messages → blocks) and let the template pull `archived_count` from
  the render context directly. Cleaner separation.

Updated literal sets at the top of `app/render.py`:

```python
_TOOL_ROLES = frozenset({"tool_call", "tool_result"})
_RENDERABLE_MESSAGE_ROLES = frozenset({"user", "assistant", "summary"})
```

In the loop, branch on `m.role == "summary"` to emit a `SummaryBlock(message=m)`
(without `archived_count` — bound in template).

### E2. New template: `_summary_bubble.html`

```html
{# A compacted-summary bubble: lighter-styled assistant-like bubble with a
   "summary" badge and a disclosure for the archived originals. The
   disclosure target is fetched on demand via hx-get so the chat panel
   doesn't pay to render archived rows on every panel mount. #}
<article class="message message--summary" id="summary-{{ message.id }}">
  <header class="message__badge">summary of earlier conversation</header>
  <div class="message__body">{{ message.content | markdown }}</div>
  {% if archived_count > 0 %}
  <details class="message__archived">
    <summary>{{ archived_count }} archived message{{ "s" if archived_count != 1 }}</summary>
    <div hx-get="/chats/{{ conversation.id }}/archived"
         hx-trigger="toggle once from:closest details"
         hx-swap="innerHTML">
      <small>Loading…</small>
    </div>
  </details>
  {% endif %}
</article>
```

### E3. New template: `_messages_inner.html`

Extract the `<div id="messages">…</div>` body from `_chat_panel.html` into its
own partial so the compact endpoint can render it standalone. The chat panel
becomes a one-line include. Same trick `_chat_tool_chips.html` uses
(phase 16).

`_chat_panel.html` lines 60-91 become:

```html
{% include "_messages_inner.html" %}
```

`_messages_inner.html` adds a branch for the new block type:

```html
{% elif block.kind == "summary" %}
  {% set message = block.message %}
  {% include "_summary_bubble.html" %}
{% elif block.kind == "tool_batch" %}
  …
{% else %}
  …
{% endif %}
```

### E4. Archived-rows viewer endpoint

```python
@router.get("/chats/{conversation_id}/archived", response_class=HTMLResponse)
async def archived_messages_endpoint(
    request: Request, conversation_id: int, db: DB,
) -> Response:
    """Render the archived (compacted-away) messages for inline disclosure."""
    try:
        queries.get_conversation(db, conversation_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    rows = [
        m for m in queries.list_messages(db, conversation_id)
        if m.archived_at is not None and m.role != "summary"
    ]
    # Render as plain MessageBlocks / ToolBatchBlocks but inside the
    # archived viewer (the template renders them with a "faded" wrapper
    # class).
    blocks = render.group_messages_for_render(rows)
    return templates.TemplateResponse(
        request=request, name="_archived_messages.html",
        context={"blocks": blocks},
    )
```

Archived `summary` rows are intentionally hidden — they're stale by
definition and only confuse the viewer.

### E5. Compact button in the chat panel

Add a button to the chat-panel header (`templates/_chat_panel.html` after
the tool-cap chip, around line 39):

```html
<button class="chat-panel__compact"
        hx-post="/chats/{{ conversation.id }}/compact"
        hx-target="#messages"
        hx-swap="outerHTML"
        hx-confirm="Compact this chat? The earlier conversation will be summarized; originals stay in the database."
        hx-indicator=".chat-panel__compact-spinner"
        title="Summarize the older portion of this chat to shrink the prompt.">
  <span class="chat-panel__compact-label">Compact</span>
  <span class="chat-panel__compact-spinner" aria-hidden="true">…</span>
</button>
```

The `hx-indicator` shows the spinner during the 1–5 s Ollama round-trip
(htmx's `htmx-request` class toggles `display: inline` on the spinner via
`.chat-panel__compact-spinner` rules in `static/style.css`). `hx-confirm`
uses the browser's native confirm dialog — same pattern the delete-chat
button already uses elsewhere in the app.

A small CSS rule in `static/style.css` (`.message--summary`,
`.chat-panel__compact`, `.chat-panel__compact-spinner`) gives the bubble its
distinct look and the button its disabled-while-loading affordance. Visual
spec deferred to the implementer — match the existing chip / button family.

---

## Part F — Tests

`tests/test_db.py`
- New: `archived_at` column is added by the migration on a pre-existing DB.
- New: the partial index is created.

`tests/test_queries_messages.py` (or wherever `list_messages` is covered)
- `list_active_messages` excludes archived rows; includes summary rows whose
  `archived_at IS NULL`.
- `archive_messages_before` archives the right window; is idempotent;
  returns the right rowcount; bumps `conversations.updated_at`.
- `count_archived_messages` counts only archived rows.
- `append_message(..., role="summary", ...)` round-trips correctly with the
  new role.
- `_row_to_message` populates `archived_at` (None / datetime cases).

`tests/test_ollama.py`
- `summarize_conversation` posts the right payload (model, messages,
  `stream=False`, options including the instruction turn appended); strips
  whitespace; raises `OllamaUnavailable` on transport failure;
  `OllamaProtocolError` on bad JSON / missing fields; returns empty string
  when the body has an empty content.

`tests/test_routes_compact.py` (new)
- 404 on unknown chat.
- 409 when a generation is in flight (`live_generations[id].done = False`).
- 422 when there are fewer than KEEP_RECENT + 2 active messages.
- Happy path: posts; the summary row exists; old rows are archived;
  KEEP_RECENT trailing messages are still active; the returned HTML is the
  rendered `_messages_inner.html` and contains the summary text.
- Re-compact path: a chat with an existing summary + new turns compacts
  again; the prior summary is now archived; exactly one active summary row
  remains.
- Ollama-down path: 502.
- `GET /chats/{id}/archived` returns archived rows (not the summary) as
  message blocks; 404 on unknown chat.

`tests/test_generation.py`
- `_build_history_payload` emits a `summary` row as a `system` message.
- Generation walks `list_active_messages`: a chat with archived rows + a
  summary row sends the summary + the active tail in the payload, omitting
  the archived rows.

`tests/test_render.py`
- `group_messages_for_render` emits a `SummaryBlock` for `summary` rows.
- A `summary` row mid-list flushes a pending tool batch ahead of itself.
- `_messages_inner.html` renders all three block kinds.

`tests/test_integration.py`
- End-to-end: start a chat, send several turns, compact (mocking Ollama's
  `/api/chat` to return a canned summary), send one more turn, assert that
  the second-turn's Ollama call sees the summary `system` message and not
  the archived turns.

Target: coverage stays at 97%+ on `app/` + `main.py`.

---

## Part G — Verification

1. `pytest --cov=app --cov=main --cov-report=term-missing` — green, no
   coverage regression.
2. **Browser smoke** (`uvicorn main:app --reload`):
   - Send 6+ turns in a chat against a small installed model.
   - Click **Compact**. Confirm the dialog; wait for the spinner; the chat
     re-renders with a single `summary` bubble at the top and the most-recent
     4 messages below.
   - Expand the disclosure → archived originals appear (faded styling).
   - Send a new message → it streams as normal; check the
     server-side Ollama payload (uvicorn DEBUG logs or a one-shot
     `httpx`-level capture) and confirm only the summary + recent turns
     went out, not the archived rows.
   - Compact again. The disclosure now shows N + prior turns archived; only
     one summary bubble visible.
   - Try to Compact during a generation → the button posts, server returns
     409, browser surfaces the error (htmx default behavior writes the body
     to the swap target — we may want a small `hx-on::response-error`
     handler that toasts instead; v1 can ship without the toast and tighten
     in a follow-up).
   - Reload mid-chat with a summary present → the summary bubble + disclosure
     re-render correctly from the persisted rows.
3. **Memory check** (the actual motivation): on the same M1, with a chat
   that previously pushed `prompt_tokens` near the configured `num_ctx`,
   verify that after a compact the next assistant turn reports a much
   smaller `prompt_eval_count` (visible via the per-turn token columns;
   surfaced in a small dev-tools probe or just `sqlite3 chats.db "SELECT
   prompt_tokens FROM messages ORDER BY id DESC LIMIT 3;"`).

---

## Critical files

| File | Change |
|---|---|
| `app/db.py` | `archived_at` column + migration + partial index |
| `app/queries/_models.py` | `Role` gains `"summary"`; `Message.archived_at` field |
| `app/queries/messages.py` | `list_active_messages`, `archive_messages_before`, `count_archived_messages`; SELECT columns updated |
| `app/ollama.py` | New `summarize_conversation` helper |
| `app/generation.py` | `_build_history_payload` handles `summary` role; call sites use `list_active_messages`; promote `_build_history_payload` to public |
| `app/routes/chats.py` | New `POST /chats/{id}/compact`, `GET /chats/{id}/archived`; switch history fetches to `list_active_messages` |
| `app/render.py` | New `SummaryBlock`; widened `_RENDERABLE_MESSAGE_ROLES`; emit summary block in `group_messages_for_render` |
| `templates/_chat_panel.html` | Compact button in header; extract messages body to `_messages_inner.html` |
| `templates/_messages_inner.html` | **New** — extracted body with `summary` block branch |
| `templates/_summary_bubble.html` | **New** — summary bubble + archived disclosure |
| `templates/_archived_messages.html` | **New** — viewer for archived rows |
| `static/style.css` | `.message--summary`, `.chat-panel__compact*`, spinner toggling |
| `tests/` | New `test_routes_compact.py`; additions to db / queries / ollama / generation / render / integration tests |
