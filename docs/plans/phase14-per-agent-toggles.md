# Phase 14 — Per-agent toggles for the agentic loop

Executable implementation plan. Style matches
`phase13-agentic-loop.md`: exact file paths, code snippets verbatim
where it matters, test specs included.

If this file disagrees with anything earlier in `docs/plans/`, **this
file wins** — it's the most recent and reflects the decision pass
that produced it.

---

## Why this exists

Phase 13 shipped a single global toggle (`agentic_mode = on|off`)
that flips the producer between the single-agent path
(`_run_generation`) and the three-agent loop
(`_run_agentic_generation`). The loop's composition is hardcoded —
research → review → generation, no skipping.

Real-use feedback: the user wants to experiment with subsets of the
loop to feel out which agents add value for which questions. Phase
14 makes the composition user-controllable via two new toggles —
**Reviewer** and **Generator** — that nest under the existing
master toggle.

The composition rules came from the user verbatim:

- If research is off, **all** agents are off.
- If reviewer is off, research **comes to its own conclusions**.
- If generator is off, research **generates their own response**.

Implementation note on the first rule: the existing `agentic_mode`
master toggle already acts as "research off = mode off", because
research is the only producer of findings. So Phase 14 adds **two**
new toggles, not three. The master toggle keeps its role as the
research toggle (renamed in the UI, unchanged in storage).

---

## Locked decisions

Confirmed via the planning conversation that produced this file.

- **UI structure:** master toggle (existing `agentic_mode`) +
  Reviewer and Generator as nested sub-toggles. Sub-toggles
  rendered only when master is on; hidden entirely otherwise.
- **Storage:** two new rows in `app_settings` —
  `review_enabled` and `generator_enabled` — alongside the
  existing `agentic_mode` row. Same `"on"` / `"off"` string
  encoding. Both default to `"on"` when absent (so existing chats
  with master on keep current Phase 13 behavior).
- **Scope:** **global runtime**, not per-chat-snapshotted. The
  three settings are read at turn-start time in `start_generation`
  and the active configuration applies to the whole turn. No new
  columns on `conversations`. Matches the existing `agentic_mode`
  scope.
- **Reviewer-off semantics:** research runs a **single pass** (one
  iteration, up to 5 tool calls). No iteration loop, no verdict
  event, no max-iterations badge — those concepts are meaningless
  without a reviewer to drive them.
- **Generator-off semantics:** research's final findings text is
  used **verbatim** as the user-facing assistant message. No
  separate generation Ollama call. No prompt swap — the existing
  research prompt is used as-is.
- **Prompts:** unchanged. No new `RESEARCH_AS_ANSWER_SYSTEM_PROMPT`
  variant in v1. Known quality tradeoff: when generator is off,
  the assistant message will read in the "findings" voice (lists
  facts, notes gaps, "NOT attempt to answer the user directly"
  per the existing prompt). User accepts this for v1; iterate on
  the prompt as follow-up if quality bites.
- **Streaming when generator is off:** research's text output
  comes back as a complete string from `maybe_tool_call`, not
  streamed. The whole findings text is emitted as a single
  `token` SSE event, then the standard `done` event swaps in the
  markdown-rendered bubble. UX delta vs. generator-on (no
  progressive reveal of the answer text) is acceptable.
- **Tool card layout:** unchanged structurally. Iteration headers
  still emit at index=1 even when reviewer is off, so DOM ids
  (`{card_id}-iter-1-row-N`) line up with the historic-render
  template. When reviewer is off, no verdict row, no max-iter
  badge — the absence is the visual signal.
- **Dispatcher logic:** unchanged. `start_generation` still routes
  to `_run_agentic_generation` based on master toggle + model
  `tools` capability. The two new sub-toggles are passed as
  kwargs to the agentic producer and consumed there. The
  single-agent path (`_run_generation`) is **untouched**.
- **Master toggle UI label:** keep as "Enable agentic mode" (not
  renamed to "Research agent"). Users already know the term;
  the sub-toggle labels disambiguate.

---

## Configuration matrix

