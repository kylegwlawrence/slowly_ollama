# Phase 12h — Tool-row source list (expandable)

## Context

The aggregated tool-usage card that sits above each assistant response (`templates/_tool_card_shell.html`, shipped in phase 12e) shows one row per tool call with a label + elapsed time, but says nothing about *what the model retrieved*. Users want to see the sources behind a `query_rag` call without having to read the model's prose.

This phase adds an expandable per-row sources list: clicking a RAG tool row reveals the deduplicated titles (with section when single-chunk, chunk count when multi-chunk). Non-RAG tools (`current_time`) render as today.

The current RAG contract returns `title`, `section`, `text` per chunk plus a handful of debug fields; only the formatted citation block currently survives into storage. This phase routes the structured `title + section` data through the persistence and render paths so the UI can use it.

### Codebase state at planning time
- Phase 12e ✓ (aggregated tool card), 12e.1 ✓ (cancellation safety net), 12f ✓ (model-capability filter on tools), 12g ✓ (resumable generation via background task — `app/generation.py` is now the producer module).
- Most recent commit `2464527` (design-system polish — radius/space tokens). Working tree clean.
- The generation loop lives in `app/generation.py:_run_generation` (was `_stream_assistant_reply` in `routes.py` pre-12g).
- Phase 12f gates tool advertisement on `ollama.model_supports_tools(...)` (`app/generation.py:450-455`) — if a conversation's pinned model doesn't support tools, no tool calls fire at all and the sources surface is moot for that turn (still correct behavior; nothing to do).

## Locked design decisions (already agreed with user)

- **Placement**: each tool row becomes a `<details>` *only* when sources are present; non-source tools stay as plain `<li>` rows.
- **Source line format**:
  - count == 1 with section: `Title (§Section)`
  - count == 1 no section: `Title`
  - count > 1: `Title (N chunks)` — section is dropped on purpose (chunks may span sections)
- **Dedup by title only** — first-seen order preserved.
- **No chapter field** anywhere. Don't read it, don't tolerate it, don't extend the contract.
- **No snippet preview, no clickable links** — text only.
- **Phase name**: 12h. New plan + retro under `docs/plans/` and `docs/retros/`.
- **No SQL migration** — store structured data inside `tool_result.content` as a JSON envelope.

## Implementation

### 1 — New types in `app/tools/__init__.py`

Add `field` to the dataclasses import. Define:

```python
from dataclasses import dataclass, field
import json

@dataclass(frozen=True)
class Source:
    """One retrieved chunk's UI-facing metadata.

    Attributes:
        title: Document title (the chunk's `title` field). Always
            present; the caller normalizes a missing value to
            "(untitled)" before constructing.
        section: Optional section heading (`section` field). None when
            absent — the UI omits the "(§Section)" suffix in that case.
    """
    title: str
    section: str | None


@dataclass(frozen=True)
class ToolResult:
    """Structured return value for tools that surface sources to the UI.

    The model sees only `.text`. Sources are a UI-only concern.
    Plain-string returns from tools are wrapped by `run_tool` so the
    rest of the system handles a single shape.

    Attributes:
        text: What the model sees as the tool's output.
        sources: Zero-or-more entries used to render the expandable
            sub-list. Empty for non-source tools.
    """
    text: str
    sources: list[Source] = field(default_factory=list)


def encode_tool_result(result: ToolResult) -> str:
    """Serialize a ToolResult for storage in `messages.content`."""
    return json.dumps({
        "text": result.text,
        "sources": [
            {"title": s.title, "section": s.section} for s in result.sources
        ],
    })


def decode_tool_result(content: str) -> ToolResult:
    """Inverse of `encode_tool_result`, with plain-text fallback.

    Pre-12h DB rows store plain text (the formatted citation block).
    Any non-JSON content, or JSON without the envelope keys, decodes
    to `ToolResult(text=content, sources=[])` so old conversations
    still render.
    """
    try:
        payload = json.loads(content)
        if not isinstance(payload, dict) or "text" not in payload:
            return ToolResult(text=content, sources=[])
        raw = payload.get("sources") or []
        sources = [
            Source(title=s.get("title", "(untitled)"), section=s.get("section"))
            for s in raw if isinstance(s, dict)
        ]
        return ToolResult(text=payload["text"], sources=sources)
    except (json.JSONDecodeError, TypeError):
        return ToolResult(text=content, sources=[])
```

