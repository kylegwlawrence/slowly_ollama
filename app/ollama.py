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




def _url(path: str, host: str | None = None) -> str:
    """Build the URL passed to the shared httpx client.

    When ``host`` is None the path is returned unchanged — httpx merges
    it with the client's ``base_url`` (the local ``OLLAMA_HOST``). When
    ``host`` is set, an absolute URL is built that overrides ``base_url``
    on a per-call basis, so a single shared client can target the local
    Ollama for most operations and a remote one for agent turns.

    Args:
        path: The Ollama API path, leading slash included (e.g.
            ``"/api/chat"``).
        host: Optional override base URL (e.g. ``"http://host1:11434"``).
            ``None`` falls through to the client's ``base_url``.

    Returns:
        Either ``path`` unchanged or ``f"{host}{path}"`` with any
        trailing slash on ``host`` stripped.
    """
    if host is None:
        return path
    return f"{host.rstrip('/')}{path}"


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
    up the first time it's used in a session). We loosen READ to 600
    seconds (10 minutes) — long enough for any reasonable cold-start
    and large context processing — and set CONNECT to 10 seconds. A
    localhost connect is sub-second, but ``OLLAMA_HOST`` can point at a
    remote machine over a private network (the split deployment in
    ``docs/plans/phase23-split-deployment.md``), where the first connect
    to an idle peer may be relayed or wait for the peer to wake — 10s
    absorbs that without masking a genuinely wedged server.
    Per-call overrides still win, e.g. ``generate_title`` passes
    ``timeout=10`` to bound how long the SSE connection stays open after
    the user-visible reply.

    Returns:
        A freshly built ``httpx.AsyncClient`` with ``base_url`` and a
        chat-friendly default timeout configured.

    Raises:
        KeyError: If ``OLLAMA_HOST`` is not set in ``.env`` or the
            environment.
    """
    return httpx.AsyncClient(
        base_url=ollama_host(),
        timeout=httpx.Timeout(600.0, connect=10.0),
    )


async def list_models(
    client: httpx.AsyncClient, host: str | None = None
) -> list[str]:
    """Return the names of every model installed in the Ollama server.

    Args:
        client: An ``httpx.AsyncClient`` pointed at the Ollama host
            (typically from ``create_client``).
        host: Optional override base URL (e.g. ``"http://host1:11434"``).
            ``None`` queries the client's ``base_url`` (the primary host);
            set it to list a second host's models (the "host2" host picker).

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
        response = await client.get(_url("/api/tags", host))
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

# Phase 25: a sibling cache for the "thinking"-capable model set, used to mark
# composer model options so the Think select can show/hide as the model changes.
# Same shape + TTL as ``_capability_cache``; kept separate so the two probes
# don't invalidate each other.
_thinking_cache: dict | None = None


