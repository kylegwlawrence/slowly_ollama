"""Phase 12b: tool decorator + registry tests."""

import pytest

from app.tools import TOOLS, run_tool, tool, tool_specs_for_ollama


def test_decorator_registers_with_inferred_schema() -> None:
    """@tool extracts name, description, and arg schema from the function."""
    @tool
    def add(x: int, y: int = 1) -> int:
        """Add two integers.

        Args:
            x: The first integer.
            y: The second integer (defaults to 1).
        """
        return x + y

    spec = TOOLS["add"]
    assert spec.name == "add"
    assert spec.description == "Add two integers."
    props = spec.parameters_schema["properties"]
    assert props["x"]["type"] == "integer"
    assert props["x"]["description"] == "The first integer."
    assert props["y"]["type"] == "integer"
    # Only x is required; y has a default.
    assert spec.parameters_schema["required"] == ["x"]
    # Read-only by default.
    assert spec.is_read_only is True


def test_decorator_handles_no_docstring() -> None:
    """A tool with no docstring still registers (description = name)."""
    @tool
    def noop() -> str:
        return ""

    assert TOOLS["noop"].description == "noop"
    assert TOOLS["noop"].parameters_schema["required"] == []


def test_tool_specs_for_ollama_shape() -> None:
    """The shape matches Ollama's /api/chat tools parameter format."""
    @tool
    def greet(name: str) -> str:
        """Say hi."""
        return f"hi {name}"

    specs = tool_specs_for_ollama()
    # Other tests register their own tools; pick out greet by name rather
    # than asserting on the full list (the registry is module-level).
    greet_spec = next(s for s in specs if s["function"]["name"] == "greet")
    assert greet_spec == {
        "type": "function",
        "function": {
            "name": "greet",
            "description": "Say hi.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    }


@pytest.mark.asyncio
async def test_run_tool_dispatches_sync_function() -> None:
    @tool
    def double(n: int) -> int:
        """Double n."""
        return n * 2

    assert await run_tool("double", {"n": 21}) == "42"


@pytest.mark.asyncio
async def test_run_tool_unknown_returns_error_string() -> None:
    result = await run_tool("nonexistent", {})
    assert "not registered" in result


@pytest.mark.asyncio
async def test_run_tool_arg_mismatch_returns_error_string() -> None:
    @tool
    def need_x(x: int) -> int:
        """Need x."""
        return x

    result = await run_tool("need_x", {"wrong_name": 1})
    # Returns explanatory string, doesn't raise.
    assert "rejected arguments" in result


@pytest.mark.asyncio
async def test_current_time_baseline() -> None:
    """The baseline tool runs and returns an ISO 8601 string."""
    from app.tools import builtins  # noqa: F401 — registers current_time
    result = await run_tool("current_time", {"timezone": "UTC"})
    # ISO format starts with YYYY-
    assert result[:4].isdigit()
    assert result[4] == "-"
