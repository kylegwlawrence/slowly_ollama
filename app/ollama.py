"""Async HTTP client wrapping the local Ollama server.

Operations, all async:

- ``list_models`` — ``GET /api/tags``, returns every installed model name.
- ``list_tool_capable_models`` — same, filtered by ``/api/show`` to models
  whose capabilities include ``"tools"``. Cached per process.
- ``model_supports_tools`` — membership check against that cache, used to
  gate the ``tools=`` payload at the generation layer.
- ``stream_chat`` — ``POST /api/chat`` with ``stream=true``, yields
  ``ChatChunk`` objects as Ollama emits them (NDJSON, one object per line).
- ``maybe_tool_call`` — non-streaming ``POST /api/chat`` to detect whether
  the model wants to call a tool before opening the stream.
- ``generate_title`` — single-shot ``POST /api/chat`` that asks the chat's
  own model to summarise the conversation as a sidebar title.

Two failure classes let the UI present different errors:

- ``OllamaUnavailable`` — transport problems (connect refused, timeout,
  5xx, malformed URL): "Ollama isn't running."
- ``OllamaProtocolError`` — Ollama responded but with something we can't
  parse (invalid JSON or unexpected shape): "Ollama returned something I
  don't understand."
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

from app.config import ollama_chat_timeout, ollama_host, ollama_util_timeout


class OllamaUnavailable(Exception):
    """Raised when Ollama can't be reached or returns an HTTP error.

    Transport-layer failures: connect refused, timeout, non-2xx status,
    malformed URL. Contrast ``OllamaProtocolError``, where Ollama answered
    but the payload wasn't what we expected. The original httpx exception is
    preserved via ``__cause__``.
    """


class OllamaProtocolError(Exception):
    """Raised when Ollama responds with something we can't parse.

    Bad JSON, missing required fields, or wrong-typed values. Contrast
    ``OllamaUnavailable``, where Ollama couldn't be reached at all. The
    original parse exception is preserved via ``__cause__``.
    """




def _url(path: str, host: str | None = None) -> str:
    """Build the URL passed to the shared httpx client.

    With ``host`` None, returns ``path`` unchanged — httpx merges it with the
    client's ``base_url`` (the local ``OLLAMA_HOST``). With ``host`` set,
    builds an absolute URL that overrides ``base_url`` per call, so one shared
    client can target the local Ollama for most operations and a remote one
    for agent turns.

    Args:
        path: The Ollama API path, leading slash included (e.g. ``"/api/chat"``).
        host: Optional override base URL (e.g. ``"http://host1:11434"``).
            ``None`` falls through to the client's ``base_url``.

    Returns:
        ``path`` unchanged, or ``f"{host}{path}"`` with any trailing slash on
        ``host`` stripped.
    """
    if host is None:
        return path
    return f"{host.rstrip('/')}{path}"


@dataclass(frozen=True)
class ChatChunk:
    """One streamed chunk of an assistant response.

    Attributes:
        content: Assistant text emitted in this chunk. May be empty —
            Ollama emits empty content on the final ``done`` chunk.
        thinking: Reasoning text emitted in this chunk. Ollama streams a
            thinking model's reasoning in a SEPARATE ``message.thinking``
            field (not inline ``<think>`` tags), parallel to ``content``:
            thinking chunks arrive first (``content`` empty), then content
            chunks (``thinking`` empty). Empty on non-thinking models and
            when the ``think`` flag is off. Defaulted so existing
            constructions stay valid.
        done: ``True`` on the final chunk, ``False`` otherwise. Callers stop
            iterating once they see ``done=True``.
        prompt_tokens: Tokens Ollama evaluated for this turn's input prompt
            (system + history + new user message). Set only on the ``done``
            chunk; ``None`` on intermediate chunks and when Ollama didn't
            report a count (e.g. full prompt-cache hit). Use the latest
            turn's value as "current context size" — summing across turns
            double-counts shared history.
        eval_tokens: Tokens the model generated this turn. Set only on the
            ``done`` chunk; ``None`` otherwise.
    """

    content: str
    done: bool
    thinking: str = ""
    prompt_tokens: int | None = None
    eval_tokens: int | None = None


def create_client() -> httpx.AsyncClient:
    """Build an ``httpx.AsyncClient`` targeting the local Ollama server.

    Reads ``OLLAMA_HOST`` from ``.env`` (via ``app.config``). The returned
    client is not a context manager — the caller closes it (typically the
    FastAPI lifespan).

    Timeout policy: httpx's 5s default is far too tight for a local chat
    model's first-token latency on cold load (a 7B model can take 10-30s to
    warm up). The READ timeout is sourced from ``OLLAMA_CHAT_TIMEOUT`` (via
    :func:`app.config.ollama_chat_timeout`, default 600s) — ample for any
    cold-start or large-context processing — and CONNECT is fixed at 10s. A
    localhost connect is sub-second, but ``OLLAMA_HOST`` can point at a remote
    machine over a private network (the split deployment), where the first
    connect to an idle peer may be relayed or wait for it to wake; 10s absorbs
    that without masking a wedged server. Per-call overrides still win — the
    short metadata calls pass ``timeout=ollama_util_timeout()``.

    Returns:
        A fresh ``httpx.AsyncClient`` with ``base_url`` and a chat-friendly
        default timeout.

    Raises:
        KeyError: If ``OLLAMA_HOST`` is not set in ``.env`` or the environment.
    """
    return httpx.AsyncClient(
        base_url=ollama_host(),
        timeout=httpx.Timeout(ollama_chat_timeout(), connect=10.0),
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
        Model names in Ollama's order, e.g.
        ``["llama3:latest", "qwen2.5:7b"]``. Unsorted — the UI decides how
        to present them.

    Raises:
        OllamaUnavailable: Ollama unreachable, timed out, or non-2xx.
        OllamaProtocolError: Body wasn't valid JSON or had the wrong shape.
    """
    try:
        response = await client.get(
            _url("/api/tags", host), timeout=ollama_util_timeout()
        )
        response.raise_for_status()
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        # HTTPError is the umbrella for connection failures, timeouts, and
        # status errors; InvalidURL covers a misconfigured OLLAMA_HOST. One
        # wrapper lets the caller catch a single type.
        raise OllamaUnavailable(f"Ollama request failed: {e}") from e

    try:
        # JSONDecodeError: body isn't JSON (e.g. an HTML proxy error page).
        # KeyError: no "models"/"name" field. TypeError: "models" isn't
        # iterable, or an entry isn't a dict. All three → wrong shape.
        return [model["name"] for model in response.json()["models"]]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise OllamaProtocolError(
            f"Ollama returned an unexpected /api/tags shape: {e}"
        ) from e


