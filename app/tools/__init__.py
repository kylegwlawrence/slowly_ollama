"""Phase 12: tool definitions and registry for tool-calling.

A `@tool` decorator turns a Python function into a tool the chat
model can call:
- The function's name becomes the tool's name.
- The first line of its docstring becomes the tool description.
- Its type hints become the tool's argument JSON schema.
- An `is_read_only` flag (default True) determines whether tool
  execution requires user confirmation (phase 12 has no
  confirm-needed tools yet; the flag is forward-looking).

The registry (`TOOLS`) is a module-level dict the routes layer
queries via `tool_specs_for_ollama()` (formats for Ollama's
/api/chat tools=[...] parameter) and `run_tool()` (dispatches a
named tool call to its function).
"""

import inspect
import json
import re
import typing
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Source:
    """One retrieved chunk's UI-facing metadata.

    Attributes:
        title: Document title. The caller normalizes a missing value to
            ``"(untitled)"`` before constructing; this field is never
            None at the storage boundary.
        section: Optional section heading. ``None`` when absent — the
            UI omits the ``"(§Section)"`` suffix in that case.
    """

    title: str
    section: str | None


@dataclass(frozen=True)
class ToolResult:
    """Structured return value for tools that surface sources to the UI.

    The chat model only ever sees ``.text`` — sources are a UI-only
    concern, carried alongside so the tool card can render the
    retrieved titles without re-parsing the formatted citation block.
    Plain-string returns from tools are wrapped by :func:`run_tool`
    so the rest of the system handles a single shape.

    Attributes:
        text: What the model sees as the tool's output. For
            ``query_rag`` this is the formatted citation block.
        sources: Zero-or-more entries used to render the expandable
            sub-list. Empty for non-source tools (e.g. ``current_time``).
    """

    text: str
    sources: list[Source] = field(default_factory=list)


def encode_tool_result(result: ToolResult) -> str:
    """Serialize a :class:`ToolResult` for storage in ``messages.content``.

    The envelope is ``{"text": ..., "sources": [...]}`` — paired with
    :func:`decode_tool_result` on the read side. Sources are emitted
    even when empty so the on-disk shape stays uniform across rows.

    Args:
        result: The tool's return value.

    Returns:
        A JSON string suitable for the messages table's ``content``
        column.
    """
    return json.dumps({
        "text": result.text,
        "sources": [
            {"title": s.title, "section": s.section} for s in result.sources
        ],
    })


def decode_tool_result(content: str) -> ToolResult:
    """Inverse of :func:`encode_tool_result`, with plain-text fallback.

    Pre-12h DB rows store plain text (the formatted citation block).
    Any non-JSON content, or JSON without the envelope keys, decodes
    to ``ToolResult(text=content, sources=[])`` so old conversations
    still render unchanged.

    Args:
        content: The raw ``messages.content`` string for a
            ``tool_result`` row.

    Returns:
        A ``ToolResult`` — never raises. Sources is ``[]`` for legacy
        rows and for malformed envelopes.
    """
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError, ValueError):
        return ToolResult(text=content, sources=[])
    if not isinstance(payload, dict) or "text" not in payload:
        return ToolResult(text=content, sources=[])
    raw = payload.get("sources") or []
    sources = [
        Source(
            title=s.get("title", "(untitled)"),
            section=s.get("section"),
        )
        for s in raw
        if isinstance(s, dict)
    ]
    return ToolResult(text=payload["text"], sources=sources)


def encode_tool_call(name: str, arguments: dict) -> str:
    """Serialize a tool call for storage in ``messages.content``.

    Paired with :func:`decode_tool_call`. The shape is the same as the
    JSON the chat model emits in its ``tool_calls`` list, flattened to
    ``{"name": ..., "arguments": ...}`` (Ollama wraps each call as
    ``{"function": {"name", "arguments"}}`` on the wire; the wrapper is
    unwrapped at the boundary in ``ollama.maybe_tool_call``).

    Args:
        name: The tool's registered name.
        arguments: The argument dict the model sent.

    Returns:
        A JSON string suitable for the messages table's ``content``
        column.
    """
    return json.dumps({"name": name, "arguments": arguments})