| `agentic_mode` | `review_enabled` | `generator_enabled` | Behavior |
|---|---|---|---|
| off | * | * | Single-agent path (`_run_generation`) — unchanged |
| on | on | on | Current Phase 13 full loop |
| on | on | off | Research↔Review loop; last findings → assistant bubble |
| on | off | on | Research one-shot → generator synthesizes |
| on | off | off | Research one-shot → findings → assistant bubble |

The two new "sub-config" rows expand the agentic-on landscape from
one shape to four. Master-off (single-agent) is unchanged.

---

## Architecture delta vs. Phase 13

```
                   start_generation
                          │
        master? + capable? ── no ──▶ _run_generation (unchanged)
                          │
                         yes
                          │
                          ▼
              _run_agentic_generation
              (new kwargs: review_enabled, generator_enabled)
                          │
            ┌─────────────┴─────────────┐
            │                            │
   review_enabled?                       │
            │                            │
            yes ─▶ iterate up to 3      no ─▶ ONE research pass
                   research↔review              (no verdict, no loop)
            │                            │
            └─────────────┬──────────────┘
                          │
                          ▼
              generator_enabled?
                          │
            ┌─────────────┴─────────────┐
            │                            │
           yes ─▶ stream_chat            no ─▶ emit findings text
                  generation pass              as single token event,
            │                                  persist as assistant row
            └─────────────┬──────────────┘
                          │
                          ▼
                    done event
```

---

## Files to change

### 1. `app/queries.py` — new setting helpers

Add immediately after `set_agentic_mode` (line 524).

```python
_REVIEW_ENABLED_KEY = "review_enabled"
_GENERATOR_ENABLED_KEY = "generator_enabled"


def get_review_enabled(conn: sqlite3.Connection) -> bool:
    """Return True when the review agent participates in the loop.

    Default (no row): True. The reviewer is on by default so the
    first-time experience after master-toggle-on matches Phase 13's
    full-loop behavior. Any value other than the literal string
    ``"off"`` returns True.

    Only meaningful when ``get_agentic_mode`` is True; the single-
    agent path ignores this setting.

    Args:
        conn: Open SQLite connection.
    """
    return get_setting(conn, _REVIEW_ENABLED_KEY, default="on") != "off"


def set_review_enabled(conn: sqlite3.Connection, enabled: bool) -> None:
    """Toggle the reviewer-participation setting.

    Args:
        conn: Open SQLite connection.
        enabled: True for ``"on"``, False for ``"off"``. Must be a
            real bool — same foot-gun guard as ``set_agentic_mode``.

    Raises:
        TypeError: When ``enabled`` is not a bool.
    """
    if not isinstance(enabled, bool):
        raise TypeError(
            f"set_review_enabled requires a bool; got {type(enabled).__name__}"
        )
    set_setting(conn, _REVIEW_ENABLED_KEY, "on" if enabled else "off")


def get_generator_enabled(conn: sqlite3.Connection) -> bool:
    """Return True when the generator agent participates in the loop.

    Default (no row): True. Same first-time-experience rationale as
    ``get_review_enabled``.

    Only meaningful when ``get_agentic_mode`` is True.

    Args:
        conn: Open SQLite connection.
    """
    return get_setting(conn, _GENERATOR_ENABLED_KEY, default="on") != "off"


def set_generator_enabled(conn: sqlite3.Connection, enabled: bool) -> None:
    """Toggle the generator-participation setting.

    Args:
        conn: Open SQLite connection.
        enabled: True for ``"on"``, False for ``"off"``. Must be a
            real bool.

    Raises:
        TypeError: When ``enabled`` is not a bool.
    """
    if not isinstance(enabled, bool):
        raise TypeError(
            f"set_generator_enabled requires a bool; got "
            f"{type(enabled).__name__}"
        )
    set_setting(conn, _GENERATOR_ENABLED_KEY, "on" if enabled else "off")
```

