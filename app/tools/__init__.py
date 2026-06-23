"""Tool definitions and registry for tool-calling.

The `@tool` decorator turns a Python function into a tool the chat model
can call: the function name becomes the tool name, the first docstring
line becomes the description, and type hints become the argument JSON
schema.

The registry (`TOOLS`) is a module-level dict the routes layer reads via
`tool_specs_for_ollama()` (formats for Ollama's /api/chat `tools` param)
and `run_tool()` (dispatches a named call to its function).
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
        title: Document title. Never None — the caller normalizes a missing
            value to ``"(untitled)"`` before constructing.
        section: Optional section heading. ``None`` omits the
            ``"(§Section)"`` suffix in the UI.
    """

    title: str
    section: str | None


@dataclass(frozen=True)
class ToolResult:
    """Structured return value for tools that surface sources to the UI.

    The model only sees ``.text``; sources are UI-only, carried alongside
    so the tool card can render retrieved titles without re-parsing the
    citation block. :func:`run_tool` wraps plain-string returns so the rest
    of the system handles a single shape.

    Attributes:
        text: What the model sees as output. For ``query_rag``, the
            formatted citation block.
        sources: Entries for the expandable sub-list. Empty for non-source
            tools (e.g. ``current_time``).
    """

    text: str
    sources: list[Source] = field(default_factory=list)


def encode_tool_result(result: ToolResult) -> str:
    """Serialize a :class:`ToolResult` for storage in ``messages.content``.

    The envelope is ``{"text": ..., "sources": [...]}`` — paired with
    :func:`decode_tool_result`. Sources are emitted even when empty so the
    on-disk shape stays uniform.

    Args:
        result: The tool's return value.

    Returns:
        A JSON string for the messages table's ``content`` column.
    """
    return json.dumps({
        "text": result.text,
        "sources": [
            {"title": s.title, "section": s.section} for s in result.sources
        ],
    })


def decode_tool_result(content: str) -> ToolResult:
    """Inverse of :func:`encode_tool_result`, with plain-text fallback.

    Legacy rows store plain text (the citation block). Non-JSON content, or
    JSON without the envelope keys, decodes to
    ``ToolResult(text=content, sources=[])`` so old conversations render
    unchanged.

    Args:
        content: The raw ``messages.content`` for a ``tool_result`` row.

    Returns:
        A ``ToolResult`` — never raises. Sources is ``[]`` for legacy rows
        and malformed envelopes.
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

    Paired with :func:`decode_tool_call`. Flattened to
    ``{"name": ..., "arguments": ...}`` (Ollama wraps each call as
    ``{"function": {...}}`` on the wire; unwrapped in
    ``ollama.maybe_tool_call``).

    Args:
        name: The tool's registered name.
        arguments: The argument dict the model sent.

    Returns:
        A JSON string for the messages table's ``content`` column.
    """
    return json.dumps({"name": name, "arguments": arguments})


def decode_tool_call(content: str) -> tuple[str, dict] | None:
    """Inverse of :func:`encode_tool_call`, returning ``None`` on corrupt rows.

    Distinguishes "decoded" from "corrupt" so callers can choose recovery:

    - ``generation._build_history_payload`` drops a ``None`` row AND its
      paired tool_result (an orphan result would 400 Ollama).
    - ``render._row_view_from_pair`` falls back to ``("?", {})`` so a
      corrupt row still renders a placeholder card.

    Args:
        content: The raw ``messages.content`` for a ``tool_call`` row.

    Returns:
        ``(name, arguments)`` on success; ``None`` when the content isn't
        valid JSON, isn't a dict, or lacks a ``name`` key. Never raises.
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
        parameters_schema: JSON schema for the arguments (Ollama "tools"
            format). A plain mutable dict so callers can refresh dynamic
            fields (e.g. enum descriptions from the RAG-servers list)
            without re-registering.
        func: The callable. Sync or async; `run_tool` awaits async ones
            and calls sync ones directly.
    """
    name: str
    description: str
    parameters_schema: dict
    func: Callable[..., object] | Callable[..., Awaitable[object]]


# Module-level registry. Populated as a side-effect when @tool-decorated
# functions are imported. The routes layer reads it via the helpers below.
TOOLS: dict[str, ToolSpec] = {}

