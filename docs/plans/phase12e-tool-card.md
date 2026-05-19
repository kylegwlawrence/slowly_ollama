# Phase 12e — Tool-usage card above AI responses

## Context

Phase 12d shipped the server-side tool-calling loop but only emits
placeholder SSE events (`tool-call` / `tool-result`) carrying empty
`<div data-tool-call="{name}"></div>` divs — nothing renders to the
user. Phase 12e was originally specced in
`docs/plans/phase12-tool-calling-detail.md:1719-1814` as one
`<details>` card per individual tool call (separate cards for call
and result). This plan supersedes that design with a different
shape: **one aggregated card per assistant turn** that:

- Counts up live as tools fire (`using 1 tool…` → `using 2 tools…`)
- Switches to past tense once streaming completes (`used 2 tools`)
- Expands via a right-side chevron to a list of per-call rows
- Each row reads `searching <source>: "<query>"` for `query_rag`,
  or a generic `calling <tool>(<args>)` fallback for other tools
- Each row shows live elapsed time (`0:01`, `0:02`, …) that freezes
  at the final `mm:ss` when its `tool-result` event arrives
- Reappears for historic conversations too (grouped from persisted
  `tool_call` / `tool_result` rows)

The new design was driven by a clearer UX intent from the user — a
single per-turn surface is easier to scan than N independent
`<details>` cards, and a live counter conveys progress better than
N silent rows materializing one by one.

## UX summary

```
┌─ tool card (collapsed, default) ─────────────────────┐
│ ⚙  using 2 tools…                              ▼    │
└──────────────────────────────────────────────────────┘
        ↓ (live; "using" flips to "used" on done) ↓
┌─ tool card (expanded) ───────────────────────────────┐
│ ⚙  used 2 tools                                ▲    │
│   searching arxiv: "enhanced gas transfer…"  0:12   │
│   calling current_time(timezone='UTC')       0:00   │
└──────────────────────────────────────────────────────┘
        (sits ABOVE the assistant message bubble)
```

- Collapsed by default for both live and historic turns.
- Singular/plural: `using 1 tool…` / `using 2 tools…`.
- Microcopy switch from `using` → `used` happens via an OOB swap
  bundled into the `done` event payload (no JS state needed).

## SSE events: payload changes

`_stream_assistant_reply` in `app/routes.py:815-969` keeps the
existing event names (`tool-call`, `tool-result`, `done`); only
the payloads grow.

### Turn id

At the top of `_stream_assistant_reply`, generate a stable id for
this turn so card+row elements have unique DOM ids:

```python
turn_id = str(int(time.monotonic_ns()))  # short, unique within session
card_id = f"tool-card-{conversation_id}-{turn_id}"
list_id = f"{card_id}-list"
summary_id = f"{card_id}-summary-text"
```

### Summary-text formatting (single source of truth)

One helper produces the whole summary phrase (verb + count + noun +
ellipsis) so we never have to coordinate three separate OOB swaps
to keep verb / plural / ellipsis consistent:

```python
def _summary_text(count: int, done: bool) -> str:
    verb = "used" if done else "using"
    noun = "tool" if count == 1 else "tools"
    suffix = "" if done else "…"
    return f"{verb} {count} {noun}{suffix}"
```

The summary span is OOB-swapped as a whole on every state change.

### First tool-call event of the turn

Before persisting the first call, render the empty card and
OOB-swap it `beforebegin` the streaming placeholder. The card
carries the list shell + the summary span. Payload:

```html
<details id="{card_id}" class="tool-card"
         hx-swap-oob="beforebegin:#assistant-stream-{conversation_id}">
  <summary class="tool-card__summary">
    <span class="material-symbols-outlined">build</span>
    <span id="{summary_id}">using 1 tool…</span>
    <span class="tool-card__chevron material-symbols-outlined">expand_more</span>
  </summary>
  <ul id="{list_id}" class="tool-card__list">
    <li id="{row_id}" class="tool-row" data-elapsed-start="{epoch_ms}">…</li>
  </ul>
</details>
```

Emit this as the body of the `tool-call` SSE event (same event
name already wired). The first row is included so the card never
appears empty.

### Subsequent tool-call events