# ---------------------------------------------------------------------------
# Tool-capability filtering
# ---------------------------------------------------------------------------

# Ollama advertises model capabilities ("completion", "tools", "embedding",
# "vision", ...) on /api/show. Passing `tools=[...]` to a model without
# "tools" 400s, so the helpers below (a) filter the composer dropdown to
# tool-capable models and (b) gate the tools= payload at the generation layer
# as defense in depth for chats whose model has since lost tool support.

# 60s amortises the per-model /api/show round trips across composer re-renders
# without making installed-model updates feel stale (a freshly pulled model
# surfaces within a minute).
_CAPABILITY_TTL_SECONDS = 60.0

# Module-level cache. Single-process uvicorn means one writer; the refresh
# runs on the event loop without external synchronisation. Shape when
# populated: {"expires_at": float (monotonic), "names": list[str]}.
_capability_cache: dict | None = None

# Sibling cache for the "thinking"-capable set, used to mark composer model
# options so the Think select can show/hide as the model changes. Same shape +
# TTL as ``_capability_cache``; separate so the two probes don't invalidate
# each other.
_thinking_cache: dict | None = None


async def list_tool_capable_models(
    client: httpx.AsyncClient, host: str | None = None
) -> list[str]:
    """Return installed models whose /api/show capabilities include 'tools'.

    Fans /api/show out over the installed models with ``asyncio.gather``, so a
    cold call against ~10 models lands in ~150ms instead of the ~500ms a
    sequential walk would cost. Results are cached for
    ``_CAPABILITY_TTL_SECONDS``; the next dropdown render is free.

    Args:
        client: An ``httpx.AsyncClient`` pointed at the Ollama host
            (typically from ``create_client``).
        host: Optional override base URL. ``None`` lists the primary host
            (cached). When set (the host picker), the probe targets that host
            and is NOT cached — the module cache is host-agnostic, and mixing
            a second host's models in would corrupt the primary dropdown.

    Returns:
        Tool-capable model names in /api/tags order. Models whose /api/show
        probe fails are silently dropped — better a slightly short list than
        failing the whole dropdown over one misbehaving model.

    Raises:
        OllamaUnavailable: /api/tags was unreachable or non-2xx (same contract
            as ``list_models``).
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
            resp = await client.post(
                _url("/api/show", host),
                json={"model": name},
                timeout=ollama_util_timeout(),
            )
            resp.raise_for_status()
            caps = resp.json().get("capabilities") or []
            # Require BOTH "completion" and "tools". Ollama occasionally
            # reports `["embedding", "tools"]` for derived models (e.g.
            # reranker spinoffs) that can't serve as chat models — /api/chat
            # to them gives garbage or a 400. Real chat-with-tools models
            # always advertise "completion"; embedders/rerankers don't. The
            # cheapest reliable filter short of a name-pattern denylist.
            return name if "completion" in caps and "tools" in caps else None
        except (
            httpx.HTTPError,
            httpx.InvalidURL,
            ValueError,
            KeyError,
            TypeError,
        ):
            # /api/show errored (HTTPError covers transport + status +
            # timeout) or the body was misshapen. Drop this model rather than
            # poisoning the result; the rest of the dropdown stays usable.
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

    Sibling to :func:`list_tool_capable_models`. The composer marks each model
    option with this set so the Think select can show/hide as the user changes
    models without a round trip. Same /api/show fan-out, TTL cache, and host
    semantics — ``host=None`` is cached (primary host); a non-primary ``host``
    bypasses the host-agnostic cache.

    Args:
        client: An ``httpx.AsyncClient`` pointed at the Ollama host.
        host: Optional override base URL. ``None`` lists the primary host
            (cached); when set, the probe targets that host and is NOT cached.

    Returns:
        Thinking-capable model names in /api/tags order. Probe failures are
        silently dropped (same policy as the tool-capable list).

    Raises:
        OllamaUnavailable: /api/tags was unreachable or non-2xx.
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
            resp = await client.post(
                _url("/api/show", host),
                json={"model": name},
                timeout=ollama_util_timeout(),
            )
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

    The dropdown filter is the primary defense, but a SQLite chat row pins the
    model the user picked at creation — if that model later loses tool support
    (Ollama upgrade, re-pull without the capability), we still need to avoid
    400ing the next message. Warms ``list_tool_capable_models`` and checks
    membership.

    Args:
        client: An ``httpx.AsyncClient`` pointed at the Ollama host.
        name: The model identifier to check (e.g. ``"llama3.1:8b"``).
        host: When set, probe this remote host with a single ``/api/show``
            instead of the local cache (per-process and host-agnostic; mixing
            remote and local entries would corrupt it). ``None`` keeps the
            cache-warmed local path.

    Returns:
        True if the cache lists ``name`` as tool-capable. False otherwise, or
        if the probe failed (better to skip ``tools=`` and degrade to plain
        chat than risk a 400).
    """
    if host is not None:
        # One-shot probe against the remote host. The cache stays local-only —
        # remote callers each pay one /api/show round-trip, the same cost the
        # local path pays on a cache miss.
        try:
            resp = await client.post(
                _url("/api/show", host),
                json={"model": name},
                timeout=ollama_util_timeout(),
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

    Gates the per-chat thinking toggle in the chat header — the control only
    renders for reasoning models. A single ``/api/show`` round-trip, mirroring
    :func:`model_supports_tools` but without the process cache: thinking-gating
    happens once per chat-panel render, the same cost and path as the
    ``is_model_loaded`` probe already there.

    Args:
        client: An ``httpx.AsyncClient`` pointed at the Ollama host.
        name: The model identifier to check (e.g. ``"qwen3.5:9b"``).
        host: Optional override base URL. ``None`` probes the primary host;
            set it to probe a non-primary host (the chat's selected host).

    Returns:
        True if ``/api/show`` lists ``"thinking"``. False on any failure
        (transport, status, or unexpected body) — better to hide the toggle
        than render it on a model that can't think.
    """
    try:
        resp = await client.post(
            _url("/api/show", host),
            json={"model": name},
            timeout=ollama_util_timeout(),
        )
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
# Memory residency (the "unload" chip)
# ---------------------------------------------------------------------------
#
# Ollama lazy-loads each model on first use and keeps it resident for ~5 min
# of idle (the default `keep_alive`). The header model chip exposes a manual
# unload to free VRAM without waiting for the idle timer, and `/api/ps` lets
# us colour the chip to reflect actual residency.


async def list_loaded_models(
    client: httpx.AsyncClient, host: str | None = None
) -> list[str]:
    """Return the names of models currently held in Ollama's memory.

    Wraps ``GET /api/ps`` — Ollama's "what's resident right now" endpoint.
    Distinct from :func:`list_models`, which reports every *installed* model
    regardless of memory state.

    Args:
        client: An ``httpx.AsyncClient`` pointed at the Ollama host
            (typically from ``create_client``).
        host: Optional override base URL. ``None`` queries the primary host;
            set it to probe a non-primary host's residency (the header chip
            targets the selected host).

    Returns:
        Loaded model names in Ollama's order; empty when nothing is loaded.

    Raises:
        OllamaUnavailable: Ollama unreachable, timed out, or non-2xx.
        OllamaProtocolError: Body wasn't valid JSON or had the wrong shape.
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

    Wraps :func:`list_loaded_models` and swallows Ollama errors — callers use
    this only to colour the header chip, so a transient /api/ps failure
    defaults to "looks loaded" rather than failing the whole chat-panel render.

    Args:
        client: An ``httpx.AsyncClient`` pointed at the Ollama host.
        name: The model identifier to check (e.g. ``"llama3.1:8b"``).
        host: Optional override base URL. ``None`` checks the primary host;
            set it so the chip reflects residency on the chat's selected host.

    Returns:
        True if /api/ps lists ``name``. False only when /api/ps succeeded AND
        ``name`` was absent. On any failure, True — better to show the chip
        loaded and let the next click correct it than to lie about residency.
    """
    try:
        return name in await list_loaded_models(client, host=host)
    except (OllamaUnavailable, OllamaProtocolError):
        return True


async def unload_model(
    client: httpx.AsyncClient, name: str, host: str | None = None
) -> None:
    """Ask Ollama to evict ``name`` from memory immediately.

    Ollama's unload protocol: POST ``/api/generate`` with ``keep_alive: 0``
    and no prompt. The server drops the model from VRAM/RAM and replies with a
    small JSON ack — the same mechanism as its idle eviction, on demand.
    Unloading an already-unloaded model is a no-op that still returns 200, so
    the caller needn't check residency first.

    Args:
        client: An ``httpx.AsyncClient`` pointed at the Ollama host.
        name: The model identifier to evict (e.g. ``"llama3.1:8b"``).
        host: Optional override base URL. ``None`` unloads from the primary
            host; set it to evict from the chat's selected host.

    Raises:
        OllamaUnavailable: Ollama unreachable, timed out, or non-2xx. The
            UI chip-flip is best-effort; the caller decides whether to surface
            this.
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
        model: Identifier of an installed model (e.g. ``"llama3:latest"``),
            one of the names ``list_models`` returns.
        messages: Conversation history, oldest first. Each item is a dict with
            ``"role"`` (``"user"`` or ``"assistant"``) and ``"content"``.
        temperature: Sampling temperature (0.0–2.0). Passed in Ollama's
            ``options``; its default is 0.8.
        think: When not ``None``, sets Ollama's ``think`` flag — ``False``
            suppresses a thinking model's reasoning phase (safe on any model),
            ``True`` requires a thinking-capable model (else Ollama 400s).
            ``None`` omits the key, leaving Ollama's default.
        num_ctx: When not ``None``, sets ``num_ctx`` (total context window in
            tokens — system + history + input + reply share this budget).
            ``None`` omits the key, leaving Ollama's default of 2048.

    Yields:
        One ``ChatChunk`` per NDJSON line. The final chunk has ``done=True``;
        earlier chunks carry incremental text in ``content``.

    Raises:
        OllamaUnavailable: Ollama unreachable, timed out mid-stream, or non-2xx.
        OllamaProtocolError: A stream line wasn't valid JSON.
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
            # NDJSON: one JSON object per line, each an incremental token
            # batch or the final `done` marker.
            async for line in response.aiter_lines():
                if not line.strip():
                    # Skip stray blank lines defensively — the protocol allows
                    # them even though Ollama doesn't emit them in practice.
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError as e:
                    # A garbled line is a protocol error, not transport.
                    # Raising breaks out of the generator; the outer httpx
                    # handler doesn't catch OllamaProtocolError, so it
                    # propagates cleanly.
                    raise OllamaProtocolError(
                        f"Ollama emitted a non-JSON line: {line!r}"
                    ) from e
                done = bool(data.get("done", False))
                # prompt_eval_count / eval_count are populated only on the
                # final chunk; pull them out for per-turn token counts. Ollama
                # omits them on full prompt-cache hits — treat missing as None,
                # not 0, so the UI can tell "no data" from "zero tokens".
                prompt_tokens = data.get("prompt_eval_count") if done else None
                eval_tokens = data.get("eval_count") if done else None
                message = data.get("message", {})
                yield ChatChunk(
                    content=message.get("content", ""),
                    # Reasoning rides a separate field; `or ""` collapses a
                    # null/absent value so consumers never see None.
                    thinking=message.get("thinking") or "",
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

    Used by the tool-calling loop: before opening a streaming response, ask
    Ollama in one shot whether the model wants a tool. If yes, the loop runs
    the tool then re-asks; if no, it falls through to ``stream_chat`` for the
    visible response.

    This costs an extra round-trip per "final answer" turn (one non-streaming
    call to detect intent, then a streaming one for the reply). Acceptable for
    a local app; revisit if the latency becomes annoying.

    Args:
        client: An ``httpx.AsyncClient`` pointed at the Ollama host.
        model: Identifier of an installed Ollama model.
        messages: Conversation history in Ollama wire format (already includes
            any tool_call / tool_result rows mapped by
            ``_build_history_payload``).
        tools: Tool specs to advertise (Ollama's ``tools=`` shape). Pass
            ``None`` to omit the key — required for non-tool models, where
            even ``tools=[]`` trips a 400.
        temperature: Sampling temperature (0.0–2.0). Passed in Ollama's
            ``options``; its default is 0.8.

    Returns:
        ``(tool_calls, content)``:
        - ``tool_calls``: ``{"name": str, "arguments": dict}`` dicts, unwrapped
          from Ollama's ``{"function": {...}}`` wire shape. Empty when no tool
          was requested.
        - ``content``: assistant text alongside the tool call (usually empty
          when ``tool_calls`` is non-empty). Discarded by the loop — the
          visible response comes from a later streaming call.

    Raises:
        OllamaUnavailable: Ollama unreachable, timed out, or non-2xx.
        OllamaProtocolError: Body wasn't valid JSON or had the wrong shape.
    """
    options: dict = {"temperature": temperature}
    if num_ctx is not None:
        options["num_ctx"] = num_ctx
    payload: dict = {
        "model": model,
        "messages": messages,
        # Non-streaming — the whole reply (or its tool_calls) comes back in
        # one JSON object rather than NDJSON.
        "stream": False,
        "options": options,
    }
    # Include `tools` only when there's something to advertise — some models
    # 400 on an empty list, so the caller gates by passing None.
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
        # `message` may be absent if an error-shaped body slips past
        # raise_for_status; defaulting to {} keeps the .get chains honest.
        message = body.get("message", {})
        raw_calls = message.get("tool_calls") or []
        content = message.get("content") or ""
        # Ollama wraps each call as {"function": {"name", "arguments"}}, but
        # the rest of the loop (run_tool, history persistence) wants the
        # flatter {"name", "arguments"}. Unwrap here so the wire format lives
        # only at this boundary.
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

    Powers the manual ``Compact`` action. Single-shot, non-streaming POST to
    ``/api/chat``. Uses the chat's own model — already warm from the previous
    turn, so the round-trip is cheap and no second model goes resident.

    Unlike :func:`generate_title`, the output is returned with only whitespace
    stripping — no quote unwrapping, word cap, or preamble heuristics. The
    prompt is explicit enough that smaller models reply cleanly; if output
    ever needs sanitizing, do it in the caller so this stays a thin wrapper.

    Args:
        client: Async ``httpx.AsyncClient`` pointed at the Ollama host.
        model: Identifier of an installed model. Pass the conversation's own
            model so the summarizer reuses the warm KV cache.
        history: Conversation rows in Ollama wire format (already mapped by
            :func:`app.generation.build_history_payload`). Not filtered here —
            pass exactly the rows you want summarized.
        num_ctx: Per-project context-window override, mirroring
            :func:`stream_chat`. ``None`` omits the key (Ollama default).

    Returns:
        The stripped summary text. The caller treats an empty string as
        "skip — don't archive anything".

    Raises:
        OllamaUnavailable: Ollama unreachable, timed out, or non-2xx.
        OllamaProtocolError: Body wasn't valid JSON or had the wrong shape.
    """
    # Delivered as a final user turn, mirroring generate_title — small local
    # models follow a verb-first user instruction more reliably than a
    # `system` directive.
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
    # Hardcoded (not the chat's temperature) so a creatively-tuned chat
    # doesn't get a creatively-summarized history.
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
        # Matches stream_chat's read timeout (OLLAMA_CHAT_TIMEOUT, default
        # 600s). The earlier 120s assumed a warm model, but by the time a user
        # clicks Compact the model has usually idled out of memory (Ollama's
        # default keep_alive is ~5 min), so this is a COLD load: weights load +
        # the whole conversation prefills before the first token. A 9b model at
        # num_ctx=32768 alone measured ~112s cold — under any extra load that
        # blew past 120s and surfaced as a spurious 503. Generation gets the
        # full chat timeout; compaction shares it.
        response = await client.post(
            _url("/api/chat", host), json=payload, timeout=ollama_chat_timeout()
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        # Ollama answered with a non-2xx; its body carries the real reason
        # (e.g. model-not-found, context too large). Surface it so the error
        # the UI shows is actionable rather than a bare status line.
        detail = (e.response.text or "").strip()
        raise OllamaUnavailable(
            f"Compaction request failed: {e}"
            + (f" — {detail}" if detail else "")
        ) from e
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
    host: str | None = None,
    num_ctx: int | None = None,
) -> str:
    """Ask the chat's own model to summarize the conversation as a title.

    Single-shot, non-streaming POST to ``/api/chat``. Reuses the conversation's
    model — warm from the reply we just streamed, so the roundtrip is fast and
    no second model goes resident.

    Args:
        client: An ``httpx.AsyncClient`` pointed at the Ollama host.
        model: The model identifier to use. Callers pass the conversation's
            own model; the chat just used it, so it's installed and warm.
        history: The conversation so far — the same wire-format list
            ``stream_chat`` accepts. A title-request user turn is appended
            here; callers needn't.
        host: Optional override base URL — the chat's selected Ollama host.
            Must match the host that just streamed the reply, or the title
            request lands on a *different* machine where the model isn't
            resident (a cold load, often past the timeout). ``None`` falls
            through to the client's ``base_url`` (the primary host).
        num_ctx: ``num_ctx`` to request, matching the turn that just streamed.
            Ollama keys a resident model on its load params, so passing the
            SAME value the stream used keeps this title call on the warm
            instance instead of forcing a reload at a different context size.
            ``None`` omits the key (Ollama default).

    Returns:
        The generated title, stripped of surrounding quotes and known
        preambles. May be empty if the model misbehaves; the caller treats
        empty as "skip the rename".

    Raises:
        OllamaUnavailable: Ollama unreachable, timed out, or non-2xx.
        OllamaProtocolError: JSON we couldn't parse into the expected
            ``{"message": {"content": ...}}`` shape.
    """
    # Delivered as a final user turn so the model treats it as the current
    # request, not a system directive (small chat models often ignore
    # `system`). Verb-first ("Title this...") behaves better than
    # "Summarize..." — smaller models parse the latter literally and echo the
    # constraint phrasing.
    title_request = {
        "role": "user",
        "content": (
            "Give this conversation a noun-phrase title:"
            " 7 words or fewer, under 50 characters."
            " Reply with only the title, no punctuation."
        ),
    }
    # Match the streamed turn's num_ctx so Ollama reuses the warm instance
    # rather than reloading the model at a different context size.
    options: dict = {}
    if num_ctx is not None:
        options["num_ctx"] = num_ctx
    payload: dict = {
        "model": model,
        "messages": [*history, title_request],
        "stream": False,
        # Force thinking OFF regardless of the chat's think_mode. A title is a
        # few tokens; a reasoning model left to "think" first burns its budget
        # on a hidden reasoning phase and blows the cap below — silently
        # skipping the rename. ``think: false`` is safe on any model
        # (non-thinking ones ignore it), so we send it unconditionally.
        "think": False,
    }
    if options:
        payload["options"] = options

    try:
        # Util cap (OLLAMA_UTIL_TIMEOUT, default 30s). The model is resident
        # (we just streamed the reply), and the caller feeds only the opening
        # exchange, so a few-token title returns quickly once any prefill is
        # done. The headroom over a sub-second warm call covers a cold reload
        # (10-30s for the weights, though the chat we just answered should keep
        # them resident). The cap bounds how long the connection stays open if
        # Ollama wedges. On expiry httpx raises ReadTimeout (an httpx.HTTPError),
        # which the caller catches as OllamaUnavailable → silent skip in
        # _maybe_emit_title.
        response = await client.post(
            _url("/api/chat", host), json=payload, timeout=ollama_util_timeout()
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

    # Defensive post-processing: tinyllama tends to wrap titles in quotes
    # despite the instruction, add a trailing period, and occasionally emit a
    # multi-line "reasoning" preamble. Strip those so a runaway response can't
    # become a giant sidebar row.
    text = text.strip()

    # First non-empty line — guards against "Here is your title:\n\nFoo Bar".
    for line in text.splitlines():
        if line.strip():
            text = line.strip()
            break

    # Strip common surrounding quote characters.
    text = text.strip(' "“”‘’\'.')

    # Strip preambles tinyllama adds despite the instructions, checked
    # case-insensitively at the start. Order doesn't matter — only one match
    # is stripped per run.
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