Update `run_tool` to always return `ToolResult` (wraps strings, wraps errors):

```python
async def run_tool(name: str, args: dict) -> ToolResult:
    spec = TOOLS.get(name)
    if spec is None:
        return ToolResult(text=f"Tool '{name}' is not registered.")
    try:
        result = spec.func(**args)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, ToolResult):
            return result
        return ToolResult(text=str(result))
    except TypeError as e:
        return ToolResult(text=f"Tool '{name}' rejected arguments: {e}")
    except Exception as e:
        return ToolResult(text=f"Tool '{name}' failed: {e}")
```

### 2 — `app/tools/rag.py`

`_format_chunks` is unchanged. Change `query_rag`'s success path to return a `ToolResult`. Every error-path return becomes `ToolResult(text="...")`. Build sources from the same `items` that feed `_format_chunks`:

```python
from app.tools import Source, ToolResult, tool  # add ToolResult + Source

@tool
async def query_rag(source: str, query: str) -> ToolResult:
    """Retrieve passages from a configured RAG source.

    Args:
        source: Name of the configured RAG server to query.
        query: Natural-language query string.
    """
    if not query.strip():
        return ToolResult(text="Tool query_rag: 'query' cannot be empty.")
    # ... existing source lookup + http call + error branches:
    # every existing `return "..."` becomes `return ToolResult(text="...")`.

    sources = [
        Source(
            title=item.get("title") or "(untitled)",
            section=item.get("section"),
        )
        for item in items
    ]
    return ToolResult(text=_format_chunks(items, used_dense), sources=sources)
```

### 3 — Persistence in `app/generation.py`

Three pin-pointed edits in the post-12g producer:

1. **Module-level imports** — add `encode_tool_result, decode_tool_result` to the existing `from app.tools import ...` line at `app/generation.py:41`:

   ```python
   from app.tools import (
       decode_tool_result,
       encode_tool_result,
       format_tool_invocation,
       run_tool,
       tool_specs_for_ollama,
   )
   ```

2. **Tool-result persistence in `_run_generation`** — around lines 560–567. `run_tool` now returns a `ToolResult`; persist the JSON envelope:

   ```python
   result = await run_tool(name, arguments)  # ToolResult

   queries.append_message(
       db,
       conversation_id,
       "tool_result",
       content=encode_tool_result(result),
   )
   ```

3. **`_build_history_payload`** — the `tool_result` branch at lines 298–299 currently writes `m.content` verbatim. Decode the envelope so Ollama sees plain text:

   ```python
   elif m.role == "tool_result":
       out.append({"role": "tool", "content": decode_tool_result(m.content).text})
   ```

4. **Frozen row at the `tool-result` emission site** — lines 571–577 construct `frozen_row` without sources. Add `sources=result.sources`:

   ```python
   frozen_row = render.ToolRowView(
       id=row_id,
       label=label,
       elapsed_start_ms=None,
       elapsed_final_ms=duration_ms,
       elapsed_display=render.format_elapsed_mm_ss(duration_ms),
       sources=result.sources,
   )
   ```

   The live (pending) row at lines 514–520 stays at `sources=[]` (omit — defaults to `[]`). The bail-branch frozen rows in `_build_done_card_oobs` (lines 345–351) also stay at `sources=[]` — calls that never resolved have no sources to show.

### 4 — `app/render.py` — historic render path

Add the sources field to `ToolRowView`:

```python
from app.tools import Source, decode_tool_result

@dataclass(frozen=True)
class ToolRowView:
    id: str
    label: str
    elapsed_start_ms: int | None
    elapsed_final_ms: int | None
    elapsed_display: str
    sources: list[Source] = field(default_factory=list)

    @property
    def deduped_sources(self) -> list["DedupedSource"]:
        """Template-facing collapsed view of `sources`."""
        return dedup_sources(self.sources)
```

