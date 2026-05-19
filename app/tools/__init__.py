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
import re
import typing
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


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
    """
    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.parameters_schema,
            },
        }
        for spec in TOOLS.values()
    ]


async def run_tool(name: str, args: dict) -> str:
    """Look up a tool by name and call it with the given args.

    Args:
        name: The tool's registered name (from `@tool`).
        args: The argument dict the model sent in its tool_call.

    Returns:
        Whatever the tool returns, stringified. Returns an
        explanatory error string for unknown tools or argument
        mismatches — never raises (the caller stores the result
        verbatim as a tool_result message; raising would break
        the SSE stream).
    """
    spec = TOOLS.get(name)
    if spec is None:
        return f"Tool '{name}' is not registered."
    try:
        result = spec.func(**args)
        # Unify sync + async callables: if the function returned a
        # coroutine/awaitable, await it; otherwise use the value as-is.
        if inspect.isawaitable(result):
            result = await result
        return str(result)
    except TypeError as e:
        # Argument mismatch — the model passed wrong kwargs.
        return f"Tool '{name}' rejected arguments: {e}"
    except Exception as e:
        # Tool itself raised; surface to the model but don't crash.
        return f"Tool '{name}' failed: {e}"


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
    args_str = ", ".join(f"{k}={v!r}" for k, v in arguments.items())
    return f"calling {name}({args_str})"
