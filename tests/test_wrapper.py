"""Tests for MCP tool wrapper."""

import json
from typing import ClassVar

import pytest
from amplifier_core import ToolResult

from amplifier_module_tool_mcp.sdk_compat import MCPProtocolError
from amplifier_module_tool_mcp.wrapper import MCPToolWrapper


class MockMCPClient:
    """Mock MCP client for testing."""

    def __init__(self):
        self.call_count = 0
        self.last_tool_name = None
        self.last_arguments = None

    async def call_tool(self, tool_name: str, arguments: dict):
        """Mock call_tool method."""
        self.call_count += 1
        self.last_tool_name = tool_name
        self.last_arguments = arguments

        # Return mock result
        class MockResult:
            class MockContent:
                text = "Mock tool output"

            content = [MockContent()]

        return MockResult()


@pytest.mark.asyncio
async def test_wrapper_initialization(sample_tool_def, mock_hooks):
    """Test tool wrapper initialization."""
    client = MockMCPClient()
    wrapper = MCPToolWrapper("test-server", sample_tool_def, client, mock_hooks)

    assert wrapper.server_name == "test-server"
    assert wrapper.tool_name == "test_tool"
    assert wrapper.name == "mcp_test-server_test_tool"
    assert wrapper.description == "A test tool"
    assert "properties" in wrapper.input_schema


@pytest.mark.asyncio
async def test_wrapper_execute(sample_tool_def, mock_hooks):
    """Test tool execution through wrapper."""
    client = MockMCPClient()
    wrapper = MCPToolWrapper("test-server", sample_tool_def, client, mock_hooks)

    # Execute tool
    result = await wrapper.execute({"input": "test"})

    # Verify result is ToolResult
    assert isinstance(result, ToolResult)
    assert result.success is True
    assert isinstance(result.output, dict)
    assert "content" in result.output
    assert "Mock tool output" in result.output["content"]
    assert client.call_count == 1
    assert client.last_tool_name == "test_tool"
    assert client.last_arguments == {"input": "test"}


@pytest.mark.asyncio
async def test_wrapper_error_handling(sample_tool_def, mock_hooks):
    """Test tool wrapper error handling."""

    class FailingClient:
        async def call_tool(self, tool_name: str, arguments: dict):
            raise RuntimeError("Tool execution failed")

    client = FailingClient()
    wrapper = MCPToolWrapper("test-server", sample_tool_def, client, mock_hooks)

    # Execute tool (should handle error gracefully)
    result = await wrapper.execute({"input": "test"})

    # Verify result is ToolResult with error
    assert isinstance(result, ToolResult)
    assert result.success is False
    assert result.error is not None
    assert "message" in result.error


@pytest.mark.asyncio
async def test_tool_name_sanitization(mock_hooks):
    """Test that tool names with special characters are sanitized."""
    client = MockMCPClient()

    tool_def = {
        "name": "get user.info (v2)",
        "description": "Get user info",
        "input_schema": {"type": "object", "properties": {}},
    }

    wrapper = MCPToolWrapper("test-server", tool_def, client, mock_hooks)

    # Only a-zA-Z0-9_- should remain in the name
    assert " " not in wrapper.name
    assert "." not in wrapper.name
    assert "(" not in wrapper.name
    assert ")" not in wrapper.name
    assert wrapper.name == "mcp_test-server_get_user_info__v2_"


@pytest.mark.asyncio
async def test_tool_input_schema_json_serializable(sample_tool_def, mock_hooks):
    """Test that the tool input_schema is fully JSON-serializable."""
    client = MockMCPClient()
    wrapper = MCPToolWrapper("test-server", sample_tool_def, client, mock_hooks)

    # Must not raise TypeError
    serialized = json.dumps(wrapper.input_schema)
    assert isinstance(serialized, str)


@pytest.mark.asyncio
async def test_wrapper_execute_surfaces_structured_content(sample_tool_def, mock_hooks):
    """structuredContent/structured_content from the server is surfaced, not discarded."""

    class StructuredResultClient:
        async def call_tool(self, tool_name: str, arguments: dict):
            class MockContent:
                text = "text form"

            class MockResult:
                content: ClassVar[list] = [MockContent()]
                structured_content: ClassVar[dict] = {"result": 5}

            return MockResult()

    client = StructuredResultClient()
    wrapper = MCPToolWrapper("test-server", sample_tool_def, client, mock_hooks)

    result = await wrapper.execute({"input": "test"})

    assert result.success is True
    assert result.output is not None
    assert result.output["structured_content"] == {"result": 5}


@pytest.mark.asyncio
async def test_wrapper_execute_structured_content_absent_defaults_to_none(
    sample_tool_def, mock_hooks
):
    """A server that never returns structuredContent gets None, not an error.

    structuredContent is genuinely optional -- absence must not raise
    (unlike a renamed-but-mandatory field, which sdk_field still refuses to
    silently default).
    """
    client = (
        MockMCPClient()
    )  # MockResult above has no structured_content attribute at all
    wrapper = MCPToolWrapper("test-server", sample_tool_def, client, mock_hooks)

    result = await wrapper.execute({"input": "test"})

    assert result.success is True
    assert result.output is not None
    assert result.output["structured_content"] is None


@pytest.mark.asyncio
async def test_wrapper_execute_surfaces_mcp_protocol_error(sample_tool_def, mock_hooks):
    """An MCPProtocolError raised by the client is distinguished from a generic failure.

    Its code/data/explanation must be preserved in ToolResult.error so a
    caller can tell a protocol-level rejection (real JSON-RPC error code)
    apart from a transport failure (connection refused, timeout, etc.).
    """

    class ProtocolFailingClient:
        async def call_tool(self, tool_name: str, arguments: dict):
            raise MCPProtocolError(
                "Tool execution failed: missing capability",
                code=-32021,
                message="missing capability",
                data={"requiredCapabilities": ["elicitation"]},
                explanation="MissingRequiredClientCapability: ...",
            )

    client = ProtocolFailingClient()
    wrapper = MCPToolWrapper("test-server", sample_tool_def, client, mock_hooks)

    result = await wrapper.execute({"input": "test"})

    assert isinstance(result, ToolResult)
    assert result.success is False
    assert result.error is not None
    assert result.error["mcp_error_code"] == -32021
    assert result.error["mcp_error_data"] == {"requiredCapabilities": ["elicitation"]}
    assert "MissingRequiredClientCapability" in result.error["mcp_error_explanation"]