Update `_row_view_from_pair`:

```python
def _row_view_from_pair(call, result, row_id):
    # ... existing JSON-parse + label build unchanged ...
    if result is None:
        return ToolRowView(
            id=row_id, label=label,
            elapsed_start_ms=None, elapsed_final_ms=None,
            elapsed_display="?", sources=[],
        )
    decoded = decode_tool_result(result.content)
    duration_ms = int(
        (result.created_at - call.created_at).total_seconds() * 1000
    )
    return ToolRowView(
        id=row_id, label=label,
        elapsed_start_ms=None, elapsed_final_ms=duration_ms,
        elapsed_display=format_elapsed_mm_ss(duration_ms),
        sources=decoded.sources,
    )
```

Add `DedupedSource` + `dedup_sources` in the same module:

```python
@dataclass(frozen=True)
class DedupedSource:
    """One source entry as the template renders it.

    Attributes:
        title: Document title, unmodified.
        meta: Parenthesized suffix:
            - "(§Section)" when count == 1 with section
            - "(N chunks)" when count > 1
            - "" otherwise
    """
    title: str
    meta: str


def dedup_sources(sources: list[Source]) -> list[DedupedSource]:
    """Collapse sources by title in first-seen order."""
    groups: dict[str, list[Source]] = {}
    order: list[str] = []
    for s in sources:
        if s.title not in groups:
            groups[s.title] = []
            order.append(s.title)
        groups[s.title].append(s)
    out: list[DedupedSource] = []
    for title in order:
        items = groups[title]
        if len(items) > 1:
            meta = f"({len(items)} chunks)"
        elif items[0].section:
            meta = f"(§{items[0].section})"
        else:
            meta = ""
        out.append(DedupedSource(title=title, meta=meta))
    return out
```

### 5 — `templates/_tool_row.html` — two-branch render

Keep the outer `<li>` as the swap unit (preserves `data-elapsed-*`, `hx-swap-oob`, JS tick selector). Wrap contents in `<details>` only when sources exist:

```jinja
{# Tool-card row. Renders as <details> when row.deduped_sources is non-empty;
   otherwise as the original plain <li>. The outer <li> stays the swap unit
   (same id, data-elapsed-*, hx-swap-oob) so SSE OOB replace and the JS tick
   driver are unaffected.

   See _tool_card_shell.html for the parent card. #}
{% if row.deduped_sources %}
<li id="{{ row.id }}" class="tool-row tool-row--expandable"
    {%- if row.elapsed_start_ms is not none %} data-elapsed-start="{{ row.elapsed_start_ms }}"{% endif %}
    {%- if row.elapsed_final_ms is not none %} data-elapsed-final="{{ row.elapsed_final_ms }}"{% endif %}
    {%- if swap_oob %} hx-swap-oob="{{ swap_oob }}"{% endif %}>
  <details class="tool-row__details">
    <summary class="tool-row__summary">
      <span class="tool-row__label">{{ row.label }}</span>
      <span class="tool-row__elapsed">{{ row.elapsed_display }}</span>
      <span class="tool-row__chevron material-symbols-outlined">expand_more</span>
    </summary>
    <ul class="tool-row__sources">
      {% for src in row.deduped_sources %}
      <li class="tool-row__source">
        <span class="tool-row__source-title">{{ src.title }}</span>
        {%- if src.meta %} <span class="tool-row__source-meta">{{ src.meta }}</span>{% endif %}
      </li>
      {% endfor %}
    </ul>
  </details>
</li>
{% else %}
<li id="{{ row.id }}" class="tool-row"
    {%- if row.elapsed_start_ms is not none %} data-elapsed-start="{{ row.elapsed_start_ms }}"{% endif %}
    {%- if row.elapsed_final_ms is not none %} data-elapsed-final="{{ row.elapsed_final_ms }}"{% endif %}
    {%- if swap_oob %} hx-swap-oob="{{ swap_oob }}"{% endif %}>
  <span class="tool-row__label">{{ row.label }}</span>
  <span class="tool-row__elapsed">{{ row.elapsed_display }}</span>
</li>
{% endif %}
```