# Canonical name for the RAG query tool. Referenced in routes (chip
# filtering) and generation (source-description patching) — one place to
# update if the function is renamed.
RAG_TOOL_NAME = "query_rag"


# Python type → JSON schema type. Anything unmapped defaults to "string";
# the function's own coercion handles the rest.
_TYPE_TO_JSON_SCHEMA = {
    str: {"type": "string"},
    int: {"type": "integer"},
    float: {"type": "number"},
    bool: {"type": "boolean"},
}


def _parse_arg_descriptions(docstring: str) -> dict[str, str]:
    """Pull `Args:` block lines from a Google-style docstring.

    Returns a {arg_name: description} dict; tolerates missing/malformed
    Args blocks (returns {}). Multi-line arg descriptions: first line wins.
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


def tool(fn: Callable) -> Callable:
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
        fn: The function being decorated.

    Returns:
        The original function, unmodified. Registration is a side effect —
        call sites use the function normally; the model sees it via TOOLS /
        tool_specs_for_ollama().
    """
    name = fn.__name__
    doc = inspect.getdoc(fn) or ""
    # First docstring line = the tool description shown to the model;
    # fall back to the function name so the model always sees something.
    description = doc.split("\n", 1)[0].strip() or name
    arg_descriptions = _parse_arg_descriptions(doc)

    # get_type_hints resolves string-form annotations and follows the
    # function's __globals__, so this handles `from __future__ import
    # annotations` and `X | None` syntax alike.
    hints = typing.get_type_hints(fn)
    sig = inspect.signature(fn)

    properties: dict[str, dict] = {}
    required: list[str] = []
    for param_name, param in sig.parameters.items():
        if param_name == "self":
            continue
        py_type = hints.get(param_name, str)
        # Strip Optional[X] / X | None down to X — both collapse to
        # Union[X, None], with Union as origin and NoneType in the args.
        origin = typing.get_origin(py_type)
        if origin is typing.Union or (origin is not None and type(None) in typing.get_args(py_type)):
            non_none = [a for a in typing.get_args(py_type) if a is not type(None)]
            py_type = non_none[0] if non_none else str
        # Copy so each tool gets its own schema dict (no cross-tool
        # mutation of the shared template).
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

    TOOLS[name] = ToolSpec(
        name=name,
        description=description,
        parameters_schema=parameters_schema,
        func=fn,
    )
    return fn


def tool_specs_for_ollama() -> list[dict]:
    """Format every registered tool for Ollama's /api/chat `tools` param.

    Returns a list of dicts shaped like::

        {"type": "function",
         "function": {"name": "...", "description": "...",
                      "parameters": {... JSON schema ...}}}

    ``parameters`` is a deep copy of the registered ``parameters_schema`` so
    per-turn callers can patch dynamic fields (e.g.
    ``query_rag.source.description``) without leaking the patch into the
    next chat's spec list.
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

    Always returns a :class:`ToolResult` — non-``ToolResult`` returns are
    wrapped here so the generation loop handles exactly one shape.

    Args:
        name: The tool's registered name (from ``@tool``).
        args: The argument dict the model sent in its tool_call.

    Returns:
        A :class:`ToolResult`. Errors (unknown tool, argument mismatch,
        exception inside the tool) come back as ``ToolResult(text="...")``
        so the caller can feed the explanation back to the model. Never
        raises.
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

    The tool card shows one row per call. Common tools get purpose-built
    labels; everything else falls back to a generic `calling name(args)`.
    Add a branch here for nicer labels rather than threading a display
    field through ToolSpec.

    Args:
        name: The tool's registered name.
        arguments: The arguments dict from the model's tool_call.

    Returns:
        A single line of plain text. The caller handles HTML escaping
        (Jinja autoescape on template interpolation).
    """
    if name == "query_rag":
        source = arguments.get("source", "?")
        query = arguments.get("query", "")
        return f'searching {source}: "{query}"'
    if name == "search_files":
        pattern = arguments.get("pattern", "*")
        path = arguments.get("path", ".")
        return f'searching {path} for "{pattern}"'
    if name == "fetch_github_file":
        url = arguments.get("url", "?")
        return f"fetching {url}"
    if name == "write_file":
        path = arguments.get("path", "?")
        return f"writing {path}"
    args_str = ", ".join(f"{k}={v!r}" for k, v in arguments.items())
    return f"calling {name}({args_str})"
