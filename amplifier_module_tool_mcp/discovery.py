"""Shared capability discovery (tools/resources/prompts) for MCP clients.

Both transports (stdio and Streamable HTTP) discover the same three
primitive types from an established `ClientSession` in an identical way.
Prior to this module, `_discover_capabilities` was duplicated near-verbatim
in both `client.py` and `streamable_http_client.py`. This module is the
single implementation both transports call, so the pagination-following
and SDK-field-mapping logic exists in exactly one place rather than two
copies that could drift apart.

Tools are mandatory: a server that fails `tools/list` fails discovery
outright (matches the original, non-tolerant behavior). Resources and
prompts are optional MCP capabilities: a server that doesn't support them
raises on `resources/list` / `prompts/list`, which is treated as "this
server has none" rather than a connection-fatal error.
"""

from __future__ import annotations

import logging
from typing import Any

from amplifier_module_tool_mcp.pagination import (
    list_all_prompts,
    list_all_resources,
    list_all_tools,
)
from amplifier_module_tool_mcp.sdk_compat import MCP_ERROR_CLASS, sdk_field

logger = logging.getLogger(__name__)

# JSON-RPC 2.0 reserves -32601 for "the server does not implement this
# method" -- this is how an MCP server that genuinely lacks the optional
# resources/prompts capability reports that fact. It is the *only* shape
# that should be absorbed into "this server has none"; any other exception
# (a different protocol error, a transport failure, or -- critically --
# pagination.collect_paginated's deliberately loud RuntimeError safety nets
# for a repeated cursor or an unbounded page count) is a real failure and
# must propagate, exactly as it already does for discover_tools.
_METHOD_NOT_FOUND = -32601


def _is_method_not_found(exc: BaseException) -> bool:
    """True if `exc` is the SDK's protocol-error class carrying code -32601.

    Only `MCP_ERROR_CLASS` instances are inspected here -- `collect_paginated`
    raises plain `RuntimeError`, which is not an instance of `MCP_ERROR_CLASS`
    (verified: neither SDK major's error class subclasses `RuntimeError`), so
    those errors are never at risk of being misclassified as "not supported"
    by this check.
    """
    if not isinstance(exc, MCP_ERROR_CLASS):
        return False
    error_data = sdk_field(exc, "error")
    return sdk_field(error_data, "code") == _METHOD_NOT_FOUND


async def discover_tools(session: Any) -> list[dict[str, Any]]:
    """Discover all tools from ``session``, following pagination cursors.

    Propagates any error from `tools/list` -- tools are the one MCP
    primitive this module treats as mandatory, matching prior behavior.
    """
    tools = await list_all_tools(session)
    return [
        {
            "name": tool.name,
            "description": tool.description or "",
            "input_schema": sdk_field(tool, "input_schema", "inputSchema"),
        }
        for tool in tools
    ]


async def discover_resources(session: Any, *, server_name: str) -> list[dict[str, Any]]:
    """Discover all resources from ``session``, following pagination cursors.

    Resources are an optional MCP capability. A server that reports it via a
    method-not-found (-32601) protocol error is treated as "no resources"
    rather than a fatal discovery error, matching prior behavior. Any other
    exception -- a different protocol error, a transport failure, or
    pagination's repeated-cursor/max-pages guards -- propagates, since those
    are real failures, not "this capability is absent".
    """
    try:
        resources = await list_all_resources(session)
    except MCP_ERROR_CLASS as e:
        if not _is_method_not_found(e):
            raise
        logger.debug(f"Server '{server_name}' does not support resources: {e}")
        return []

    return [
        {
            "uri": str(resource.uri),
            "name": resource.name,
            "description": resource.description or "",
            "mime_type": sdk_field(resource, "mime_type", "mimeType"),
        }
        for resource in resources
    ]


async def discover_prompts(session: Any, *, server_name: str) -> list[dict[str, Any]]:
    """Discover all prompts from ``session``, following pagination cursors.

    Prompts are an optional MCP capability. A server that reports it via a
    method-not-found (-32601) protocol error is treated as "no prompts"
    rather than a fatal discovery error, matching prior behavior. Any other
    exception -- a different protocol error, a transport failure, or
    pagination's repeated-cursor/max-pages guards -- propagates, since those
    are real failures, not "this capability is absent".
    """
    try:
        prompts = await list_all_prompts(session)
    except MCP_ERROR_CLASS as e:
        if not _is_method_not_found(e):
            raise
        logger.debug(f"Server '{server_name}' does not support prompts: {e}")
        return []

    return [
        {
            "name": prompt.name,
            "description": prompt.description or "",
            "arguments": [
                {
                    "name": arg.name,
                    "description": arg.description or "",
                    "required": arg.required,
                }
                for arg in (prompt.arguments or [])
            ],
        }
        for prompt in prompts
    ]
