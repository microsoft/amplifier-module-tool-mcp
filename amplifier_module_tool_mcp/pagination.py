"""Cursor-following pagination for MCP `list_*` calls.

Prior to this module, `_discover_capabilities` in both transports called
`list_tools()` / `list_resources()` / `list_prompts()` once and read
`.tools` / `.resources` / `.prompts` directly, ignoring any `nextCursor` /
`next_cursor` the server returned. Any server whose tool/resource/prompt
list spans more than one page was -- and, before this module, still is --
silently truncated. No error, no warning. This is a live correctness bug
against the *current* protocol, independent of any 2026-07-28 work.

The spec is explicit that cursors are opaque tokens: "Clients MUST treat
cursors as opaque tokens ... an empty string is a valid cursor and thus
MUST NOT be treated as the end of results." So termination is decided by
`next_cursor is None` (absent), never by falsiness -- an empty-string
cursor is followed like any other.

Both transports (stdio and Streamable HTTP) page through tools, resources,
and prompts identically, so this logic exists in exactly one place and is
called by both rather than forked into two near-duplicate copies.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from amplifier_module_tool_mcp.sdk_compat import sdk_field

# Hard cap on pages fetched for a single list_* call. This is a safety net,
# not an expected limit -- a well-behaved server terminates via
# `next_cursor=None` long before this. Exceeding it means the server is
# either misbehaving or the wire is looping some other way; either way we
# raise loudly rather than silently truncating and returning a partial list.
DEFAULT_MAX_PAGES = 10_000


async def collect_paginated(
    fetch_page: Callable[[str | None], Awaitable[Any]],
    items_attr: str,
    *,
    context: str,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> list[Any]:
    """Follow a cursor-paginated MCP `list_*` call to completion.

    Args:
        fetch_page: Async callable taking the cursor for the page to fetch
            (``None`` for the first page) and returning the SDK's page
            result object (e.g. a `ListToolsResult`).
        items_attr: Attribute on the page result holding that page's items
            (e.g. ``"tools"``, ``"resources"``, ``"prompts"``).
        context: Human-readable label used in error messages (e.g.
            ``"tools/list on 'my-server'"``).
        max_pages: Safety cap on the number of pages fetched. Exceeding it
            raises rather than returning a partial list silently.

    Returns:
        The concatenation of every page's ``items_attr`` list, in order.

    Raises:
        RuntimeError: The server returns the same cursor twice in a row
            (a non-terminating loop), or more than ``max_pages`` pages are
            fetched without a terminal (``None``) cursor.

    Note on opacity: per the 2026-07-28 spec, cursors MUST be treated as
    opaque -- an empty string (``""``) is a valid, non-terminal cursor.
    Termination is therefore decided by ``next_cursor is None`` (the field
    being absent/null), never by truthiness, so an empty-string cursor is
    followed exactly like any other non-``None`` cursor.
    """
    items: list[Any] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()

    for page_num in range(1, max_pages + 1):
        result = await fetch_page(cursor)
        items.extend(getattr(result, items_attr))

        next_cursor = sdk_field(result, "next_cursor", "nextCursor", default=None)
        if next_cursor is None:
            return items

        if next_cursor in seen_cursors:
            raise RuntimeError(
                f"{context}: server returned a repeated cursor {next_cursor!r} "
                f"after {page_num} page(s) -- this is a non-terminating loop, "
                f"not a legitimate large result set. Aborting rather than "
                f"looping forever or silently returning a partial list."
            )
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    raise RuntimeError(
        f"{context}: exceeded {max_pages} pages without a terminal cursor "
        f"(no page returned next_cursor=None). Aborting to avoid an "
        f"unbounded loop rather than silently returning a partial list."
    )


async def list_all_tools(session: Any) -> list[Any]:
    """Fetch every `Tool` from ``session``, following pagination cursors."""
    from mcp import types

    async def fetch(cursor: str | None) -> Any:
        params = (
            types.PaginatedRequestParams(cursor=cursor) if cursor is not None else None
        )
        return await session.list_tools(params=params)

    return await collect_paginated(fetch, "tools", context="tools/list")


async def list_all_resources(session: Any) -> list[Any]:
    """Fetch every `Resource` from ``session``, following pagination cursors."""
    from mcp import types

    async def fetch(cursor: str | None) -> Any:
        params = (
            types.PaginatedRequestParams(cursor=cursor) if cursor is not None else None
        )
        return await session.list_resources(params=params)

    return await collect_paginated(fetch, "resources", context="resources/list")


async def list_all_prompts(session: Any) -> list[Any]:
    """Fetch every `Prompt` from ``session``, following pagination cursors."""
    from mcp import types

    async def fetch(cursor: str | None) -> Any:
        params = (
            types.PaginatedRequestParams(cursor=cursor) if cursor is not None else None
        )
        return await session.list_prompts(params=params)

    return await collect_paginated(fetch, "prompts", context="prompts/list")