`_tool_card_shell.html` is unchanged.

### 6 — CSS append to `static/style.css`

Place immediately after the existing `.tool-row__elapsed` block (after the current line ~660):

```css
/* ===== Tool-row expandable sources (phase 12h) =========================
   When a tool row carries retrieved sources (currently only query_rag),
   the row contents are wrapped in <details>. Chevron sits to the right
   of the elapsed value; sources list lives indented underneath. Visual
   language matches the parent .tool-card: same chevron rotation, same
   monospace + secondary-text tone. */

.tool-row--expandable {
  display: block;   /* override base flex so <details> stacks summary + list */
  padding: 0;
}

.tool-row__details { width: 100%; }

.tool-row__summary {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  cursor: pointer;
  list-style: none;
  padding: 0.15rem 0;
}
.tool-row__summary::-webkit-details-marker { display: none; }

.tool-row__summary .tool-row__label { flex: 1; }
.tool-row__summary .tool-row__elapsed { font-variant-numeric: tabular-nums; }

.tool-row__chevron {
  font-size: 1em;
  color: var(--text-secondary);
  transition: transform 0.15s;
}
.tool-row__details[open] .tool-row__chevron { transform: rotate(180deg); }

.tool-row__sources {
  list-style: none;
  margin: 0;
  padding: var(--space-xs) 0 var(--space-xs) var(--space-md);
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.tool-row__source {
  font-family: ui-monospace, SFMono-Regular, monospace;
  font-size: 0.85em;
  color: var(--text-secondary);
}
.tool-row__source-title { color: var(--text-primary); }
.tool-row__source-meta { color: var(--text-secondary); margin-left: var(--space-xs); }
```

Design notes:
- `display: block` on the expandable variant overrides the base `.tool-row { display: flex }` so the `<details>` can stack the summary line + sources list vertically.
- Padding moves from the `<li>` to `.tool-row__summary` so the chevron-bearing line keeps its `0.15rem 0` rhythm.
- Indent uses `--space-md` (14px) — half the card's outer indent — to imply "sub-list of a sub-list".
- Title in `--text-primary`, meta in `--text-secondary` — same legibility hierarchy as the chat-item title/URL pair. Dark mode flips automatically via token swap.

### 7 — SSE flow (no code changes beyond §3)

1. **`tool-call` event** (live, ticking) — emitted at `app/generation.py:550` via `_emit(state, "tool-call", payload)`. The pending row renders with `sources=[]`, so the template picks the plain-`<li>` branch. No chevron. JS tick driver finds `.tool-row__elapsed` on the `<li>` directly.
2. **`tool-result` event** — emitted at `app/generation.py:582`. The frozen row renders with `result.sources`. If non-empty, expandable branch; otherwise plain. `hx-swap-oob="outerHTML"` on the outer `<li>` (matched by id) replaces the pending row in place. The nested `<details>` rides along as inner content.
3. **JS tick driver** (inline `<script>` in `templates/_chat_panel.html`) — selector `.tool-row[data-elapsed-start]:not([data-elapsed-final])` matches the outer `<li>` regardless of the `--expandable` modifier. `querySelector('.tool-row__elapsed')` works for both branches because the elapsed span is present in both.
4. **Bail-branch frozen-in-flight rows** (`_build_done_card_oobs` at `app/generation.py:305-355`) — `sources=[]`, plain row. No change.
5. **Late-consumer replay** — `consume_generation` (`app/generation.py:123`) replays the event log from index 0 for reloads / second tabs. Because the SSE payloads are HTML strings already produced by the templates, reloading a partially-finished generation shows whatever rows had landed at that point with the correct branch (plain or expandable). No additional work.

### 8 — Files to modify

