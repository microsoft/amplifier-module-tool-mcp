"""Tests for amplifier_module_tool_mcp.discovery.

Regression coverage for two BLOCKER defects found by independent review:

- BLOCKER 1 (pagination.py): a page object carrying neither `next_cursor`
  nor `nextCursor` is an unrecognized SDK shape and must raise -- not be
  silently treated as the terminal page (which would truncate the result
  set with zero signal).
- BLOCKER 2 (discovery.py): `discover_resources`/`discover_prompts` used to
  catch bare `Exception`, which also swallowed pagination's deliberately
  loud `RuntimeError` safety nets (repeated cursor, max-pages-exceeded).
  A misbehaving resources/prompts server would silently report `[]`
  instead of raising. The fix narrows the catch to the SDK's protocol-error
  class, and further to method-not-found (-32601) specifically, so a
  genuinely unsupported capability still yields `[]` quietly while any
  other failure -- including pagination's guards -- propagates.

These tests must fail against the pre-fix code and pass after it; that was
verified by running this file both before and after applying the fix.
"""

from dataclasses import dataclass, field
from typing import Any

import pytest
from mcp.types import ErrorData

from amplifier_module_tool_mcp.discovery import (
    discover_prompts,
    discover_resources,
    discover_tools,
)
from amplifier_module_tool_mcp.sdk_compat import MCP_ERROR_CLASS

# `MCP_ERROR_CLASS` is typed as `type[Exception]` in sdk_compat.py; its
# constructor genuinely differs across SDK majors, so it's used through an
# `Any`-typed alias here (same pattern as tests/test_sdk_compat.py).
_ErrorClass: Any = MCP_ERROR_CLASS


def _make_mcp_error(code: int, message: str = "error") -> Exception:
    """Construct an instance of the installed SDK's protocol-error class.

    `mcp` 2.x: `MCPError(code, message, data=None)`.
    `mcp` 1.x: `McpError(error: ErrorData)`.
    Mirrors the helper in tests/test_sdk_compat.py.
    """
    try:
        return _ErrorClass(code=code, message=message, data=None)  # mcp>=2.0
    except TypeError:
        return _ErrorClass(ErrorData(code=code, message=message, data=None))  # mcp<2.0


@dataclass
class _FakeTool:
    name: str
    description: str = ""
    input_schema: dict = field(default_factory=dict)


@dataclass
class _FakeResource:
    name: str
    uri: str = "file:///fake"
    description: str = ""
    mime_type: str | None = None


@dataclass
class _FakePrompt:
    name: str
    description: str = ""
    arguments: list = field(default_factory=list)


@dataclass
class _FakeToolsResult:
    tools: list[Any] = field(default_factory=list)
    next_cursor: str | None = None


@dataclass
class _FakeResourcesResult:
    resources: list[Any] = field(default_factory=list)
    next_cursor: str | None = None


@dataclass
class _FakePromptsResult:
    prompts: list[Any] = field(default_factory=list)
    next_cursor: str | None = None


def _cursor_of(params: Any) -> str | None:
    return params.cursor if params is not None else None


# ---------------------------------------------------------------------------
# A genuinely unsupported capability (-32601 method-not-found) stays quiet
# ---------------------------------------------------------------------------


class _MethodNotFoundSession:
    """tools/list works; resources/list and prompts/list are unimplemented."""

    async def list_tools(self, *, params=None):
        return _FakeToolsResult(tools=[_FakeTool(name="a-tool")], next_cursor=None)

    async def list_resources(self, *, params=None):
        raise _make_mcp_error(-32601, "Method not found")

    async def list_prompts(self, *, params=None):
        raise _make_mcp_error(-32601, "Method not found")


@pytest.mark.asyncio
async def test_discover_resources_method_not_found_returns_empty_list():
    """A server that genuinely lacks resources/list reports [] quietly."""
    result = await discover_resources(_MethodNotFoundSession(), server_name="s")
    assert result == []


@pytest.mark.asyncio
async def test_discover_prompts_method_not_found_returns_empty_list():
    """A server that genuinely lacks prompts/list reports [] quietly."""
    result = await discover_prompts(_MethodNotFoundSession(), server_name="s")
    assert result == []


@pytest.mark.asyncio
async def test_discover_tools_unaffected_by_sibling_method_not_found():
    """discover_tools has no absorbing catch -- tools are mandatory -- and
    is unaffected by resources/prompts being unimplemented on the same
    session.
    """
    tools = await discover_tools(_MethodNotFoundSession())
    assert [t["name"] for t in tools] == ["a-tool"]


# ---------------------------------------------------------------------------
# A *different* protocol error must NOT be swallowed as "unsupported"
# ---------------------------------------------------------------------------


class _OtherProtocolErrorSession:
    async def list_resources(self, *, params=None):
        raise _make_mcp_error(-32602, "Invalid params")

    async def list_prompts(self, *, params=None):
        raise _make_mcp_error(-32602, "Invalid params")


