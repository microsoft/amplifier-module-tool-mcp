"""Resource wrapper that exposes MCP resources as Amplifier Tools."""

import logging
import re
from typing import Any

from amplifier_core import ToolResult

from amplifier_module_tool_mcp.content_utils import (
    DEFAULT_MAX_CONTENT_SIZE,
    extract_text_from_mcp_resource,
    truncate_content_if_needed,
)

logger = logging.getLogger(__name__)


class MCPResourceWrapper:
    """
    Wraps an MCP server resource as an Amplifier Tool.

    Resources in MCP are files, data, or URIs that servers expose.
    We wrap them as tools so they can be called by the LLM.
    """

    def __init__(
        self,
        server_name: str,
        resource_def: dict[str, Any],
        client: Any,
        hooks: Any,
        max_content_size: int = DEFAULT_MAX_CONTENT_SIZE,
    ):
        """
        Initialize resource wrapper.

        Args:
            server_name: Name of the MCP server providing this resource
            resource_def: Resource definition (uri, name, description, mime_type)
            client: MCPClient instance to use for resource reading
            hooks: Hook registry for event emission (currently unused - orchestrator handles tool events)
            max_content_size: Maximum content size in characters to prevent context exhaustion
        """
        self.server_name = server_name
        self.client = client
        self.hooks = hooks
        self.max_content_size = max_content_size

        # Extract resource info
        self.resource_uri = resource_def["uri"]
        resource_name_clean = re.sub(r"[^a-zA-Z0-9_-]", "_", resource_def["name"])
        self._name = f"mcp_{server_name}_resource_{resource_name_clean}"
        self._description = resource_def.get(
            "description", f"Read resource: {resource_def['name']}"
        )
        self.mime_type = resource_def.get("mime_type")

    @property
    def name(self) -> str:
        """Tool name."""
        return self._name

    @property
    def description(self) -> str:
        """Tool description."""
        return self._description

    @property
    def input_schema(self) -> dict[str, Any]:
        """
        Input schema for the resource tool.

        Resources are read-only, so no parameters needed beyond
        optional overrides.
        """
        return {
            "type": "object",
            "properties": {
                "uri": {
                    "type": "string",
                    "description": f"Resource URI (default: {self.resource_uri})",
                    "default": self.resource_uri,
                }
            },
            "required": [],
        }

    async def execute(self, input: dict[str, Any]) -> ToolResult:
        """
        Execute the resource read by proxying to the MCP server.

        Args:
            input: Tool input (optional uri override)

        Returns:
            ToolResult with resource content

        Note:
            Tool execution events (TOOL_PRE, TOOL_POST, TOOL_ERROR) are emitted
            automatically by the orchestrator. No manual event emission needed here.
        """
        try:
            # Use provided URI or default to resource's URI
            uri = input.get("uri", self.resource_uri)

            # Read resource from MCP server
            result = await self.client.read_resource(uri)

            # Extract content from result with size protection
            content, was_truncated = self._extract_content(result)

            # ToolResult.output must be a dict, not a string
            # Include MCP metadata for better log viewer experience
            return ToolResult(
                success=True,
                output={
                    "uri": uri,
                    "content": content,
                    "mcp_server": self.server_name,
                    "mcp_resource": self.resource_uri,
                    "content_size_chars": len(content),
                    "content_truncated": was_truncated,
                },
            )

        except Exception as e:
            logger.error(f"MCP resource '{self.name}' read failed: {e}")
            error_msg = str(e)
            return ToolResult(
                success=False,
                output=error_msg,
                error={
                    "message": error_msg,
                    "mcp_server": self.server_name,
                    "mcp_resource": self.resource_uri,
                },
            )

    def _extract_content(self, result: Any) -> tuple[str, bool]:
        """
        Extract content from MCP resource result with size protection.

        Args:
            result: Raw result from MCP server

        Returns:
            Tuple of (content_string, was_truncated)
        """
        # Extract raw content
        if hasattr(result, "contents"):
            # Result with contents attribute (standard MCP format)
            raw_content = extract_text_from_mcp_resource(result.contents)
        else:
            # Fallback: convert to string
            raw_content = str(result)

        # Apply size limit with truncation if needed
        context_info = f"MCP resource '{self.name}'"
        content, was_truncated = truncate_content_if_needed(
            raw_content, self.max_content_size, context_info
        )

        return content, was_truncated

    def __repr__(self) -> str:
        """String representation."""
        return f"MCPResourceWrapper(name={self.name}, server={self.server_name}, uri={self.resource_uri})"