**Default-truthy vs. default-falsy.** `get_agentic_mode` defaults to
False (`!= "on"` returns False on absence). The new helpers default
to True (`!= "off"` returns True on absence). That asymmetry is
deliberate: master defaults off (agentic mode is opt-in), but once
you've opted in, both sub-agents default on (Phase 13 behavior).

### 2. `app/generation.py` — read flags, pass to agentic producer

Inside `start_generation`, replace the producer-selection block
(currently around lines 318–326):

```python
use_agentic = (
    queries.get_agentic_mode(db)
    and await ollama.model_supports_tools(client, model)
)
producer_kwargs: dict = {}
if use_agentic:
    from app.agents.loop import _run_agentic_generation
    producer = _run_agentic_generation
    producer_kwargs["review_enabled"] = queries.get_review_enabled(db)
    producer_kwargs["generator_enabled"] = queries.get_generator_enabled(db)
else:
    producer = _run_generation
```

Then in the `create_task` call, spread the kwargs:

```python
state.task = asyncio.create_task(
    producer(
        state=state,
        client=client,
        db=db,
        conversation_id=conversation_id,
        model=model,
        history=history,
        on_complete=on_complete,
        **producer_kwargs,
    )
)
```

`_run_generation` doesn't accept the new kwargs, so they're
conditionally added only on the agentic branch. Keeps the single-
agent signature stable.

### 3. `app/agents/loop.py` — branch on the two flags

Signature change for `_run_agentic_generation`:

```python
async def _run_agentic_generation(
    *,
    state: GenerationState,
    client: httpx.AsyncClient,
    db: sqlite3.Connection,
    conversation_id: int,
    model: str,
    history: list,
    on_complete: Literal["append", "replace"],
    review_enabled: bool = True,
    generator_enabled: bool = True,
) -> None:
```

Defaults to `True` for both so any test that constructs the producer
directly without the new flags keeps Phase-13 behavior.

#### 3a. Reviewer-off branch

After the research pass (line 446-ish, right after the
`research-findings` event is emitted), the current code runs the
review pass unconditionally. Wrap it:

```python
if review_enabled:
    # === Review pass === (existing code, lines 448–496 unchanged)
    review_payload = _build_review_payload(user_message, findings)
    try:
        verdict_calls, _ = await ollama.maybe_tool_call(
            client, model, review_payload, tools=REVIEW_TOOL_SPECS,
        )
    except (OllamaUnavailable, OllamaProtocolError) as e:
        persisted_or_errored = True
        await emit_ollama_error(state, e)
        return

    decision = parse_verdict(verdict_calls)
    queries.append_message(
        db, conversation_id, "review_verdict",
        content=json.dumps({
            "verdict": decision.verdict,
            "message": decision.message,
        }),
    )
    await _emit(
        state,
        "review-verdict",
        render.render_verdict_row(
            verdict_status=decision.verdict,
            verdict_message=decision.message,
            iteration_index=iteration_index,
            list_id=list_id,
        ),
    )

    final_findings = findings
    iterations_run = iteration_index
    if decision.verdict == "passed":
        break

    # Failed — push feedback into intra_turn ... (existing code)
    intra_turn.append({...})
else:
    # No reviewer: single research pass, exit loop immediately.
    final_findings = findings
    iterations_run = iteration_index
    break
```

The `for…else` branch (lines 497–508) that emits the max-iterations
badge is reachable only when `review_enabled=True` AND the loop ran
all 3 iterations without a `passed` verdict. Reviewer-off path
always hits `break` on iteration 1, so the for-else doesn't fire.
No change needed there.

#### 3b. Generator-off branch

Replace the generation pass (lines 510–558) with a conditional:

