"""Prompt wrapper that exposes MCP prompts as Amplifier Tools."""

import logging
from typing import Any

from amplifier_core import ToolResult

from amplifier_module_tool_mcp.content_utils import (
    DEFAULT_MAX_CONTENT_SIZE,
    truncate_content_if_needed,
)

logger = logging.getLogger(__name__)


class MCPPromptWrapper:
    """
    Wraps an MCP server prompt as an Amplifier Tool.

    Prompts in MCP are reusable templates that can be filled with arguments.
    We wrap them as tools so they can be called by the LLM.
    """

    def __init__(
        self,
        server_name: str,
        prompt_def: dict[str, Any],
        client: Any,
        hooks: Any,
        max_content_size: int = DEFAULT_MAX_CONTENT_SIZE,
    ):
        """
        Initialize prompt wrapper.

        Args:
            server_name: Name of the MCP server providing this prompt
            prompt_def: Prompt definition (name, description, arguments)
            client: MCPClient instance to use for prompt retrieval
            hooks: Hook registry for event emission (currently unused - orchestrator handles tool events)
            max_content_size: Maximum content size in characters to prevent context exhaustion
        """
        self.server_name = server_name
        self.client = client
        self.hooks = hooks
        self.max_content_size = max_content_size

        # Extract prompt info
        self.prompt_name = prompt_def["name"]
        self._name = f"mcp_{server_name}_prompt_{self.prompt_name}"
        self._description = prompt_def.get(
            "description", f"Get prompt: {self.prompt_name}"
        )
        self.prompt_arguments = prompt_def.get("arguments", [])

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
        Input schema for the prompt tool.

        Builds schema from prompt's argument definitions.
        """
        properties = {}
        required = []

        for arg in self.prompt_arguments:
            properties[arg["name"]] = {
                "type": "string",  # MCP prompt args are typically strings
                "description": arg.get("description", ""),
            }
            if arg.get("required", False):
                required.append(arg["name"])

        return {
            "type": "object",
            "properties": properties,
            "required": required,
        }

    async def execute(self, input: dict[str, Any]) -> ToolResult:
        """
        Execute the prompt by retrieving it from the MCP server.

        Args:
            input: Tool input (prompt arguments)

        Returns:
            ToolResult with filled prompt messages

        Note:
            Tool execution events (TOOL_PRE, TOOL_POST, TOOL_ERROR) are emitted
            automatically by the orchestrator. No manual event emission needed here.
        """
        try:
            # Get prompt from MCP server
            result = await self.client.get_prompt(self.prompt_name, input)

            # Extract messages from result with size protection
            messages, was_truncated = self._extract_messages(result)

            # ToolResult.output must be a dict, not a string
            # Include MCP metadata for better log viewer experience
            return ToolResult(
                success=True,
                output={
                    "prompt": self.prompt_name,
                    "messages": messages,
                    "mcp_server": self.server_name,
                    "mcp_prompt": self.prompt_name,
                    "content_size_chars": len(messages),
                    "content_truncated": was_truncated,
                },
            )

        except Exception as e:
            logger.error(f"MCP prompt '{self.name}' retrieval failed: {e}")
            error_msg = str(e)
            return ToolResult(
                success=False,
                output=error_msg,
                error={
                    "message": error_msg,
                    "mcp_server": self.server_name,
                    "mcp_prompt": self.prompt_name,
                },
            )

    def _extract_messages(self, result: Any) -> tuple[str, bool]:
        """
        Extract messages from MCP prompt result with size protection.

        Args:
            result: Raw result from MCP server

        Returns:
            Tuple of (formatted_messages, was_truncated)
        """
        # Extract raw messages
        if hasattr(result, "messages"):
            # Result with messages attribute
            messages = result.messages
            if isinstance(messages, list):
                parts = []
                for msg in messages:
                    role = getattr(msg, "role", "unknown")
                    content = getattr(msg, "content", "")

                    # Handle content types
                    if hasattr(content, "text"):
                        text = content.text
                    elif isinstance(content, str):
                        text = content
                    else:
                        text = str(content)

                    parts.append(f"[{role}]\n{text}")

                raw_messages = "\n\n".join(parts)
            else:
                raw_messages = str(messages)
        else:
            # Fallback: convert to string
            raw_messages = str(result)

        # Apply size limit with truncation if needed
        context_info = f"MCP prompt '{self.name}'"
        messages, was_truncated = truncate_content_if_needed(
            raw_messages, self.max_content_size, context_info
        )

        return messages, was_truncated

    def __repr__(self) -> str:
        """String representation."""
        return f"MCPPromptWrapper(name={self.name}, server={self.server_name})"