| File | Change |
|---|---|
| `app/tools/__init__.py` | Add `Source`, `ToolResult`, `encode_tool_result`, `decode_tool_result`; change `run_tool` return type to `ToolResult`. Also add `field` to the dataclasses import. |
| `app/tools/rag.py` | `query_rag` returns `ToolResult`; every error-path `return "..."` wraps in `ToolResult(text=...)`. Add `Source, ToolResult` to imports. |
| `app/generation.py` | Lines 41, 298–299, 560–567, 571–577 — see §3. Persist `encode_tool_result(result)`; decode in `_build_history_payload`; pass `sources=result.sources` to frozen row. |
| `app/render.py` | Add `sources` field + `deduped_sources` property on `ToolRowView`; add `DedupedSource` + `dedup_sources`; update `_row_view_from_pair` to decode and pass sources. Add `Source, decode_tool_result` to imports. |
| `templates/_tool_row.html` | Two-branch template: plain `<li>` when no sources, expandable `<details>` form when sources present. |
| `static/style.css` | Append the `.tool-row--expandable` / `.tool-row__details` / `.tool-row__chevron` / `.tool-row__sources` block — insertion point is immediately after line 660 (current end of `.tool-row__elapsed { … }` block), before the regenerate-button section. |
| `docs/plans/phase12h-tool-sources.md` *(new)* | Materialize this plan (per repo convention). |
| `docs/retros/phase12h-tool-sources.md` *(new, stub)* | Empty retro placeholder to fill after implementation. |
| `docs/CONVENTIONS.md` | Append one bullet under tool-calling: `tool_result.content` is now a JSON envelope produced by `encode_tool_result` (plain-text fallback for legacy rows). |
| `CLAUDE.md` | Update phase 12 status line to mention 12h. |

No SQL schema change. No new packages. No change to `templates/_tool_card_shell.html`.

### 9 — Test plan

Reused convention: assertions pin contracts (data-* / hx-* / class names / substrings), not DOM tree shape.

**`tests/test_tools.py`** — additions:
- `test_run_tool_wraps_string_returns_in_tool_result`
- `test_run_tool_passes_through_tool_result_returns`
- `test_run_tool_errors_return_tool_result_with_empty_sources` (unknown tool, TypeError, generic Exception)
- `test_encode_decode_tool_result_round_trip` (empty + populated sources)
- `test_decode_tool_result_plain_text_backwards_compat`
- `test_decode_tool_result_malformed_json_falls_back`
- `test_decode_tool_result_json_without_envelope_keys_falls_back`
- Update existing `test_query_rag_*_success` to assert `ToolResult.text` and `ToolResult.sources` shape.
- `test_query_rag_error_paths_return_tool_result` (empty query, unknown source, 503, unreachable)

**`tests/test_render.py`** — additions:
- `test_historic_row_view_extracts_sources_from_json_content`
- `test_historic_row_view_plain_text_content_has_empty_sources`
- `test_dedup_sources_single_chunk_with_section` → `(§S)`
- `test_dedup_sources_single_chunk_no_section` → `""`
- `test_dedup_sources_multi_chunk_same_title_drops_section` → `(2 chunks)`
- `test_dedup_sources_multi_chunk_different_titles_first_seen_order`
- `test_dedup_sources_empty_input`
- `test_tool_row_view_deduped_sources_property_round_trip`

**`tests/test_templates_tool_row.py`** *(new)*:
- `test_tool_row_renders_plain_li_without_sources` (no `tool-row--expandable`, no `<details>`)
- `test_tool_row_renders_details_when_sources_present` (has `<details>`, chevron, source list)
- `test_tool_row_single_source_with_section_shows_paragraph_section_marker` (`(§Section)`)
- `test_tool_row_single_source_no_section_shows_title_only`
- `test_tool_row_multi_chunk_shows_chunk_count_drops_section`
- `test_tool_row_preserves_elapsed_attrs_in_expandable_form` (outer `<li>` still carries `data-elapsed-final` etc.)
- `test_tool_row_swap_oob_lives_on_outer_li_in_expandable_form` (HTMX swap unit contract)

