"""Phase 5 (extended in 12f): async HTTP client wrapping the local Ollama server.

Operations, all async:

- ``list_models`` — ``GET /api/tags``, returns every installed model name.
- ``list_tool_capable_models`` (12f) — same, filtered by ``/api/show`` to
  models whose capability list includes ``"tools"``. Cached per process.
- ``model_supports_tools`` (12f) — membership check against the cache used
  to gate the ``tools=`` payload at the generation layer.
- ``stream_chat`` — ``POST /api/chat`` with ``stream=true``, yields
  ``ChatChunk`` objects as Ollama emits them (NDJSON, one JSON object per
  line).
- ``maybe_tool_call`` — non-streaming ``POST /api/chat`` used to detect
  whether the model wants to call a tool before opening the stream.
- ``generate_title`` — single-shot ``POST /api/chat`` that asks the chat's
  own model to summarise the conversation as a sidebar title.

Two failure classes are surfaced so the UI can present different errors:

- ``OllamaUnavailable`` — transport problems: connect refused, timeout,
  5xx, malformed URL. "Ollama isn't running."
- ``OllamaProtocolError`` — Ollama responded, but with something we
  can't parse: invalid JSON or an unexpected response shape. "Ollama
  returned something I don't understand."
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

from app.config import ollama_host


class OllamaUnavailable(Exception):
    """Raised when Ollama can't be reached or returns an HTTP error.

    Transport-layer failures: connect refused, timeout, non-2xx status,
    malformed URL. Distinguish from ``OllamaProtocolError``, which
    means Ollama answered but the payload wasn't what we expected.

    The original httpx exception is preserved via ``__cause__`` so
    callers can log it without losing detail.
    """


class OllamaProtocolError(Exception):
    """Raised when Ollama responds with something we can't parse.

    Bad JSON, a response missing required fields, or values of the
    wrong type. Distinguish from ``OllamaUnavailable``, which means
    Ollama couldn't be reached at all.

    The original parse exception is preserved via ``__cause__``.
    """




@dataclass(frozen=True)
class ChatChunk:
    """One streamed chunk of an assistant response.

    Attributes:
        content: The piece of assistant text emitted in this chunk.
            May be the empty string — Ollama emits an empty content on
            the final ``done`` chunk.
        done: ``True`` on the final chunk of the stream, ``False`` for
            every intermediate chunk. Callers stop iterating once they
            see ``done=True``.
        prompt_tokens: Tokens Ollama evaluated for the input prompt for
            this turn (system + history + new user message). Only set on
            the final ``done`` chunk; ``None`` on intermediate chunks
            and on responses where Ollama didn't report a count (e.g.
            full prompt-cache hit). Use the most-recent turn's value
            as "current context size" — summing across turns
            double-counts shared history.
        eval_tokens: Tokens the model generated in this turn. Only set
            on the final ``done`` chunk; ``None`` otherwise.
    """

    content: str
    done: bool
    prompt_tokens: int | None = None
    eval_tokens: int | None = None


def create_client() -> httpx.AsyncClient:
    """Build an ``httpx.AsyncClient`` targeting the local Ollama server.

    Reads ``OLLAMA_HOST`` from ``.env`` (via ``app.config``). The
    returned client is not entered as a context manager — the caller is
    responsible for closing it (typically Phase 6's FastAPI lifespan).

    Timeout policy: httpx's default is 5 seconds across all phases,
    which is far too tight for a local chat model's first-token
    latency on cold load (a 7B model can take 10-30 seconds to warm
    up the first time it's used in a session). We loosen READ to 120
    seconds — long enough for any reasonable cold-start — while
    keeping CONNECT at 5 seconds (a localhost connect that takes
    longer than that means Ollama is wedged, not slow). Per-call
    overrides still win, e.g. ``generate_title`` passes ``timeout=10``
    to bound how long the SSE connection stays open after the
    user-visible reply.

    Returns:
        A freshly built ``httpx.AsyncClient`` with ``base_url`` and a
        chat-friendly default timeout configured.

    Raises:
        KeyError: If ``OLLAMA_HOST`` is not set in ``.env`` or the
            environment.
    """
    return httpx.AsyncClient(
        base_url=ollama_host(),
        timeout=httpx.Timeout(300.0, connect=5.0),
    )


async def list_models(client: httpx.AsyncClient) -> list[str]:
    """Return the names of every model installed in the Ollama server.

    Args:
        client: An ``httpx.AsyncClient`` pointed at the Ollama host
            (typically from ``create_client``).

    Returns:
        Model names in the order Ollama returned them, e.g.
        ``["llama3:latest", "qwen2.5:7b"]``. No sorting — the UI
        decides how to present them.

    Raises:
        OllamaUnavailable: Ollama is unreachable, the request timed
            out, or the server returned a non-2xx status.
        OllamaProtocolError: Ollama responded but the body wasn't
            valid JSON or didn't have the expected shape.
    """
    try:
        response = await client.get("/api/tags")
        response.raise_for_status()
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        # httpx.HTTPError is the umbrella for connection failures,
        # timeouts, and status errors; httpx.InvalidURL covers a
        # misconfigured OLLAMA_HOST. One wrapping exception lets the
        # caller catch a single type instead of three.
        raise OllamaUnavailable(f"Ollama request failed: {e}") from e

    try:
        # JSONDecodeError: body isn't JSON at all (e.g. an HTML error
        # page from a proxy). KeyError: no "models" or "name" field.
        # TypeError: "models" exists but isn't iterable, or a model
        # entry isn't a dict. All three mean "wrong shape" → protocol
        # error, not transport error.
        return [model["name"] for model in response.json()["models"]]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise OllamaProtocolError(
            f"Ollama returned an unexpected /api/tags shape: {e}"
        ) from e


# ---------------------------------------------------------------------------
# Tool-capability filtering (phase 12f)
# ---------------------------------------------------------------------------

# Ollama advertises model capabilities ("completion", "tools", "embedding",
# "vision", ...) on /api/show. We can't pass `tools=[...]` to a model that
# doesn't have "tools" in its capability list — Ollama 400s. Phase 12f uses
# the helpers below to (a) filter the composer dropdown to tool-capable
# models only and (b) gate the tools= payload at the generation layer as
# defense in depth for chats whose model has since lost tool support.

# 60s amortises the per-model /api/show round trips across composer
# re-renders without making installed-model updates feel stale (a freshly
# pulled model surfaces in the dropdown within a minute).
_CAPABILITY_TTL_SECONDS = 60.0

# Module-level cache. Single-process uvicorn means a single writer; the
# refresh runs on the event loop without external synchronisation. Shape
# when populated: {"expires_at": float (monotonic), "names": list[str]}.
_capability_cache: dict | None = None


async def list_tool_capable_models(client: httpx.AsyncClient) -> list[str]:
    """Return installed models whose /api/show capabilities include 'tools'.

    Fans /api/show out over the installed models with ``asyncio.gather``
    so a cold call against ~10 models lands in roughly 150ms instead of
    the ~500ms a sequential walk would cost. Results are cached for
    ``_CAPABILITY_TTL_SECONDS``; the next dropdown render is free.

    Args:
        client: An ``httpx.AsyncClient`` pointed at the Ollama host
            (typically from ``create_client``).

    Returns:
        Tool-capable model names in /api/tags order. Models whose
        /api/show probe fails for any reason are silently dropped —
        better to render a slightly short list than to fail the whole
        dropdown because a single misbehaving model errors on /show.

    Raises:
        OllamaUnavailable: /api/tags itself was unreachable or returned
            a non-2xx status. Same contract as ``list_models``.
        OllamaProtocolError: /api/tags returned a body we couldn't parse.
    """
    global _capability_cache
    now = time.monotonic()
    if _capability_cache and _capability_cache["expires_at"] > now:
        # Defensive copy so callers can't mutate the cached list.
        return list(_capability_cache["names"])

    all_models = await list_models(client)

    async def _supports(name: str) -> str | None:
        """Probe one model; return its name if tool-capable, else None."""
        try:
            resp = await client.post("/api/show", json={"model": name})
            resp.raise_for_status()
            caps = resp.json().get("capabilities") or []
            # Require BOTH "completion" and "tools". Ollama occasionally
            # reports `["embedding", "tools"]` for derived models (e.g.
            # reranker spinoffs from chat bases) that can't actually be
            # used as chat models — sending /api/chat to them produces a
            # garbage response or a 400. Legitimate chat-with-tools
            # models always advertise "completion"; embedders / rerankers
            # do not. Requiring both is the cheapest reliable filter we
            # have without maintaining a name-pattern denylist.
            return name if "completion" in caps and "tools" in caps else None
        except (
            httpx.HTTPError,
            httpx.InvalidURL,
            ValueError,
            KeyError,
            TypeError,
        ):
            # Either /api/show errored (HTTPError covers transport +
            # status + timeout) or the body wasn't shaped like we
            # expected. Drop this model rather than poisoning the
            # whole result; the rest of the dropdown stays usable.
            return None

    results = await asyncio.gather(*(_supports(n) for n in all_models))
    names = [n for n in results if n is not None]
    _capability_cache = {
        "expires_at": now + _CAPABILITY_TTL_SECONDS,
        "names": names,
    }
    return list(names)


async def model_supports_tools(
    client: httpx.AsyncClient, name: str
) -> bool:
    """Best-effort check used to gate the ``tools=`` payload on a chat call.

    The dropdown filter is the primary defense, but a chat row in SQLite
    pins whatever model the user picked when it was created — if that
    model later loses tool support (Ollama upgrade, model re-pulled
    without the capability), we still need to avoid 400ing the next
    message. This helper warms ``list_tool_capable_models`` and checks
    membership.

    Args:
        client: An ``httpx.AsyncClient`` pointed at the Ollama host.
        name: The model identifier to check (e.g. ``"llama3.1:8b"``).

    Returns:
        True if the cache lists ``name`` as tool-capable. False if it
        doesn't, OR if /api/tags failed (we'd rather skip ``tools=``
        and degrade to plain chat than risk a 400).
    """
    try:
        return name in await list_tool_capable_models(client)
    except (OllamaUnavailable, OllamaProtocolError):
        return False


def reset_capability_cache() -> None:
    """Drop the per-process capability cache.

    Test helper — production never calls this; the TTL handles refresh.
    Tests poke it between cases so module-level state doesn't leak.
    """
    global _capability_cache
    _capability_cache = None


async def stream_chat(
    client: httpx.AsyncClient,
    model: str,
    messages: list[dict[str, str]],
    temperature: float = 0.8,
    think: bool | None = None,
) -> AsyncIterator[ChatChunk]:
    """Stream a chat completion from Ollama, yielding chunks as they arrive.

    Args:
        client: An ``httpx.AsyncClient`` pointed at the Ollama host.
        model: Identifier of an installed Ollama model (e.g.
            ``"llama3:latest"``). Must be one of the names returned by
            ``list_models``.
        messages: Conversation history, oldest first. Each item is a
            dict with ``"role"`` (``"user"`` or ``"assistant"``) and
            ``"content"``. Phase 6 builds these from ``Message``
            dataclasses.
        temperature: Sampling temperature (0.0–2.0). Passed in Ollama's
            ``options`` dict; Ollama's own default is 0.8.
        think: When not ``None``, sets Ollama's ``think`` flag — ``False``
            suppresses a thinking model's reasoning phase (safe on any
            model), ``True`` requires a thinking-capable model (else Ollama
            400s). ``None`` omits the key, leaving Ollama's default.

    Yields:
        One ``ChatChunk`` per line of Ollama's NDJSON stream. The final
        chunk has ``done=True``; earlier chunks carry incremental text
        in ``content``.

    Raises:
        OllamaUnavailable: Ollama is unreachable, the request timed out
            mid-stream, or the server returned a non-2xx status.
        OllamaProtocolError: A line of the NDJSON stream wasn't valid
            JSON.
    """
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "options": {"temperature": temperature},
    }
    if think is not None:
        payload["think"] = think
    try:
        async with client.stream(
            "POST", "/api/chat", json=payload
        ) as response:
            response.raise_for_status()
            # Ollama emits newline-delimited JSON (NDJSON): one complete
            # JSON object per line, each representing either an
            # incremental token batch or the final `done` marker.
            async for line in response.aiter_lines():
                if not line.strip():
                    # Skip stray blank lines defensively — the protocol
                    # doesn't forbid them, even if Ollama doesn't emit
                    # them in practice.
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError as e:
                    # The malformed line is a protocol error (Ollama is
                    # reachable but garbled), not a transport error.
                    # Raising here breaks out of the async generator;
                    # the outer httpx-handler doesn't catch
                    # OllamaProtocolError so it propagates cleanly.
                    raise OllamaProtocolError(
                        f"Ollama emitted a non-JSON line: {line!r}"
                    ) from e
                done = bool(data.get("done", False))
                # prompt_eval_count / eval_count are only populated on
                # the final chunk; pull them out so the producer can
                # persist per-turn token counts. Ollama omits these
                # entirely on full prompt-cache hits — treat missing as
                # None rather than 0 so the UI can distinguish "no
                # data" from "actually zero tokens evaluated."
                prompt_tokens = data.get("prompt_eval_count") if done else None
                eval_tokens = data.get("eval_count") if done else None
                yield ChatChunk(
                    content=data.get("message", {}).get("content", ""),
                    done=done,
                    prompt_tokens=prompt_tokens,
                    eval_tokens=eval_tokens,
                )
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        raise OllamaUnavailable(f"Ollama stream failed: {e}") from e


async def maybe_tool_call(
    client: httpx.AsyncClient,
    model: str,
    messages: list[dict],
    tools: list[dict] | None,
    temperature: float = 0.8,
    think: bool | None = None,
) -> tuple[list[dict], str]:
    """Single non-streaming /api/chat to detect tool calls.

    Used by phase 12d's tool-calling loop: before opening a streaming
    response, ask Ollama (in one shot) whether the model wants to call
    a tool. If yes, the loop handles the tool then re-asks; if no, the
    loop falls through to ``stream_chat`` for the visible response.

    Yes, this costs an extra round-trip per "final answer" turn — Ollama
    is invoked once non-streaming to detect tool intent, then again
    streaming for the actual reply. Acceptable for a local app; revisit
    if the latency becomes annoying.

    Args:
        client: An ``httpx.AsyncClient`` pointed at the Ollama host.
        model: Identifier of an installed Ollama model.
        messages: Conversation history in Ollama's wire format (already
            includes any tool_call / tool_result rows mapped by
            ``_build_history_payload``).
        tools: List of tool specs to advertise (Ollama's ``tools=`` body
            shape). Pass ``None`` to omit the key entirely — required for
            models that don't advertise tool capability (passing
            ``tools=[]`` for those models still trips a 400 from Ollama).
        temperature: Sampling temperature (0.0–2.0). Passed in Ollama's
            ``options`` dict; Ollama's own default is 0.8.

    Returns:
        A 2-tuple ``(tool_calls, content)``:
        - ``tool_calls``: list of ``{"name": str, "arguments": dict}``
          dicts, unwrapped from Ollama's
          ``{"function": {"name", "arguments"}}`` wire shape. Empty list
          when the model didn't request a tool.
        - ``content``: assistant text emitted alongside the tool call
          (usually empty when ``tool_calls`` is non-empty; some models
          add a brief explanatory sentence). Discarded by the loop in
          12d — the visible response comes from a subsequent streaming
          call.

    Raises:
        OllamaUnavailable: Ollama is unreachable, the request timed out,
            or the server returned a non-2xx status.
        OllamaProtocolError: Ollama responded but the body wasn't valid
            JSON or didn't have the expected shape.
    """
    payload: dict = {
        "model": model,
        "messages": messages,
        # Non-streaming — the whole assistant reply (or its tool_calls)
        # comes back in one JSON object rather than NDJSON.
        "stream": False,
        "options": {"temperature": temperature},
    }
    # Only include `tools` when there's something to advertise. Some
    # models 400 when given an empty list; passing None lets the caller
    # gate cleanly without us second-guessing.
    if tools:
        payload["tools"] = tools
    if think is not None:
        payload["think"] = think

    try:
        response = await client.post("/api/chat", json=payload)
        response.raise_for_status()
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        raise OllamaUnavailable(f"Ollama request failed: {e}") from e

    try:
        body = response.json()
        # `message` may be absent when the server returns an error-shaped
        # body that still squeaked through raise_for_status. Defaulting
        # to {} keeps the .get chains below honest.
        message = body.get("message", {})
        raw_calls = message.get("tool_calls") or []
        content = message.get("content") or ""
        # Ollama wraps each tool call as
        #   {"function": {"name": "...", "arguments": {...}}}
        # but the rest of the loop (run_tool, history persistence) wants
        # the flatter {"name", "arguments"} shape. Unwrap here so the
        # wire format only lives at this boundary.
        unwrapped = [
            {
                "name": tc["function"]["name"],
                "arguments": tc["function"].get("arguments", {}),
            }
            for tc in raw_calls
        ]
        return unwrapped, content
    except (KeyError, TypeError, ValueError) as e:
        raise OllamaProtocolError(
            f"Ollama returned an unexpected /api/chat shape: {e}"
        ) from e


async def generate_title(
    client: httpx.AsyncClient,
    model: str,
    history: list[dict[str, str]],
) -> str:
    """Ask the chat's own model to summarize the conversation as a title.

    Single-shot, non-streaming POST to ``/api/chat``. Reuses whatever
    model the conversation is already using — it's warm in memory
    from the assistant reply we just streamed, so the title roundtrip
    is fast and we avoid having to load and keep a second model
    resident.

    Args:
        client: An ``httpx.AsyncClient`` pointed at the Ollama host.
        model: The Ollama model identifier to use for the title.
            Phase 11d passes the conversation's own model; the chat
            just used it successfully, so it's guaranteed installed
            and warm.
        history: The conversation so far — the same wire-format list
            that ``stream_chat`` accepts (``[{"role", "content"}, ...]``).
            The function appends a title-request user turn before
            sending; callers don't need to.

    Returns:
        The model-generated title, stripped of surrounding quotes,
        capped at 6 words, and capped at 80 characters as a final
        defense against runaway words. Empty strings are possible
        if the model misbehaves; the caller is expected to treat
        empty as "skip the rename".

    Raises:
        OllamaUnavailable: Ollama is unreachable, the request timed
            out, or the server returned a non-2xx status.
        OllamaProtocolError: Ollama responded with JSON we couldn't
            parse into the expected ``{"message": {"content": ...}}``
            shape.
    """
    # Instruction is delivered as a final user turn so the model treats
    # it as the current request, not a system directive (small chat
    # models often ignore `system` messages in practice). Verb-first
    # ("Title this...") tends to behave better than "Summarize..." —
    # smaller models parse the latter literally and echo the
    # constraint phrasing in their reply.
    title_request = {
        "role": "user",
        "content": (
            "Give this conversation a noun-phrase title:"
            " 4 words or fewer, under 30 characters."
            " Reply with only the title, no punctuation."
        ),
    }
    payload = {
        "model": model,
        "messages": [*history, title_request],
        "stream": False,
    }

    try:
        # 10s cap on the title request. The chat model is already warm
        # (we just used it to stream the reply), so a few-token title
        # response should come back in well under a second. The cap
        # bounds how long the SSE connection stays open if Ollama
        # wedges. On expiry httpx raises ReadTimeout (a subclass of
        # httpx.HTTPError), which the caller catches as
        # OllamaUnavailable → silent skip in _maybe_generate_title.
        response = await client.post(
            "/api/chat", json=payload, timeout=10.0
        )
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        raise OllamaUnavailable(f"Title request failed: {e}") from e

    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise OllamaUnavailable(f"Title request failed: {e}") from e

    try:
        text = response.json()["message"]["content"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise OllamaProtocolError(
            f"Ollama returned an unexpected /api/chat shape: {e}"
        ) from e

    # Defensive post-processing: tinyllama tends to wrap titles in
    # quotes despite the instruction, sometimes adds a trailing period,
    # and occasionally spits out a multi-line "reasoning" preamble.
    # Strip those and cap length so a runaway response can't become a
    # 5000-char sidebar row.
    text = text.strip()

    # Take the first non-empty line — guards against the model
    # emitting "Here is your title:\n\nFoo Bar".
    for line in text.splitlines():
        if line.strip():
            text = line.strip()
            break

    # Strip a balanced set of common surrounding quote characters.
    text = text.strip(' "“”‘’\'.')

    # Strip preambles tinyllama loves to add despite the instructions.
    # Each entry is checked case-insensitively at the start of the
    # string. Order doesn't matter — only one match is stripped per
    # run, and the loop is cheap.
    preambles = (
        "title:",
        "chat title:",
        "conversation title:",
        "conversation:",
        "summary:",
        "here is the title:",
        "here is your title:",
        "the title is:",
    )
    lowered = text.lower()
    for prefix in preambles:
        if lowered.startswith(prefix):
            text = text[len(prefix):].lstrip(' "“”‘’\'')
            break

    # Enforce the 4-word cap. The prompt asks for "4 words or fewer"
    # but smaller models routinely overshoot. `split()` with no args
    # splits on any whitespace run and drops empties, so it handles
    # tabs and double-spaces correctly without inventing empty words.
    words = text.split()
    if len(words) > 4:
        text = " ".join(words[:4])

    # 30-char cap mirrors the prompt hint and is a safety net against
    # pathological word lengths. With normal English the 4-word cap
    # dominates and this is a no-op.
    return text[:30]
