"""Tests for cursor-following pagination (amplifier_module_tool_mcp.pagination).

Prior to this module, `list_tools()`/`list_resources()`/`list_prompts()`
were called once and any `nextCursor`/`next_cursor` was ignored -- any
server with more than one page of results was silently truncated. These
tests prove the fix: cursors are followed to completion, an empty-string
cursor is treated as valid and non-terminal (per the spec's opacity rule),
and both failure modes (repeated cursor, unbounded pages) raise instead of
silently returning a partial list.
"""

from dataclasses import dataclass, field
from typing import Any

import pytest

from amplifier_module_tool_mcp.pagination import (
    collect_paginated,
    list_all_prompts,
    list_all_resources,
    list_all_tools,
)


@dataclass
class _FakeTool:
    name: str


@dataclass
class _FakeResource:
    name: str
    uri: str = "file:///fake"


@dataclass
class _FakePrompt:
    name: str


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


@dataclass
class _FakePage:
    """Minimal stand-in for a `ListToolsResult`/`ListResourcesResult`/etc.

    Only needs an items list and a `next_cursor` attribute -- `collect_paginated`
    reads the cursor via `sdk_field(result, "next_cursor", "nextCursor", default=None)`,
    so exposing `next_cursor` directly is sufficient regardless of installed
    `mcp` SDK major.
    """

    items: list
    next_cursor: str | None = None


class _PageFetcher:
    """Records every cursor it's called with and serves canned pages."""

    def __init__(self, pages_by_cursor: dict):
        self._pages_by_cursor = pages_by_cursor
        self.calls: list[str | None] = []

    async def __call__(self, cursor: str | None):
        self.calls.append(cursor)
        return self._pages_by_cursor[cursor]


@pytest.mark.asyncio
async def test_collect_paginated_single_page_no_cursor():
    """A server with one page (no next_cursor) is fetched exactly once."""
    fetcher = _PageFetcher({None: _FakePage(items=["a", "b"], next_cursor=None)})

    result = await collect_paginated(fetcher, "items", context="test")

    assert result == ["a", "b"]
    assert fetcher.calls == [None]


@pytest.mark.asyncio
async def test_collect_paginated_follows_multi_page_cursor():
    """A three-page result set is fully collected, in order, via the cursor chain."""
    pages = {
        None: _FakePage(items=[1, 2], next_cursor="page-2"),
        "page-2": _FakePage(items=[3, 4], next_cursor="page-3"),
        "page-3": _FakePage(items=[5], next_cursor=None),
    }
    fetcher = _PageFetcher(pages)

    result = await collect_paginated(fetcher, "items", context="test")

    assert result == [1, 2, 3, 4, 5]
    assert fetcher.calls == [None, "page-2", "page-3"]


@pytest.mark.asyncio
async def test_collect_paginated_empty_string_cursor_is_followed_not_terminal():
    """An empty-string cursor MUST be followed, not treated as end-of-results.

    Per the 2026-07-28 spec: "Clients MUST treat cursors as opaque tokens
    ... an empty string is a valid cursor and thus MUST NOT be treated as
    the end of results." Only `next_cursor is None` (the field being
    absent) terminates pagination.
    """
    pages = {
        None: _FakePage(items=["first"], next_cursor=""),
        "": _FakePage(items=["second"], next_cursor=None),
    }
    fetcher = _PageFetcher(pages)

    result = await collect_paginated(fetcher, "items", context="test")

    # Both pages were collected -- the empty-string cursor was followed.
    assert result == ["first", "second"]
    assert fetcher.calls == [None, ""]


@pytest.mark.asyncio
async def test_collect_paginated_repeated_cursor_raises():
    """A server returning the same cursor forever is a non-terminating loop, not data."""
    pages = {
        None: _FakePage(items=["a"], next_cursor="loop"),
        "loop": _FakePage(items=["b"], next_cursor="loop"),
    }
    fetcher = _PageFetcher(pages)

    with pytest.raises(RuntimeError, match="repeated cursor"):
        await collect_paginated(fetcher, "items", context="test")


@pytest.mark.asyncio
async def test_collect_paginated_exceeds_max_pages_raises():
    """Exceeding max_pages raises rather than silently returning a partial list."""

    call_count = {"n": 0}

    async def fetch(cursor: str | None) -> _FakePage:
        call_count["n"] += 1
        # Every cursor is unique, so the repeated-cursor guard never fires --
        # this exercises the max_pages safety net specifically.
        return _FakePage(
            items=[call_count["n"]], next_cursor=f"cursor-{call_count['n']}"
        )

    with pytest.raises(RuntimeError, match="exceeded 3 pages"):
        await collect_paginated(fetch, "items", context="test", max_pages=3)

    assert call_count["n"] == 3


@pytest.mark.asyncio
async def test_list_all_tools_follows_pagination():
    """list_all_tools() drives a real session-shaped object across multiple pages.

    Uses the real `mcp.types.PaginatedRequestParams` (constructed inside
    `list_all_tools` itself) against a fake session, proving the cursor
    round-trips correctly through the actual SDK params type on whichever
    `mcp` major is installed.
    """

    class _FakeSession:
        def __init__(self):
            self.calls: list[str | None] = []

        async def list_tools(self, *, params=None):
            cursor = params.cursor if params is not None else None
            self.calls.append(cursor)
            if cursor is None:
                return _FakeToolsResult(
                    tools=[_FakeTool(name="tool-a")], next_cursor="page-2"
                )
            return _FakeToolsResult(tools=[_FakeTool(name="tool-b")], next_cursor=None)

    session = _FakeSession()
    tools = await list_all_tools(session)

    assert [t.name for t in tools] == ["tool-a", "tool-b"]
    assert session.calls == [None, "page-2"]


@pytest.mark.asyncio
async def test_list_all_resources_follows_pagination():
    class _FakeSession:
        async def list_resources(self, *, params=None):
            cursor = params.cursor if params is not None else None
            if cursor is None:
                return _FakeResourcesResult(
                    resources=[_FakeResource(name="a")], next_cursor="page-2"
                )
            return _FakeResourcesResult(
                resources=[_FakeResource(name="b")], next_cursor=None
            )

    resources = await list_all_resources(_FakeSession())

    assert [r.name for r in resources] == ["a", "b"]


@pytest.mark.asyncio
async def test_list_all_prompts_follows_pagination():
    class _FakeSession:
        async def list_prompts(self, *, params=None):
            cursor = params.cursor if params is not None else None
            if cursor is None:
                return _FakePromptsResult(
                    prompts=[_FakePrompt(name="prompt-a")], next_cursor="page-2"
                )
            return _FakePromptsResult(
                prompts=[_FakePrompt(name="prompt-b")], next_cursor=None
            )

    prompts = await list_all_prompts(_FakeSession())

    assert [p.name for p in prompts] == ["prompt-a", "prompt-b"]