def decode_tool_call(content: str) -> tuple[str, dict] | None:
    """Inverse of :func:`encode_tool_call`, returning ``None`` on corrupt rows.

    Distinguishes "decoded successfully" from "row is corrupt" so
    callers can pick their recovery strategy:

    - ``app/generation.py:_build_history_payload`` uses ``None`` as the
      signal to drop the row AND its paired tool_result (otherwise the
      orphan result would 400 Ollama).
    - ``app/render.py:_row_view_from_pair`` falls back to
      ``("?", {})`` for display so a corrupt row still renders a
      placeholder card.

    Args:
        content: The raw ``messages.content`` string for a
            ``tool_call`` row.

    Returns:
        ``(name, arguments)`` tuple on success; ``None`` when the
        content is not valid JSON, isn't a dict, or has no ``name``
        key. Never raises.
    """
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or "name" not in payload:
        return None
    arguments = payload.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}
    return payload["name"], arguments


@dataclass(frozen=True)
class ToolSpec:
    """A registered tool's metadata + the callable that runs it.

    Attributes:
        name: Tool name as the model sees it (the function's Python name).
        description: First line of the function's docstring.
        parameters_schema: JSON schema for the function's arguments
            (Ollama "tools" parameter format). Kept as a plain mutable
            dict so phase 12c can refresh dynamic fields (e.g. enum
            descriptions sourced from the RAG-servers list) without
            re-registering the tool.
        is_read_only: When True, the route layer auto-executes the
            tool. When False, the route should require user
            confirmation (no such tools exist in phase 12 yet).
        func: The actual callable. May be sync or async; `run_tool`
            awaits async functions and calls sync ones directly.
    """
    name: str
    description: str
    parameters_schema: dict
    is_read_only: bool
    func: Callable[..., object] | Callable[..., Awaitable[object]]


# Module-level registry. Populated as a side-effect when @tool-decorated
# functions are imported. The routes layer reads it via the helpers below.
TOOLS: dict[str, ToolSpec] = {}

# Canonical name for the RAG query tool. Referenced in routes (chip
# filtering) and generation (source-description patching) — one place to
# update if the function is ever renamed.
RAG_TOOL_NAME = "query_rag"


# Maps Python types to JSON schema types. Anything not in this map
# defaults to "string" — the model will pass strings and the
# function's type coercion handles the rest.
_TYPE_TO_JSON_SCHEMA = {
    str: {"type": "string"},
    int: {"type": "integer"},
    float: {"type": "number"},
    bool: {"type": "boolean"},
}


def _parse_arg_descriptions(docstring: str) -> dict[str, str]:
    """Pull `Args:` block lines from a Google-style docstring.

    Returns a {arg_name: description} dict. Tolerates missing or
    malformed Args blocks (returns {}). Doesn't try to handle
    multi-line arg descriptions — first line wins.
    """
    if not docstring:
        return {}
    lines = docstring.splitlines()
    in_args = False
    out: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("Args:"):
            in_args = True
            continue
        if in_args:
            # Args section ends at the next blank line or section header.
            if not stripped or stripped.endswith(":"):
                in_args = False
                continue
            # Expected form: "arg_name: description text"
            m = re.match(r"^(\w+):\s*(.+)$", stripped)
            if m:
                out[m.group(1)] = m.group(2)
    return out


