"""Tool wrapper that exposes MCP tools as Amplifier Tools."""

import logging
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from amplifier_core import ToolResult

from amplifier_module_tool_mcp.content_utils import (
    DEFAULT_MAX_CONTENT_SIZE,
    extract_text_from_mcp_content,
)
from amplifier_module_tool_mcp.sdk_compat import MCPProtocolError, sdk_field
from amplifier_module_tool_mcp.evidence import (
    DEFAULT_ARTIFACT_DIR, present_evidence, validate_budget,
)

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
        artifact_dir: Path | None = None,
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
        validate_budget(max_content_size)
        self.artifact_dir = artifact_dir or Path(DEFAULT_ARTIFACT_DIR).expanduser()

        # Extract tool info
        self.tool_name = tool_def["name"]
        tool_name_clean = re.sub(r"[^a-zA-Z0-9_-]", "_", self.tool_name)
        self._name = f"mcp_{server_name}_{tool_name_clean}"
        self._description = tool_def.get("description", "")
        self._input_schema = deepcopy(tool_def.get("input_schema", {}))
        if not isinstance(self._input_schema, dict):
            raise ValueError("Unsupported MCP input schema: expected a JSON schema object")
        self.mcp_tool_definition = deepcopy(tool_def.get("mcp_tool_definition", tool_def))

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

            # structuredContent (1.x) / structured_content (2.x) is genuinely
            # optional -- a server may simply not return it -- so this reads
            # with a default rather than the fail-loud (no-default) form used
            # for fields that are merely renamed, not optional.
            structured_content = sdk_field(
                result, "structured_content", "structuredContent", default=None
            )

            raw_text = extract_text_from_mcp_content(result.content)
            output = present_evidence(
                result, raw_text, structured_content,
                self.max_content_size, self.artifact_dir,
            )
            output.update(mcp_server=self.server_name, mcp_tool=self.tool_name)
            is_error = sdk_field(result, "is_error", "isError", default=False)
            if type(is_error) is not bool:
                raise ValueError("Unsupported MCP isError shape: expected boolean")
            return ToolResult(
                success=not is_error,
                output=output,
                error={
                    "message": "MCP server returned isError; inspect output evidence",
                    "mcp_is_error": True,
                    "mcp_server": self.server_name,
                    "mcp_tool": self.tool_name,
                } if is_error else None,
            )

        except MCPProtocolError as e:
            # A well-formed JSON-RPC protocol error from the server (real
            # error code), as opposed to a transport failure. Surface the
            # code/data/explanation so a caller can distinguish the two.
            logger.error(
                f"MCP tool '{self.name}' failed with protocol error {e.code}"
            )
            error_msg = str(e)
            try:
                output = present_evidence(
                    {"message": error_msg, "code": e.code, "data": e.data,
                     "explanation": e.explanation},
                    error_msg, e.data, self.max_content_size, self.artifact_dir,
                )
            except Exception as recovery_error:
                return ToolResult(
                    success=False,
                    output=str(recovery_error)[:self.max_content_size],
                    error={"message": str(recovery_error)[:self.max_content_size],
                           "mcp_error_code": e.code, "mcp_server": self.server_name,
                           "mcp_tool": self.tool_name},
                )
            return ToolResult(
                success=False,
                output=output,
                error={
                    "message": error_msg[:1024],
                    "mcp_server": self.server_name,
                    "mcp_tool": self.tool_name,
                    "mcp_error_code": e.code,
                    "mcp_error_data": output["structured_content"],
                    "mcp_error_explanation": e.explanation[:1024],
                    "recovery": output.get("recovery"),
                },
            )

        except Exception as e:
            error_msg = str(e)[:self.max_content_size]
            logger.error(f"MCP tool '{self.name}' failed: {error_msg}")
            return ToolResult(
                success=False,
                output=error_msg,
                error={
                    "message": error_msg,
                    "mcp_server": self.server_name,
                    "mcp_tool": self.tool_name,
                },
            )

    def __repr__(self) -> str:
        """String representation."""
        return f"MCPToolWrapper(name={self.name}, server={self.server_name})"