```python
if generator_enabled:
    # === Generation pass === (existing code, unchanged)
    generation_payload = _build_generation_payload(
        user_message, final_findings,
    )
    try:
        async for chunk in ollama.stream_chat(
            client, model, generation_payload,
        ):
            if chunk.content:
                chunks.append(chunk.content)
                await _emit(state, "token", html.escape(chunk.content))
            if chunk.done:
                break
    except (OllamaUnavailable, OllamaProtocolError) as e:
        persisted_or_errored = True
        await emit_ollama_error(state, e)
        return

    full_text = "".join(chunks)
else:
    # Generator off: stream the findings as a single token event so
    # the placeholder swap mechanism (#assistant-stream-{conv_id})
    # has been "fed" before the done event lands. Then persist the
    # findings text as the assistant row.
    full_text = final_findings
    if full_text:
        await _emit(state, "token", html.escape(full_text))

if on_complete == "append":
    message = queries.append_message(
        db, conversation_id, "assistant", full_text,
    )
else:
    message = queries.replace_last_assistant_message(
        db, conversation_id, full_text,
    )
persisted_or_errored = True

# (rest of the function — title generation + done event — unchanged)
```

The single `token` emit when generator is off is deliberate:

- It keeps the wire protocol uniform (every turn emits at least one
  `token` before `done`).
- It paints the answer text into the placeholder during the brief
  window before `done` lands, then the `done` event swaps in the
  markdown-rendered bubble. User sees a flash of plain text → fully
  rendered markdown, instead of an empty placeholder → fully
  rendered markdown.

The `html.escape` is mandatory — findings can contain `<`/`>`/`&`
and the placeholder accepts raw HTML.

#### 3c. Iteration-start event when reviewer is off