def tool(
    func: Callable | None = None,
    *,
    is_read_only: bool = True,
) -> Callable:
    """Decorate a function to register it as a callable tool.

    Usage::

        @tool
        def current_time(timezone: str = "UTC") -> str:
            \"\"\"Get the current time.

            Args:
                timezone: IANA timezone name like "America/Vancouver".
            \"\"\"
            ...

    Args:
        func: The function being decorated (when used without arguments).
        is_read_only: When True, the route layer auto-executes. When
            False, user confirmation is required (phase 12 has no such
            tools yet).

    Returns:
        The original function, unmodified. Registration is a side
        effect — call sites use the function normally; the model
        sees it via TOOLS / tool_specs_for_ollama().
    """
    def _decorate(fn: Callable) -> Callable:
        name = fn.__name__
        doc = inspect.getdoc(fn) or ""
        # First line of the docstring = tool description shown to the model.
        # Fall back to the function name so the model always sees something.
        description = doc.split("\n", 1)[0].strip() or name
        arg_descriptions = _parse_arg_descriptions(doc)

        # get_type_hints resolves string-form annotations (PEP 563/649) and
        # follows the function's __globals__, so this works with `from
        # __future__ import annotations` and `X | None` syntax alike.
        hints = typing.get_type_hints(fn)
        sig = inspect.signature(fn)

        properties: dict[str, dict] = {}
        required: list[str] = []
        for param_name, param in sig.parameters.items():
            if param_name == "self":
                continue
            py_type = hints.get(param_name, str)
            # Strip Optional[X] / X | None down to X for schema purposes.
            # Both forms collapse to `Union[X, None]` at the typing level,
            # which has Union as the origin and includes NoneType in args.
            origin = typing.get_origin(py_type)
            if origin is typing.Union or (origin is not None and type(None) in typing.get_args(py_type)):
                non_none = [a for a in typing.get_args(py_type) if a is not type(None)]
                py_type = non_none[0] if non_none else str
            # dict(...) copies so each tool gets its own schema dict (avoids
            # accidental cross-tool mutation of the shared template).
            schema = dict(_TYPE_TO_JSON_SCHEMA.get(py_type, {"type": "string"}))
            if param_name in arg_descriptions:
                schema["description"] = arg_descriptions[param_name]
            properties[param_name] = schema
            if param.default is inspect.Parameter.empty:
                required.append(param_name)

        parameters_schema = {
            "type": "object",
            "properties": properties,
            "required": required,
        }

        spec = ToolSpec(
            name=name,
            description=description,
            parameters_schema=parameters_schema,
            is_read_only=is_read_only,
            func=fn,
        )
        TOOLS[name] = spec
        return fn

    # Allow both `@tool` and `@tool(is_read_only=False)` forms. When used
    # bare (`@tool`), `func` is the function being decorated; with kwargs
    # (`@tool(is_read_only=False)`) the outer call returns the decorator.
    if func is None:
        return _decorate
    return _decorate(func)


def tool_specs_for_ollama() -> list[dict]:
    """Format every registered tool as an entry in Ollama's
    /api/chat `tools` parameter.

    Returns a list of dicts shaped like::

        {"type": "function",
         "function": {"name": "...", "description": "...",
                      "parameters": {... JSON schema ...}}}

    The ``parameters`` dict is a deep copy of the registered tool's
    ``parameters_schema`` so per-turn callers can patch dynamic fields
    (e.g. ``query_rag.source.description`` in the generation layer)
    without mutating the shared registry entry and leaking the patch
    into the next chat's spec list.
    """
    import copy as _copy

    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": _copy.deepcopy(spec.parameters_schema),
            },
        }
        for spec in TOOLS.values()
    ]


async def run_tool(name: str, args: dict) -> ToolResult:
    """Look up a tool by name and call it with the given args.

    Always returns a :class:`ToolResult`. Tools that return plain
    strings (or anything not already a ``ToolResult``) are wrapped
    here so the caller — the generation loop — handles exactly one
    shape regardless of the tool's signature.

    Args:
        name: The tool's registered name (from ``@tool``).
        args: The argument dict the model sent in its tool_call.

    Returns:
        A :class:`ToolResult`. Errors (unknown tool, argument
        mismatch, exception from inside the tool) come back as
        ``ToolResult(text="...", sources=[])`` so the caller can
        persist + feed the explanation back to the model without
        special-casing. Never raises.
    """
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


def format_tool_invocation(name: str, arguments: dict) -> str:
    """Render a tool call as a human-readable one-liner for the UI card.

    The aggregated tool-card (phase 12e) shows one row per call.
    `query_rag` is the dominant search-shaped tool, so it gets a
    purpose-built label; every other tool falls back to a generic
    `calling name(args)` shape. Future tools that want nicer labels
    add a branch here rather than threading a display field through
    ToolSpec.

    Args:
        name: The tool's registered name.
        arguments: The arguments dict from the model's tool_call.

    Returns:
        A single line of plain text. Caller is responsible for HTML
        escaping (Jinja autoescape handles this when the string is
        interpolated into a template).
    """
    if name == "query_rag":
        source = arguments.get("source", "?")
        query = arguments.get("query", "")
        return f'searching {source}: "{query}"'
    if name == "search_files":
        pattern = arguments.get("pattern", "*")
        path = arguments.get("path", ".")
        return f'searching {path} for "{pattern}"'
    args_str = ", ".join(f"{k}={v!r}" for k, v in arguments.items())
    return f"calling {name}({args_str})"
