"""Phase 5: async HTTP client wrapping the local Ollama server.

Two operations, both async:

- ``list_models`` — ``GET /api/tags``, returns model names for the sidebar
  dropdown.
- ``stream_chat`` — ``POST /api/chat`` with ``stream=true``, yields
  ``ChatChunk`` objects as Ollama emits them (NDJSON, one JSON object per
  line).

Two failure classes are surfaced so the UI can present different errors:

- ``OllamaUnavailable`` — transport problems: connect refused, timeout,
  5xx, malformed URL. "Ollama isn't running."
- ``OllamaProtocolError`` — Ollama responded, but with something we
  can't parse: invalid JSON or an unexpected response shape. "Ollama
  returned something I don't understand."
"""

import json
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
    """

    content: str
    done: bool


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
        timeout=httpx.Timeout(120.0, connect=5.0),
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


async def stream_chat(
    client: httpx.AsyncClient,
    model: str,
    messages: list[dict[str, str]],
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
    payload = {"model": model, "messages": messages, "stream": True}
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
                yield ChatChunk(
                    content=data.get("message", {}).get("content", ""),
                    done=bool(data.get("done", False)),
                )
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        raise OllamaUnavailable(f"Ollama stream failed: {e}") from e


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
        The model-generated title, stripped of surrounding quotes and
        truncated to one line and 80 characters. Empty strings are
        possible if the model misbehaves; the caller is expected to
        treat empty as "skip the rename".

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
            "Title this conversation in 3 to 6 words."
            " Reply with only the title."
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

    return text[:80]