async def list_tool_capable_models(
    client: httpx.AsyncClient, host: str | None = None
) -> list[str]:
    """Return installed models whose /api/show capabilities include 'tools'.

    Fans /api/show out over the installed models with ``asyncio.gather``
    so a cold call against ~10 models lands in roughly 150ms instead of
    the ~500ms a sequential walk would cost. Results are cached for
    ``_CAPABILITY_TTL_SECONDS``; the next dropdown render is free.

    Args:
        client: An ``httpx.AsyncClient`` pointed at the Ollama host
            (typically from ``create_client``).
        host: Optional override base URL. ``None`` lists the primary host
            (cached). When set (the "host2" host picker), the probe targets
            that host and is NOT cached — the module cache is host-agnostic,
            and mixing a second host's models into it would corrupt the
            primary dropdown.

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
    if host is None and _capability_cache and _capability_cache["expires_at"] > now:
        # Defensive copy so callers can't mutate the cached list.
        return list(_capability_cache["names"])

    all_models = await list_models(client, host=host)

    async def _supports(name: str) -> str | None:
        """Probe one model; return its name if tool-capable, else None."""
        try:
            resp = await client.post(_url("/api/show", host), json={"model": name})
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
    if host is None:
        # Only the primary host is cached; a second host's list bypasses the
        # host-agnostic cache (see the docstring).
        _capability_cache = {
            "expires_at": now + _CAPABILITY_TTL_SECONDS,
            "names": names,
        }
    return list(names)


async def list_thinking_capable_models(
    client: httpx.AsyncClient, host: str | None = None
) -> list[str]:
    """Return installed models whose /api/show capabilities include 'thinking'.

    Phase 25 sibling to :func:`list_tool_capable_models`. The composer marks
    each model option with this set so the Think select can show/hide as the
    user changes models without a round trip. Same /api/show fan-out, TTL
    cache, and host semantics — ``host=None`` is cached (primary host); a
    non-primary ``host`` bypasses the cache (it's host-agnostic).

    Args:
        client: An ``httpx.AsyncClient`` pointed at the Ollama host.
        host: Optional override base URL. ``None`` lists the primary host
            (cached); when set, the probe targets that host and is NOT cached.

    Returns:
        Thinking-capable model names in /api/tags order. Models whose
        /api/show probe fails are silently dropped (same policy as the
        tool-capable list).

    Raises:
        OllamaUnavailable: /api/tags itself was unreachable or non-2xx.
        OllamaProtocolError: /api/tags returned a body we couldn't parse.
    """
    global _thinking_cache
    now = time.monotonic()
    if host is None and _thinking_cache and _thinking_cache["expires_at"] > now:
        return list(_thinking_cache["names"])

    all_models = await list_models(client, host=host)

    async def _supports(name: str) -> str | None:
        """Probe one model; return its name if thinking-capable, else None."""
        try:
            resp = await client.post(_url("/api/show", host), json={"model": name})
            resp.raise_for_status()
            caps = resp.json().get("capabilities") or []
            return name if "thinking" in caps else None
        except (
            httpx.HTTPError,
            httpx.InvalidURL,
            ValueError,
            KeyError,
            TypeError,
        ):
            return None

    results = await asyncio.gather(*(_supports(n) for n in all_models))
    names = [n for n in results if n is not None]
    if host is None:
        _thinking_cache = {
            "expires_at": now + _CAPABILITY_TTL_SECONDS,
            "names": names,
        }
    return list(names)


async def model_supports_tools(
    client: httpx.AsyncClient, name: str, host: str | None = None
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
        host: When set, probe this remote host with a single ``/api/show``
            instead of consulting the local capability cache. The cache
            is per-process and host-agnostic; mixing remote and local
            entries would corrupt it. ``None`` keeps the existing
            cache-warmed local path.

    Returns:
        True if the cache lists ``name`` as tool-capable. False if it
        doesn't, OR if the probe failed (we'd rather skip ``tools=``
        and degrade to plain chat than risk a 400).
    """
    if host is not None:
        # Direct one-shot probe against the remote host. The cache stays
        # local-only — remote callers each pay one /api/show round-trip,
        # which is the same cost the local path pays on a cache miss.
        try:
            resp = await client.post(
                _url("/api/show", host), json={"model": name}
            )
            resp.raise_for_status()
            caps = resp.json().get("capabilities") or []
            return "completion" in caps and "tools" in caps
        except (
            httpx.HTTPError,
            httpx.InvalidURL,
            ValueError,
            KeyError,
            TypeError,
        ):
            return False
    try:
        return name in await list_tool_capable_models(client)
    except (OllamaUnavailable, OllamaProtocolError):
        return False


async def model_supports_thinking(
    client: httpx.AsyncClient, name: str, host: str | None = None
) -> bool:
    """Best-effort check: does ``name`` advertise the 'thinking' capability?

    Phase 25 uses this to gate the per-chat thinking toggle in the chat
    header — the control only renders for reasoning models. A single
    ``/api/show`` round-trip, mirroring :func:`model_supports_tools` but
    without the process cache: thinking-gating happens once per chat-panel
    render, the same cost (and on the same path) as the ``is_model_loaded``
    probe already there.

    Args:
        client: An ``httpx.AsyncClient`` pointed at the Ollama host.
        name: The model identifier to check (e.g. ``"qwen3.5:9b"``).
        host: Optional override base URL. ``None`` probes the client's
            ``base_url`` (the primary host); set it to probe a non-primary
            host (the chat's selected host).

    Returns:
        True if ``/api/show`` lists ``"thinking"`` in the model's
        capabilities. False on any failure (transport, status, or an
        unexpected body) — we'd rather hide the toggle than render it on a
        model that can't think.
    """
    try:
        resp = await client.post(_url("/api/show", host), json={"model": name})
        resp.raise_for_status()
        caps = resp.json().get("capabilities") or []
        return "thinking" in caps
    except (
        httpx.HTTPError,
        httpx.InvalidURL,
        ValueError,
        KeyError,
        TypeError,
    ):
        return False


def reset_capability_cache() -> None:
    """Drop the per-process capability caches (tools + thinking).

    Test helper — production never calls this; the TTL handles refresh.
    Tests poke it between cases so module-level state doesn't leak.
    """
    global _capability_cache, _thinking_cache
    _capability_cache = None
    _thinking_cache = None


# ---------------------------------------------------------------------------
# Memory residency (post-phase-19 "unload" chip)
# ---------------------------------------------------------------------------
#
# Ollama lazy-loads each model on first use and keeps it resident for ~5 min
# of idle (the default `keep_alive`). The header model chip exposes a manual
# unload so the user can free VRAM without waiting for the idle timer, and
# `/api/ps` lets us colour the chip to reflect the actual residency state.


async def list_loaded_models(
    client: httpx.AsyncClient, host: str | None = None
) -> list[str]:
    """Return the names of models currently held in Ollama's memory.

    Wraps ``GET /api/ps`` — Ollama's "what's resident right now" endpoint.
    Distinct from :func:`list_models`, which reports every *installed*
    model regardless of memory state.

    Args:
        client: An ``httpx.AsyncClient`` pointed at the Ollama host
            (typically from ``create_client``).
        host: Optional override base URL. ``None`` queries the client's
            ``base_url`` (the primary host); set it to probe a non-primary
            host's residency (the header chip targets the selected host).

    Returns:
        Loaded model names in the order Ollama returned them. Empty
        list when nothing is loaded.

    Raises:
        OllamaUnavailable: Ollama is unreachable, the request timed
            out, or the server returned a non-2xx status.
        OllamaProtocolError: Ollama responded but the body wasn't
            valid JSON or didn't have the expected shape.
    """
    try:
        response = await client.get(_url("/api/ps", host))
        response.raise_for_status()
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        raise OllamaUnavailable(f"Ollama request failed: {e}") from e

    try:
        return [model["name"] for model in response.json()["models"]]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise OllamaProtocolError(
            f"Ollama returned an unexpected /api/ps shape: {e}"
        ) from e


async def is_model_loaded(
    client: httpx.AsyncClient, name: str, host: str | None = None
) -> bool:
    """Best-effort check: is ``name`` resident in Ollama right now?

    Wraps :func:`list_loaded_models` and swallows Ollama errors —
    callers use this purely to colour the header chip, so a transient
    /api/ps failure should default to "looks loaded" (the chip stays in
    its normal colour) rather than fail the whole chat-panel render.

    Args:
        client: An ``httpx.AsyncClient`` pointed at the Ollama host.
        name: The model identifier to check (e.g. ``"llama3.1:8b"``).
        host: Optional override base URL. ``None`` checks the primary host;
            set it so the header chip reflects residency on the chat's
            selected non-primary host.

    Returns:
        True if /api/ps lists ``name``. False only when /api/ps
        succeeded AND ``name`` was absent. On any failure, returns
        True — better to show the chip as loaded and let the next
        click correct it than to lie about residency.
    """
    try:
        return name in await list_loaded_models(client, host=host)
    except (OllamaUnavailable, OllamaProtocolError):
        return True


async def unload_model(
    client: httpx.AsyncClient, name: str, host: str | None = None
) -> None:
    """Ask Ollama to evict ``name`` from memory immediately.

    Ollama's unload protocol: POST ``/api/generate`` (or ``/api/chat``)
    with ``keep_alive: 0`` and no prompt. The server drops the model
    from VRAM/RAM and replies with a small JSON ack. This is the same
    mechanism Ollama uses for its own idle eviction, just triggered on
    demand.

    Unloading a model that isn't loaded is a no-op on Ollama's side and
    still returns 200 — the caller doesn't need to check residency first.

    Args:
        client: An ``httpx.AsyncClient`` pointed at the Ollama host.
        name: The model identifier to evict (e.g. ``"llama3.1:8b"``).
        host: Optional override base URL. ``None`` unloads from the primary
            host; set it to evict from the chat's selected non-primary host.

    Raises:
        OllamaUnavailable: Ollama is unreachable, the request timed out,
            or the server returned a non-2xx status. The chip-flip in the
            UI is best-effort; the caller decides whether to surface this
            to the user.
    """
    try:
        response = await client.post(
            _url("/api/generate", host),
            json={"model": name, "keep_alive": 0},
            timeout=10.0,
        )
        response.raise_for_status()
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        raise OllamaUnavailable(f"Ollama unload failed: {e}") from e


async def stream_chat(
    client: httpx.AsyncClient,
    model: str,
    messages: list[dict[str, str]],
    temperature: float = 0.8,
    think: bool | None = None,
    num_ctx: int | None = None,
    host: str | None = None,
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
        num_ctx: When not ``None``, sets Ollama's ``num_ctx`` option (the
            total context window in tokens — system + history + user
            input + generated reply all share this budget). ``None``
            omits the key, leaving Ollama's own default of 2048.

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
    options: dict = {"temperature": temperature}
    if num_ctx is not None:
        options["num_ctx"] = num_ctx
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "options": options,
    }
    if think is not None:
        payload["think"] = think
    try:
        async with client.stream(
            "POST", _url("/api/chat", host), json=payload
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
    num_ctx: int | None = None,
    host: str | None = None,
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
    options: dict = {"temperature": temperature}
    if num_ctx is not None:
        options["num_ctx"] = num_ctx
    payload: dict = {
        "model": model,
        "messages": messages,
        # Non-streaming — the whole assistant reply (or its tool_calls)
        # comes back in one JSON object rather than NDJSON.
        "stream": False,
        "options": options,
    }
    # Only include `tools` when there's something to advertise. Some
    # models 400 when given an empty list; passing None lets the caller
    # gate cleanly without us second-guessing.
    if tools:
        payload["tools"] = tools
    if think is not None:
        payload["think"] = think

    try:
        response = await client.post(_url("/api/chat", host), json=payload)
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


async def summarize_conversation(
    client: httpx.AsyncClient,
    model: str,
    history: list[dict[str, str]],
    *,
    num_ctx: int | None = None,
    host: str | None = None,
) -> str:
    """Ask ``model`` to summarize ``history`` into a compact briefing.

    Phase 18: powers the manual ``Compact`` action. Single-shot,
    non-streaming POST to ``/api/chat``. Uses the chat's own model — it's
    already warm in Ollama's memory from the previous turn, so the
    round-trip is cheap and we don't load a second model resident.

    Unlike :func:`generate_title`, the model's output is returned with
    only whitespace stripping — no quote unwrapping, no word cap, no
    preamble heuristics. The summarization prompt is explicit enough
    that smaller models reply cleanly; if a model's output ever needs
    sanitizing, do it in the caller (the compact endpoint) so the
    helper stays a thin Ollama wrapper.

    Args:
        client: Async ``httpx.AsyncClient`` pointed at the Ollama host.
        model: Identifier of an installed Ollama model. The caller should
            pass the conversation's own model so the summarizer reuses the
            warm KV cache.
        history: Conversation rows in Ollama wire format (already mapped
            by :func:`app.generation.build_history_payload`). This helper
            does NOT filter — pass exactly the rows you want summarized.
        num_ctx: Per-project context-window override, mirroring
            :func:`stream_chat`. ``None`` omits the key (Ollama default).

    Returns:
        The stripped summary text. The caller is expected to treat the
        empty string as ``"skip — don't archive anything"``.

    Raises:
        OllamaUnavailable: Ollama is unreachable, the request timed out,
            or the server returned a non-2xx status.
        OllamaProtocolError: Ollama responded but the body wasn't valid
            JSON or didn't have the expected shape.
    """
    # The instruction is delivered as a final user turn, mirroring
    # generate_title. Small local models follow a verb-first user
    # instruction more reliably than a `system` directive.
    instruction = {
        "role": "user",
        "content": (
            "Summarize the conversation above into a compact briefing"
            " that preserves what the assistant needs to keep responding"
            " well. Keep:"
            "\n- the user's stated goals, constraints, and preferences"
            "\n- concrete facts, decisions, and conclusions"
            "\n- findings from tool calls (what was asked, what was found)"
            "\n- open questions and unresolved threads"
            "\n- any persona or style instructions the user gave"
            "\nOmit pleasantries, restated questions, and long verbatim"
            " quotes. Write a third-person briefing, not dialogue. Under"
            " ~400 words. Begin directly — no preamble."
        ),
    }
    # Low temperature: compaction is a recall task, not a generative one.
    # Hardcoded (not the chat's own temperature) so a creatively-tuned
    # chat doesn't get a creatively-summarized history.
    options: dict = {"temperature": 0.2}
    if num_ctx is not None:
        options["num_ctx"] = num_ctx
    payload = {
        "model": model,
        "messages": [*history, instruction],
        "stream": False,
        "options": options,
    }

    try:
        # 120s cap: the model is warm but the input may be the entire
        # conversation, so the time-to-first-token can be longer than a
        # title call. The default 300s read timeout on the shared client
        # would also work; the explicit value documents intent.
        response = await client.post(
            _url("/api/chat", host), json=payload, timeout=120.0
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
        The model-generated title, stripped of surrounding quotes
        and known preambles. Empty strings are possible if the model
        misbehaves; the caller is expected to treat empty as "skip
        the rename".

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

    return text
