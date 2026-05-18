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

    Returns:
        A freshly built ``httpx.AsyncClient`` with ``base_url`` set so
        the rest of the module can use relative paths like
        ``/api/tags``.

    Raises:
        KeyError: If ``OLLAMA_HOST`` is not set in ``.env`` or the
            environment.
    """
    return httpx.AsyncClient(base_url=ollama_host())


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
