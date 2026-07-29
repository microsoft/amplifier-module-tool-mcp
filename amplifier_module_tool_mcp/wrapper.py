"""Tool wrapper that exposes MCP tools as Amplifier Tools."""

import logging
import re
from typing import Any

from amplifier_core import ToolResult

from amplifier_module_tool_mcp.content_utils import (
    DEFAULT_MAX_CONTENT_SIZE,
    extract_text_from_mcp_content,
    truncate_content_if_needed,
)
from amplifier_module_tool_mcp.sdk_compat import MCPProtocolError, sdk_field

logger = logging.getLogger(__name__)


class MCPToolWrapper:
    """
    Wraps an MCP server tool as an Amplifier Tool.

    This adapter allows MCP tools to be used seamlessly in Amplifier's
    tool system. The wrapper handles:
    - Name prefixing to avoid conflicts
    - Input schema translation
    - Tool execution proxying to MCP server
    """

    def __init__(
        self,
        server_name: str,
        tool_def: dict[str, Any],
        client: Any,
        hooks: Any,
        max_content_size: int = DEFAULT_MAX_CONTENT_SIZE,
    ):
        """
        Initialize tool wrapper.

        Args:
            server_name: Name of the MCP server providing this tool
            tool_def: Tool definition from MCP server (name, description, input_schema)
            client: MCPClient instance to use for tool execution
            hooks: Hook registry for event emission (currently unused - orchestrator handles tool events)
            max_content_size: Maximum content size in characters to prevent context exhaustion
        """
        self.server_name = server_name
        self.client = client
        self.hooks = hooks
        self.max_content_size = max_content_size

        # Extract tool info
        self.tool_name = tool_def["name"]
        tool_name_clean = re.sub(r"[^a-zA-Z0-9_-]", "_", self.tool_name)
        self._name = f"mcp_{server_name}_{tool_name_clean}"
        self._description = tool_def.get("description", "")
        self._input_schema = tool_def.get("input_schema", {})

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
        """Input schema for the tool."""
        return self._input_schema

    async def execute(self, input: dict[str, Any]) -> ToolResult:
        """
        Execute the tool by proxying to the MCP server.

        Args:
            input: Tool input parameters

        Returns:
            ToolResult with success status and output/error

        Note:
            Tool execution events (TOOL_PRE, TOOL_POST, TOOL_ERROR) are emitted
            automatically by the orchestrator. No manual event emission needed here.
        """
        try:
            # Call tool on MCP server
            result = await self.client.call_tool(self.tool_name, input)

            # Extract content from result with size protection
            content, was_truncated = self._extract_content(result)

            # structuredContent (1.x) / structured_content (2.x) is genuinely
            # optional -- a server may simply not return it -- so this reads
            # with a default rather than the fail-loud (no-default) form used
            # for fields that are merely renamed, not optional.
            structured_content = sdk_field(
                result, "structured_content", "structuredContent", default=None
            )

            # ToolResult.output must be a dict, not a string
            # Include MCP metadata for better log viewer experience
            return ToolResult(
                success=True,
                output={
                    "content": content,
                    "mcp_server": self.server_name,
                    "mcp_tool": self.tool_name,
                    "content_size_chars": len(content),
                    "content_truncated": was_truncated,
                    "structured_content": structured_content,
                },
            )

        except MCPProtocolError as e:
            # A well-formed JSON-RPC protocol error from the server (real
            # error code), as opposed to a transport failure. Surface the
            # code/data/explanation so a caller can distinguish the two.
            logger.error(
                f"MCP tool '{self.name}' failed with protocol error {e.code}: {e.explanation}"
            )
            error_msg = str(e)
            return ToolResult(
                success=False,
                output=error_msg,
                error={
                    "message": error_msg,
                    "mcp_server": self.server_name,
                    "mcp_tool": self.tool_name,
                    "mcp_error_code": e.code,
                    "mcp_error_data": e.data,
                    "mcp_error_explanation": e.explanation,
                },
            )

        except Exception as e:
            logger.error(f"MCP tool '{self.name}' failed: {e}")
            error_msg = str(e)
            return ToolResult(
                success=False,
                output=error_msg,
                error={
                    "message": error_msg,
                    "mcp_server": self.server_name,
                    "mcp_tool": self.tool_name,
                },
            )

    def _extract_content(self, result: Any) -> tuple[str, bool]:
        """
        Extract content from MCP tool result with size protection.

        Args:
            result: Raw result from MCP server

        Returns:
            Tuple of (content_string, was_truncated)
        """
        # Extract raw content
        if hasattr(result, "content"):
            # Result with content attribute (standard MCP format)
            raw_content = extract_text_from_mcp_content(result.content)
        else:
            # Fallback: convert to string
            raw_content = str(result)

        # Apply size limit with truncation if needed
        context_info = f"MCP tool '{self.name}'"
        content, was_truncated = truncate_content_if_needed(
            raw_content, self.max_content_size, context_info
        )

        return content, was_truncated

    def __repr__(self) -> str:
        """String representation."""
        return f"MCPToolWrapper(name={self.name}, server={self.server_name})"