After the first, only emit the new row + the re-rendered summary
text, both as OOB-swap fragments in one event payload (HTMX walks
multiple OOB elements in a single response):

```html
<li id="{row_id}" class="tool-row"
    hx-swap-oob="beforeend:#{list_id}"
    data-elapsed-start="{epoch_ms}">…</li>
<span id="{summary_id}" hx-swap-oob="outerHTML">using {N} tools…</span>
```

Server tracks `call_index` to know "first vs subsequent" and to
build unique row ids (`row_id = f"{card_id}-row-{call_index}"`).

### tool-result event

OOB-replace the specific row so its timer freezes and the row's
`data-elapsed-final` is set (the JS tick driver stops touching
rows that have a final value):

```html
<li id="{row_id}" class="tool-row"
    hx-swap-oob="outerHTML"
    data-elapsed-final="{duration_ms}">…</li>
```

Duration computed in Python from the `tool_call` row's
`created_at` to the `tool_result` row's `created_at` (both are
`datetime.now(timezone.utc).isoformat()` with microsecond
precision — see `app/queries.py:81-83`, plenty for `mm:ss`).

### done event

Switch the summary phrase to past tense (`used N tool(s)`, no
ellipsis) AND freeze any unfrozen rows. The `done` event already
carries the final-message OOB swap (`routes.py:1039-1043`); append
the summary swap plus one OOB row-replace per still-in-flight row:

```html
<span id="{summary_id}" hx-swap-oob="outerHTML">used {N} tool{s?}</span>
{# zero or more frozen-row OOBs (one per row still missing a paired tool-result) #}
<li id="{row_id}" class="tool-row" hx-swap-oob="outerHTML"
    data-elapsed-final="{duration_ms}">…</li>
<!-- existing final message HTML unchanged -->
```

The freeze-unfinished-rows step matters for the
`_TOOL_ITERATION_CAP` bail branch (`app/routes.py:951-968`): when
the loop bails, the last `tool-call` never got a paired
`tool-result`, so that row's `data-elapsed-final` was never set.
Without the freeze, the JS tick driver would keep incrementing
that row's elapsed forever (after SSE close, no event ever resets
it). The bail branch must also emit the freeze OOBs alongside the
apology message and `done` event.

### Row rendering: `searching <db>: "<query>"` vs generic

A small helper in `app/tools/__init__.py` formats a
`(name, arguments)` pair into the row's human-readable label:

```python
def format_tool_invocation(name: str, arguments: dict) -> str:
    if name == "query_rag":
        source = arguments.get("source", "?")
        query  = arguments.get("query",  "")
        return f'searching {source}: "{query}"'
    args_str = ", ".join(f"{k}={v!r}" for k, v in arguments.items())
    return f"calling {name}({args_str})"
```

Tool-aware dispatch but small. Future tools that want a nicer
label add a branch here; nothing in `ToolSpec` itself changes.

## Templates

New:

- `templates/_tool_card_shell.html` — full card with embedded rows.
- `templates/_tool_row.html` — single row (live or frozen).

Modified:

- `templates/_assistant_placeholder.html:18` — extend `sse-swap`
  to include the two events:
  `sse-swap="token,done,error,title,tool-call,tool-result"`.
  HTMX's OOB swaps fire regardless of where the SSE event lands,
  but the event name must be listed for the listener to fire at
  all.
- `templates/_chat_panel.html:17-19` — replace the flat
  `_message.html` include with a branch that consumes blocks from
  the new grouping helper.

## History replay

Add `group_messages_for_render(messages)` in **new
`app/render.py`** (keeps render-time grouping out of the query
module — `queries.py` is for SQL only). It walks the message list
and produces a sequence of blocks:

```python
@dataclass
class MessageBlock: kind: Literal["message"]; message: Message
@dataclass
class ToolBatchBlock:
    kind: Literal["tool_batch"]
    calls: list[tuple[Message, Message | None]]  # (call, paired result)
    turn_id: str  # derived from first call's id, stable across reloads
```

Rule: consecutive `tool_call` + `tool_result` rows fold into a
`ToolBatchBlock`; each `tool_call` is paired with the next
`tool_result` (or `None` if the loop bailed). The next non-tool
row (`user` or `assistant`) flushes the batch.

Template loop becomes:

```jinja
{% for block in blocks %}
  {% if block.kind == "tool_batch" %}
    {% include "_tool_card_shell.html" %}  {# rendered closed, no live timer #}
  {% else %}
    {% include "_message.html" %}
  {% endif %}
{% endfor %}
```

**End-of-list flush.** If the message list ends with a tool batch
that has no following user/assistant row (rare — e.g., a crash
mid-turn), the helper must still flush the batch at end-of-loop
rather than dropping it.

For historic cards: every row has `data-elapsed-final` set (no
`data-elapsed-start` without a final), so the JS tick driver
skips them. Summary is rendered with
`_summary_text(count, done=True)` on the historic path. Unpaired
calls (loop bailed) get a `?` for elapsed.

The route that renders the chat panel (`GET /chats/{id}` and the
right-pane partial) passes `blocks` instead of (or alongside)
`messages` to the template.

## JS tick driver

**Inline in `_chat_panel.html`** per
`docs/CONVENTIONS.md:104-108` ("Inline JS is acceptable when HTMX
can't express it… don't graduate to a separate `.js` file until
there's a second use case"). Sits next to the existing inline
auto-scroll script. A `window.__toolTimerStarted` guard makes it
safe against the panel re-rendering on every chat switch
(otherwise we'd accumulate setIntervals each click):

```html
<script>
  (function () {
    if (window.__toolTimerStarted) return;
    window.__toolTimerStarted = true;
    function fmt(ms) {
      const s = Math.floor(ms / 1000);
      return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
    }
    setInterval(() => {
      const now = Date.now();
      document.querySelectorAll(
        '.tool-row[data-elapsed-start]:not([data-elapsed-final])'
      ).forEach(row => {
        const elapsed = now - Number(row.dataset.elapsedStart);
        const el = row.querySelector('.tool-row__elapsed');
        if (el) el.textContent = fmt(elapsed);
      });
    }, 1000);
  })();
</script>
```

When a `tool-result` event lands (or the bail-branch freeze
fires), the row is OOB-replaced with one that has
`data-elapsed-final` set and the elapsed text frozen — the driver
no longer matches it on the next tick.

`data-elapsed-start` is server-stamped as
`int(time.time() * 1000)` at the moment the call is persisted, so
the live counter is anchored to the server's clock rather than
the client receiving the event (cleaner if the SSE event takes a
beat to arrive — local app, so clock skew is zero).

## CSS

In `static/style.css`, near the existing `.message` block
(`style.css:436+`):

```css
.tool-card {
  align-self: stretch;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  margin: 0.25rem 0;
  font-size: 0.9em;
}
.tool-card__summary {
  display: flex; align-items: center; gap: 0.5rem;
  padding: 0.5rem 0.75rem; cursor: pointer;
  list-style: none; /* hide native marker */
}
.tool-card__summary::-webkit-details-marker { display: none; }
.tool-card__chevron { margin-left: auto; transition: transform 0.15s; }
.tool-card[open] .tool-card__chevron { transform: rotate(180deg); }
.tool-card__list {
  list-style: none; margin: 0;
  padding: 0 0.75rem 0.5rem 2.25rem; /* indent under the build icon */
}
.tool-row {
  display: flex; gap: 0.5rem;
  font-family: ui-monospace, SFMono-Regular, monospace;
  font-size: 0.85em; color: var(--text-secondary);
  padding: 0.15rem 0;
}
.tool-row__label { flex: 1; }
.tool-row__elapsed { font-variant-numeric: tabular-nums; }
```

Reuses the same tokens (`--surface`, `--border`,
`--text-secondary`) used by message bubbles and the RAG server
rows — no new tokens.

## Files to modify

| File | Change |
|---|---|
| `app/routes.py:815-969` | Turn-id generation; new payloads for `tool-call`/`tool-result`/`done`; render via Jinja, not f-strings |
| `app/tools/__init__.py` | `format_tool_invocation(name, arguments)` helper |
| `app/render.py` *(new)* | `group_messages_for_render(messages)` + the `MessageBlock` / `ToolBatchBlock` dataclasses |
| `app/routes.py` (panel route) | Pass `blocks` to `_chat_panel.html` |
| `templates/_tool_card_shell.html` *(new)* | Full card + embedded rows |
| `templates/_tool_row.html` *(new)* | Single row, used for both first row inline and subsequent OOB appends |
| `templates/_assistant_placeholder.html:18` | Add `tool-call,tool-result` to `sse-swap` |
| `templates/_chat_panel.html:17-19` | Branch on `block.kind` |
| `templates/_chat_panel.html` (script block) | Add inline tick driver next to the existing auto-scroll script (single-init guard) |
| `static/style.css` | `.tool-card`, `.tool-card__*`, `.tool-row*` rules |
| `docs/CONVENTIONS.md:248-250` | Rewrite the "tool calls and results persist as their own rows" bullet to reflect the aggregated card design (was: per-call `<details>`; now: one card per turn, list of rows) |

## Tests

In `tests/`, mirroring the existing per-module layout
(`tests/README.md`):

- `tests/test_routes_tool_card.py`
  - Single-tool turn: SSE stream contains a `tool-card-…`
    `<details>` with one row and
    `id="…-summary-text">using 1 tool…`. After `done`, OOB
    fragment swaps the summary to `used 1 tool` (no ellipsis).
  - Two-tool turn: second `tool-call` event payload contains both
    an OOB `beforeend:#…-list` row AND an outerHTML summary
    update to `using 2 tools…`. Pluralization (`tool` → `tools`)
    verified.
  - `tool-result` event: row OOB-replaces with
    `data-elapsed-final` set to the call-to-result duration in
    ms.
  - Generic fallback: a tool other than `query_rag` produces a
    row with `calling current_time(…)` text.
  - Iteration-cap bail (`_TOOL_ITERATION_CAP`): apology message
    fires; bail-branch payload includes a `data-elapsed-final`
    OOB swap for the unpaired final row so the timer freezes.
- `tests/test_render_blocks.py`
  - `group_messages_for_render` folds consecutive `tool_call` +
    `tool_result` rows into one `ToolBatchBlock` ahead of the
    following assistant message.
  - Unpaired final `tool_call` (cap bail) → batch with
    `(call, None)`.
  - End-of-list batch still flushes (no trailing user/assistant
    row).
  - No tool calls → all `MessageBlock`s.
- `tests/test_templates_tool_card.py`
  - `_tool_card_shell.html` rendered with a historic batch: verb
    is `used`, all rows have `data-elapsed-final`, no
    `data-elapsed-start` without a final.
  - Live rendering vs historic rendering pin to the same template
    via a `mode` flag (or `is_live` context var) — test both.

Run with: `pytest --cov=app --cov=main --cov-report=term-missing`.
Coverage target: existing ceiling (99% on `app/` + `main.py`).

## Verification

1. `source .venv/bin/activate && pytest` — all green.
2. `uvicorn main:app --reload`, open `http://localhost:8000`.
3. Send a message that triggers `query_rag` (any of the RAG
   servers in `/settings`). Observe:
   - Card appears above the streaming bubble within ~1s.
   - Count says `using 1 tool…`.
   - Row reads `searching <source>: "<query>"` with a ticking
     timer.
   - On final answer: card flips to `used 1 tool`; row timer
     freezes.
4. Trigger a multi-tool turn (ask the model something that
   requires `current_time` + a RAG search). Verify count rises
   to `2`, generic fallback row shows for `current_time`, both
   timers freeze independently.
5. Reload the page. Verify the historic conversation re-renders
   the same card above the past assistant message, collapsed,
   with final `mm:ss` durations.
6. Click chevron to expand/collapse; click chevron multiple
   times — no JS errors in devtools console.
7. Toggle dark mode — card and rows use `--surface` / `--border`
   tokens and read correctly in both.
8. Stop Ollama mid-tool-call: SSE `error` event fires; card stays
   in whatever state it reached without crashing the page.

## Notes / open follow-ups

- **Phase 12e doc cleanup**: after this lands, update
  `docs/plans/phase12-tool-calling-detail.md:1719-1814` (the
  per-tool `<details>` design) to point at this spec instead.
  Flag for the user.
- **`tool-result` event payload no longer carries the result
  body** (queries-only, per the user's choice). The DB row still
  holds the full result text — accessible if a debugging panel
  ever wants it.
- **Tests for the tick driver**: not unit-tested (vanilla JS in
  the browser); covered by the smoke test in §Verification.