**`tests/test_generation.py`** — additions:
- `test_tool_result_persisted_as_json_envelope_when_sources_present`
- `test_tool_result_persisted_as_json_envelope_for_text_only` (always JSON going forward simplifies the decode path)
- `test_build_history_payload_extracts_text_from_json_tool_result`
- `test_build_history_payload_backwards_compat_plain_text_tool_result`
- `test_frozen_row_after_tool_result_carries_sources_in_oob_payload`

**`tests/test_routes.py`** — addition:
- `test_historic_chat_panel_renders_expandable_row_for_persisted_rag_call` — seed `tool_call` + JSON-envelope `tool_result` rows, GET `/chats/{id}`, assert response HTML contains `tool-row--expandable` and the source title substring.

Expected delta: ~20 new tests, one existing RAG success test updated. Coverage should stay at the 99% ceiling.

### 10 — Verification (manual end-to-end)

Pre-flight:
1. `source .venv/bin/activate && pytest` — confirm baseline green before edits.

After implementation:
2. `pytest` — all green.
3. `pytest --cov=app --cov=main --cov-report=term-missing` — coverage stays at 99% ceiling.

Browser smoke (RAG path):
4. Spin up a mock RAG server (paste in a terminal):
   ```bash
   python3 -c "
   from http.server import BaseHTTPRequestHandler, HTTPServer
   import json
   class H(BaseHTTPRequestHandler):
     def do_GET(s):
       s.send_response(200); s.send_header('Content-Type', 'application/json'); s.end_headers()
       s.wfile.write(json.dumps({'items': [
         {'title': 'Doc A', 'section': '1.2', 'text': 'first hit'},
         {'title': 'Doc A', 'section': '1.5', 'text': 'second hit same doc'},
         {'title': 'Doc B', 'section': None, 'text': 'no section'},
       ], 'used_dense': True}).encode())
   HTTPServer(('localhost', 8765), H).serve_forever()
   "
   ```
5. Add `http://localhost:8765` as a RAG server named `mocked` via `/settings`.
6. `uvicorn main:app --reload`; open `http://localhost:8000`.
7. Send a message: "Use the mocked source to search for X."
8. **Live**: row reads `searching mocked: "X"` with live tick. After result arrives, row freezes with `m:ss` AND grows a chevron on the right.
9. **Click the chevron** — row expands:
   - `Doc A (2 chunks)`
   - `Doc B`
10. Reduce mock to one item with a section, repeat — single line should read `Doc A (§1.2)`.
11. **Toggle parent card chevron** — independent of the row chevron, both still work.

Browser smoke (non-source path):
12. Ask "what time is it?" — row shows the usual `calling current_time(timezone='UTC')`. **No chevron, no expansion**.

Reload (historic render):
13. Reload the page. Tool card collapsed. Expand it, expand the source row. Sources match the live render.

Dark mode:
14. Toggle dark mode. Title in primary text, meta + chevron in secondary. All readable.

Backwards compatibility:
15. Open a pre-12h conversation (`tool_result.content` plain text). Renders as plain row, no chevron. No console errors.

## Critical files for implementation

- `/Users/user/Documents/PROJECTS/slowly_ollama/app/tools/__init__.py`
- `/Users/user/Documents/PROJECTS/slowly_ollama/app/tools/rag.py`
- `/Users/user/Documents/PROJECTS/slowly_ollama/app/generation.py`
- `/Users/user/Documents/PROJECTS/slowly_ollama/app/render.py`
- `/Users/user/Documents/PROJECTS/slowly_ollama/templates/_tool_row.html`
- `/Users/user/Documents/PROJECTS/slowly_ollama/static/style.css`

## Out of scope (for clarity)

- Chapter / page / author / URL fields — explicit user decision to defer.
- Snippet / preview text in source lines.
- Clickable source links.
- Tool-result content viewer (the raw model-facing text is in the envelope's `text` field, accessible to a future feature without further schema work).
- Schema changes to the RAG-server contract.
- A new `metadata` column on `messages` — explicitly avoided in favor of in-content JSON.