The `iteration-start` event still fires on iteration 1 (the for-
loop's first pass runs unconditionally). DOM ids stay
`{card_id}-iter-1-row-N` so historic-render alignment holds. No
code change needed for this — the existing emit at lines 316–324
runs.

#### 3d. Done-summary phrasing

`render.render_agentic_done_summary` currently says e.g.
"Researched in 1 iteration" / "Researched in 3 iterations".
Reviewer-off always produces 1 iteration. The phrasing reads
slightly odd ("iteration" implies plurality is possible), but
shipping it as-is is acceptable — adapt the helper only if it
feels off in browser smoke-testing. See section 4 for the
optional tweak.

### 4. `app/render.py` — optional summary phrasing tweak

Only ship this if smoke-testing shows the "1 iteration" phrasing
reads weirdly when reviewer is off.

`render_agentic_done_summary` gets an optional `review_enabled`
kwarg (default True for back-compat). When False, render
"Research complete" instead of "Researched in 1 iteration".

Defer the call-site update too: `_run_agentic_generation` would
pass `review_enabled=review_enabled` when calling
`render.render_agentic_done_summary`.

If the existing phrasing reads fine, skip this step entirely.

### 5. `app/routes.py` — two new toggle endpoints + GET context

#### 5a. Update `settings_endpoint` (lines 127–170)

Add the two new context vars:

```python
agentic_mode_on = queries.get_agentic_mode(db)
review_enabled = queries.get_review_enabled(db)
generator_enabled = queries.get_generator_enabled(db)
agentic_prompts = { ... }  # unchanged
```

Pass both into the template context for both branches (HX-Request
and full-page).

#### 5b. Update `toggle_agentic_mode_endpoint` (lines 247–278)

Add the same new context vars to the section fragment context, so
the sub-toggles render with current state when the master flips
on:

```python
return templates.TemplateResponse(
    request=request,
    name="_settings_agentic_section.html",
    context={
        "agentic_mode_on": agentic_mode_on,
        "review_enabled": queries.get_review_enabled(db),
        "generator_enabled": queries.get_generator_enabled(db),
        "agentic_prompts": { ... },  # unchanged
    },
)
```

#### 5c. New endpoint — reviewer toggle

```python
@router.post("/settings/agentic-review", response_class=HTMLResponse)
def toggle_review_enabled_endpoint(
    request: Request,
    db: DB,
    enabled: Annotated[str | None, Form()] = None,
) -> Response:
    """Toggle the reviewer-participation setting (phase 14).

    Presence-check on the ``enabled`` form field — checkbox sends
    ``enabled=on`` when checked, omits the field entirely when
    unchecked. Same convention as ``toggle_agentic_mode_endpoint``.

    Returns the agentic section fragment so HTMX swaps it in place;
    the toggle UI reflects the new state on the next render.
    """
    review_enabled = enabled is not None
    queries.set_review_enabled(db, review_enabled)
    return templates.TemplateResponse(
        request=request,
        name="_settings_agentic_section.html",
        context={
            "agentic_mode_on": queries.get_agentic_mode(db),
            "review_enabled": review_enabled,
            "generator_enabled": queries.get_generator_enabled(db),
            "agentic_prompts": {
                "research": RESEARCH_SYSTEM_PROMPT,
                "review": REVIEW_SYSTEM_PROMPT,
                "generation": GENERATION_SYSTEM_PROMPT,
            },
        },
    )
```

#### 5d. New endpoint — generator toggle

Same shape as 5c, with `set_generator_enabled` instead. Route path
`/settings/agentic-generator`. Naming the route `agentic-generator`
not `agentic-generation` is deliberate — keep parity with the
toggle's user-facing label ("Generator agent").

### 6. `templates/_settings_agentic_section.html` — nested sub-toggles

Current shape: master toggle + `{% if agentic_mode_on %}` block
containing the read-only prompt `<details>`. Drop the sub-toggles
into the `{% if %}` block, **before** the prompt `<details>`.

```html
{% if agentic_mode_on %}
<div class="agentic-sub-toggles">
  <p class="agentic-sub-toggles__help">
    Customize which agents participate. Research always runs (it
    produces the findings the others operate on).
  </p>

  <label class="agentic-sub-toggle">
    <input type="checkbox" name="enabled"
           {% if review_enabled %}checked{% endif %}
           hx-post="/settings/agentic-review"
           hx-trigger="change"
           hx-target="#settings-agentic-section"
           hx-swap="innerHTML">
    <span class="agentic-sub-toggle__label">
      Reviewer agent
      <small>
        When off, research runs a single pass — no iteration, no
        self-critique.
      </small>
    </span>
  </label>

  <label class="agentic-sub-toggle">
    <input type="checkbox" name="enabled"
           {% if generator_enabled %}checked{% endif %}
           hx-post="/settings/agentic-generator"
           hx-trigger="change"
           hx-target="#settings-agentic-section"
           hx-swap="innerHTML">
    <span class="agentic-sub-toggle__label">
      Generator agent
      <small>
        When off, research's findings are used as the answer
        verbatim (no separate synthesis pass).
      </small>
    </span>
  </label>
</div>

<details class="agentic-prompts"> ... </details>   {# existing #}
{% endif %}
```

Same HTMX target (`#settings-agentic-section`) and swap
(`innerHTML`) as the master toggle, so toggling any sub-toggle
re-renders the whole section with current state.

### 7. `static/style.css` — sub-toggle styling

Add after the existing `.agentic-mode-toggle` block (around line
1129):

```css
.agentic-sub-toggles {
  display: flex;
  flex-direction: column;
  gap: var(--space-sm);
  margin-top: var(--space-md);
  padding-left: var(--space-md);
  border-left: 2px solid var(--border);
}

.agentic-sub-toggles__help {
  color: var(--text-secondary);
  font-size: 13px;
  margin: 0 0 var(--space-xs);
}

.agentic-sub-toggle {
  display: flex;
  align-items: flex-start;
  gap: var(--space-sm);
  cursor: pointer;
  margin: 0;
}

.agentic-sub-toggle__label {
  display: flex;
  flex-direction: column;
  font-size: 14px;
}

.agentic-sub-toggle__label > small {
  color: var(--text-secondary);
  font-size: 12px;
  margin-top: 2px;
}
```

The left-border + indent visually communicates the cascade
relationship (sub-toggles are nested under the master).

---

## Test specs

### 8. `tests/test_queries.py` — new helpers

Add four new tests (or extend an existing parametrize block):

```python
def test_get_review_enabled_default_true(tmp_db_conn):
    # Absent row → True (default-on once master is on).
    assert queries.get_review_enabled(tmp_db_conn) is True


def test_set_review_enabled_roundtrip(tmp_db_conn):
    queries.set_review_enabled(tmp_db_conn, False)
    assert queries.get_review_enabled(tmp_db_conn) is False
    queries.set_review_enabled(tmp_db_conn, True)
    assert queries.get_review_enabled(tmp_db_conn) is True


def test_set_review_enabled_rejects_non_bool(tmp_db_conn):
    with pytest.raises(TypeError):
        queries.set_review_enabled(tmp_db_conn, "off")  # type: ignore
```

Same three tests for `generator_enabled`. ~6 tests total.

### 9. `tests/test_routes.py` — toggle endpoints

```python
def test_toggle_review_enabled_on(client, db_conn):
    r = client.post("/settings/agentic-review", data={"enabled": "on"})
    assert r.status_code == 200
    assert queries.get_review_enabled(db_conn) is True
    # Section fragment came back, with sub-toggle checked.
    assert 'name="enabled"' in r.text
    # Spot-check the checked attribute is in the right input.
    assert 'hx-post="/settings/agentic-review"' in r.text


def test_toggle_review_enabled_off(client, db_conn):
    queries.set_review_enabled(db_conn, True)
    r = client.post("/settings/agentic-review", data={})  # no `enabled`
    assert r.status_code == 200
    assert queries.get_review_enabled(db_conn) is False


def test_toggle_generator_enabled_on(...): ...  # mirror
def test_toggle_generator_enabled_off(...): ...  # mirror
```

### 10. `tests/test_agents_loop.py` — four configurations

Each configuration gets one happy-path integration test. The
existing Phase 13 happy-path test covers the (on, on, on) case.

```python
async def test_agentic_loop_reviewer_off_runs_single_pass(...):
    """review_enabled=False → one research iteration, no verdict event."""
    # Set up: agentic mode on, review off, generator on.
    # Mock maybe_tool_call to: return empty tool_calls + findings text on
    # first call (research). Mock stream_chat for generation.
    # Run _run_agentic_generation directly with review_enabled=False.
    # Assert:
    #   - exactly one `iteration-start` event
    #   - exactly one `research-findings` event
    #   - ZERO `review-verdict` events
    #   - ZERO `max-iterations` events
    #   - exactly one `done` event
    #   - assistant row content matches stream_chat's mocked output
    #   - no `review_verdict` rows in DB


async def test_agentic_loop_generator_off_uses_findings_verbatim(...):
    """generator_enabled=False → no stream_chat call, findings = assistant."""
    # Set up: review on, generator off.
    # Mock maybe_tool_call x2: first returns research findings text;
    # second (review) returns mark_passed.
    # DO NOT mock stream_chat — assert it's never called.
    # Run with generator_enabled=False.
    # Assert:
    #   - one `iteration-start`, one `research-findings`, one `review-verdict`
    #   - exactly one `token` event whose payload is the html-escaped findings
    #   - one `done` event
    #   - assistant row content == findings text (verbatim)


async def test_agentic_loop_both_off_minimal_path(...):
    """review_enabled=False AND generator_enabled=False."""
    # Research returns findings.
    # No review call, no generation call.
    # Assert:
    #   - one `iteration-start`, one `research-findings`, ZERO verdicts
    #   - exactly one `token` event with the findings
    #   - assistant row == findings text


async def test_agentic_loop_default_kwargs_match_phase13(...):
    """Backwards-compat: omitting the new kwargs == Phase 13 behavior."""
    # Same fixture as the existing Phase 13 happy-path test, but call
    # _run_agentic_generation without review_enabled/generator_enabled.
    # Assert the same event sequence as Phase 13.
```

### 11. `tests/test_settings_ui.py` (or wherever the settings template is exercised)

Two new tests:

```python
def test_settings_renders_sub_toggles_when_master_on(client, db_conn):
    queries.set_agentic_mode(db_conn, True)
    r = client.get("/settings")
    assert 'hx-post="/settings/agentic-review"' in r.text
    assert 'hx-post="/settings/agentic-generator"' in r.text


def test_settings_hides_sub_toggles_when_master_off(client, db_conn):
    queries.set_agentic_mode(db_conn, False)
    r = client.get("/settings")
    assert 'hx-post="/settings/agentic-review"' not in r.text
    assert 'hx-post="/settings/agentic-generator"' not in r.text
```

### 12. Dispatcher coverage

One test that exercises `start_generation`'s new branch where it
reads the two new settings and passes them through:

```python
async def test_start_generation_passes_subagent_flags_to_agentic(...):
    # Mock model_supports_tools=True, agentic_mode=True, review=False, generator=True.
    # Spy on _run_agentic_generation to capture its kwargs.
    # Assert review_enabled=False, generator_enabled=True passed.
```

---

## Commit order

Six commits, each independently runnable with `pytest` green.

1. **`feat: queries — review/generator enabled settings`**
   - `app/queries.py` (new helpers)
   - `tests/test_queries.py` (round-trip + TypeError tests)
   - No behavior change yet — just storage.

2. **`feat: agentic loop — review/generator branching`**
   - `app/agents/loop.py` (new kwargs, branched paths)
   - `tests/test_agents_loop.py` (four configurations)
   - Producer learns the flags; default-kwarg fallback keeps
     existing callers green.

3. **`feat: dispatcher — read sub-agent settings`**
   - `app/generation.py:start_generation` (read flags, pass kwargs)
   - One dispatcher test.
   - Now flags actually drive behavior end-to-end.

4. **`feat: settings UI — per-agent toggles`**
   - `app/routes.py` (two new endpoints + GET context updates)
   - `templates/_settings_agentic_section.html` (sub-toggles)
   - `static/style.css` (`.agentic-sub-toggles*` rules)
   - `tests/test_routes.py` (endpoint tests)
   - `tests/test_settings_ui.py` (template tests)
   - User can flip toggles in the browser.

5. **`docs: phase 14 plan`**
   - This file. (Already present when commit lands; this slot is
     where the doc gets refreshed if implementation reveals
     edits.)

6. **`docs: phase 14 retro + CLAUDE.md refresh`**
   - `docs/retros/phase14-per-agent-toggles.md`
   - Bump CLAUDE.md's "Current state" paragraph to mention phase 14.

---

## Open questions (defer or punt)

- **Streaming the research-as-answer text.** When generator is off,
  the findings come back as one chunk from `maybe_tool_call`, not
  streamed. A `maybe_tool_call_streaming` variant could yield
  tokens once the tool-call branch is rejected. Out of scope for
  v14; revisit if non-streamed answers feel sluggish.
- **`RESEARCH_AS_ANSWER_SYSTEM_PROMPT` variant.** Locked decision
  is to reuse the existing research prompt. If findings-tone in
  user-facing answers reads poorly, a one-line conditional in
  `_build_research_payload` to swap the prompt when
  `generator_enabled=False` would be cheap. Bench in real use
  first.
- **Sub-toggle UI when master flips off mid-session.** Today's
  HTMX swap re-renders the whole section, so flipping master off
  causes the `{% if agentic_mode_on %}` block (sub-toggles +
  prompts) to vanish. The DB rows for `review_enabled` /
  `generator_enabled` persist — when master flips back on, the
  prior sub-toggle state is restored. This is intentional. Worth
  a one-liner in the settings help text? Probably not for v1.
- **Per-chat snapshot.** If/when "play around" gives way to "I
  want chat X to always use this configuration", we'll add
  per-chat snapshotting. Not in v14.

---

## Definition of done

- All four toggle combinations exercised by tests, green.
- 393+ tests passing (no regressions); coverage on `app/agents/`
  and `app/queries.py` stays at or above current numbers.
- Browser smoke test:
  - Settings page: master toggle on/off cycles correctly; sub-
    toggles appear/disappear on master state change; both sub-
    toggles toggle independently and persist across reload.
  - Send a message with each of the four configurations; verify
    the tool card reflects the expected shape (verdict rows
    when review on, no verdict rows when review off; final
    bubble matches expected source per generator on/off).
- `docs/retros/phase14-per-agent-toggles.md` written.
- `CLAUDE.md` "Current state" bumped to phase 14.
