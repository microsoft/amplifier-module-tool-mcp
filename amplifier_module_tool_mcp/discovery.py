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
from amplifier_module_tool_mcp.sdk_compat import sdk_field

logger = logging.getLogger(__name__)


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

    Resources are an optional MCP capability. A server that doesn't support
    `resources/list` raises; that's treated as "no resources" rather than a
    fatal discovery error, matching prior behavior.
    """
    try:
        resources = await list_all_resources(session)
    except Exception as e:
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

    Prompts are an optional MCP capability. A server that doesn't support
    `prompts/list` raises; that's treated as "no prompts" rather than a
    fatal discovery error, matching prior behavior.
    """
    try:
        prompts = await list_all_prompts(session)
    except Exception as e:
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