@pytest.mark.asyncio
async def test_discover_resources_non_method_not_found_protocol_error_propagates():
    with pytest.raises(MCP_ERROR_CLASS):
        await discover_resources(_OtherProtocolErrorSession(), server_name="s")


@pytest.mark.asyncio
async def test_discover_prompts_non_method_not_found_protocol_error_propagates():
    with pytest.raises(MCP_ERROR_CLASS):
        await discover_prompts(_OtherProtocolErrorSession(), server_name="s")


# ---------------------------------------------------------------------------
# BLOCKER 2 reproduction: a repeated cursor must raise, never return []
# ---------------------------------------------------------------------------


class _RepeatedCursorSession:
    """Every list_* method returns the same non-terminal cursor forever.

    This is the live-reproduced BLOCKER 2 scenario: before the fix,
    discover_resources/discover_prompts caught this RuntimeError (raised by
    pagination.collect_paginated's non-terminating-loop guard) via a bare
    `except Exception` and returned `[]` -- two RPC calls, empty list, zero
    visible signal, at any log level.
    """

    async def list_tools(self, *, params=None):
        cursor = _cursor_of(params)
        if cursor is None:
            return _FakeToolsResult(tools=[_FakeTool(name="t1")], next_cursor="loop")
        return _FakeToolsResult(tools=[_FakeTool(name="t2")], next_cursor="loop")

    async def list_resources(self, *, params=None):
        cursor = _cursor_of(params)
        if cursor is None:
            return _FakeResourcesResult(
                resources=[_FakeResource(name="r1")], next_cursor="loop"
            )
        return _FakeResourcesResult(
            resources=[_FakeResource(name="r2")], next_cursor="loop"
        )

    async def list_prompts(self, *, params=None):
        cursor = _cursor_of(params)
        if cursor is None:
            return _FakePromptsResult(
                prompts=[_FakePrompt(name="p1")], next_cursor="loop"
            )
        return _FakePromptsResult(prompts=[_FakePrompt(name="p2")], next_cursor="loop")


@pytest.mark.asyncio
async def test_discover_tools_repeated_cursor_raises():
    with pytest.raises(RuntimeError, match="repeated cursor"):
        await discover_tools(_RepeatedCursorSession())


@pytest.mark.asyncio
async def test_discover_resources_repeated_cursor_raises_not_empty_list():
    """Must raise -- returning [] here is the exact bug that was live-reproduced."""
    with pytest.raises(RuntimeError, match="repeated cursor"):
        await discover_resources(_RepeatedCursorSession(), server_name="s")


@pytest.mark.asyncio
async def test_discover_prompts_repeated_cursor_raises_not_empty_list():
    """Must raise -- returning [] here is the exact bug that was live-reproduced."""
    with pytest.raises(RuntimeError, match="repeated cursor"):
        await discover_prompts(_RepeatedCursorSession(), server_name="s")


# ---------------------------------------------------------------------------
# BLOCKER 1 reproduction: a page missing the cursor field entirely must raise
# ---------------------------------------------------------------------------


@dataclass
class _ToolsResultNoCursorField:
    tools: list[Any] = field(default_factory=list)
    # Deliberately no next_cursor / nextCursor attribute at all.


@dataclass
class _ResourcesResultNoCursorField:
    resources: list[Any] = field(default_factory=list)


@dataclass
class _PromptsResultNoCursorField:
    prompts: list[Any] = field(default_factory=list)


class _UnrecognizedShapeSession:
    """Every list_* method returns a page object with neither cursor field name.

    Before the BLOCKER 1 fix, `sdk_field(..., default=None)` silently
    treated this as `next_cursor=None` (terminal page) -- a single page is
    fetched, any further pages are never retrieved, and there is no
    exception, no log line, at any level.
    """

    async def list_tools(self, *, params=None):
        return _ToolsResultNoCursorField(tools=[_FakeTool(name="only-tool")])

    async def list_resources(self, *, params=None):
        return _ResourcesResultNoCursorField(resources=[_FakeResource(name="only-r")])

    async def list_prompts(self, *, params=None):
        return _PromptsResultNoCursorField(prompts=[_FakePrompt(name="only-p")])


@pytest.mark.asyncio
async def test_discover_tools_missing_cursor_field_raises_not_truncates():
    with pytest.raises(AttributeError, match="next_cursor"):
        await discover_tools(_UnrecognizedShapeSession())


@pytest.mark.asyncio
async def test_discover_resources_missing_cursor_field_raises_not_truncates():
    with pytest.raises(AttributeError, match="next_cursor"):
        await discover_resources(_UnrecognizedShapeSession(), server_name="s")


@pytest.mark.asyncio
async def test_discover_prompts_missing_cursor_field_raises_not_truncates():
    with pytest.raises(AttributeError, match="next_cursor"):
        await discover_prompts(_UnrecognizedShapeSession(), server_name="s")
