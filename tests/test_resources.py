"""Tests for MCP resource wrapper."""

import json

import pytest
from amplifier_core import ToolResult
from amplifier_module_tool_mcp.resource_wrapper import MCPResourceWrapper


class MockMCPClient:
    """Mock MCP client for testing."""

    def __init__(self):
        self.call_count = 0
        self.last_uri = None

    async def read_resource(self, uri: str):
        """Mock read_resource method."""
        self.call_count += 1
        self.last_uri = uri

        # Return mock result
        class MockContent:
            text = f"Resource content from {uri}"

        class MockResult:
            contents = [MockContent()]

        return MockResult()


@pytest.fixture
def sample_resource_def():
    """Sample MCP resource definition."""
    return {
        "uri": "file:///workspace/README.md",
        "name": "project_readme",
        "description": "Project README file",
        "mime_type": "text/markdown",
    }


@pytest.mark.asyncio
async def test_resource_wrapper_initialization(sample_resource_def, mock_hooks):
    """Test resource wrapper initialization."""
    client = MockMCPClient()
    wrapper = MCPResourceWrapper("test-server", sample_resource_def, client, mock_hooks)

    assert wrapper.server_name == "test-server"
    assert wrapper.resource_uri == "file:///workspace/README.md"
    assert wrapper.name == "mcp_test-server_resource_project_readme"
    assert "README" in wrapper.description
    assert wrapper.mime_type == "text/markdown"


@pytest.mark.asyncio
async def test_resource_wrapper_properties(sample_resource_def, mock_hooks):
    """Test resource wrapper properties."""
    client = MockMCPClient()
    wrapper = MCPResourceWrapper("test-server", sample_resource_def, client, mock_hooks)

    # Check properties
    assert wrapper.name.startswith("mcp_test-server_resource_")
    assert isinstance(wrapper.description, str)
    assert isinstance(wrapper.input_schema, dict)
    assert "properties" in wrapper.input_schema
    assert "uri" in wrapper.input_schema["properties"]


@pytest.mark.asyncio
async def test_resource_wrapper_execute_default_uri(sample_resource_def, mock_hooks):
    """Test resource execution with default URI."""
    client = MockMCPClient()
    wrapper = MCPResourceWrapper("test-server", sample_resource_def, client, mock_hooks)

    # Execute without providing URI (use default)
    result = await wrapper.execute({})

    # Verify result is ToolResult
    assert isinstance(result, ToolResult)
    assert result.success is True
    assert isinstance(result.output, dict)
    assert "content" in result.output
    assert "file:///workspace/README.md" in result.output["content"]

    # Verify client was called
    assert client.call_count == 1
    assert client.last_uri == "file:///workspace/README.md"


@pytest.mark.asyncio
async def test_resource_wrapper_execute_custom_uri(sample_resource_def, mock_hooks):
    """Test resource execution with custom URI."""
    client = MockMCPClient()
    wrapper = MCPResourceWrapper("test-server", sample_resource_def, client, mock_hooks)

    # Execute with custom URI
    result = await wrapper.execute({"uri": "file:///other/path.txt"})

    # Verify custom URI was used
    assert result.success is True
    assert client.last_uri == "file:///other/path.txt"


@pytest.mark.asyncio
async def test_resource_wrapper_error_handling(sample_resource_def, mock_hooks):
    """Test resource wrapper error handling."""

    class FailingClient:
        async def read_resource(self, uri: str):
            raise RuntimeError("Resource read failed")

    client = FailingClient()
    wrapper = MCPResourceWrapper("test-server", sample_resource_def, client, mock_hooks)

    # Execute (should handle error gracefully)
    result = await wrapper.execute({})

    # Verify result is ToolResult with error
    assert isinstance(result, ToolResult)
    assert result.success is False
    assert result.error is not None
    assert "message" in result.error


@pytest.mark.asyncio
async def test_resource_name_sanitization(mock_hooks):
    """Test that resource names with special characters are sanitized."""
    client = MockMCPClient()

    # Resource with special characters in name
    resource_def = {
        "uri": "file:///path/to/file.txt",
        "name": "path/to/file:name",  # Has / and :
        "description": "Test resource",
    }

    wrapper = MCPResourceWrapper("test-server", resource_def, client, mock_hooks)

    # Name should have special chars replaced
    assert "/" not in wrapper.name
    assert ":" not in wrapper.name
    assert wrapper.name == "mcp_test-server_resource_path_to_file_name"


@pytest.mark.asyncio
async def test_resource_content_extraction(mock_hooks):
    """Test extraction of different content formats."""

    class MultiContentClient:
        async def read_resource(self, uri: str):
            class TextContent:
                text = "Text part"

            class BlobContent:
                blob = b"binary data"

            class MockResult:
                contents = [TextContent(), BlobContent()]

            return MockResult()

    client = MultiContentClient()
    resource_def = {
        "uri": "file:///test",
        "name": "test",
        "description": "Test",
    }

    wrapper = MCPResourceWrapper("test-server", resource_def, client, mock_hooks)
    result = await wrapper.execute({})

    # Should handle both text and blob
    assert result.success
    assert isinstance(result.output, dict)
    assert "content" in result.output
    assert "Text part" in result.output["content"]
    assert (
        "Binary content" in result.output["content"]
        or "blob" in result.output["content"].lower()
    )


@pytest.mark.asyncio
async def test_resource_name_sanitization_spaces(mock_hooks):
    """Test that resource names with spaces are sanitized (regression for API rejection)."""
    client = MockMCPClient()

    resource_def = {
        "uri": "senzing://resource/terms-of-service",
        "name": "Terms of Service",
        "description": "Senzing Terms of Service",
    }

    wrapper = MCPResourceWrapper("senzing", resource_def, client, mock_hooks)

    # Spaces must be replaced - Anthropic requires ^[a-zA-Z0-9_-]{1,128}$
    assert " " not in wrapper.name
    assert wrapper.name == "mcp_senzing_resource_Terms_of_Service"


@pytest.mark.asyncio
async def test_resource_name_sanitization_special_chars(mock_hooks):
    """Test that various special characters in resource names are sanitized."""
    client = MockMCPClient()

    resource_def = {
        "uri": "file:///test",
        "name": "my.resource (v2.1)",
        "description": "Test",
    }

    wrapper = MCPResourceWrapper("test-server", resource_def, client, mock_hooks)

    # Only a-zA-Z0-9_- should remain
    assert "." not in wrapper.name
    assert "(" not in wrapper.name
    assert ")" not in wrapper.name
    assert wrapper.name == "mcp_test-server_resource_my_resource__v2_1_"


@pytest.mark.asyncio
async def test_resource_input_schema_json_serializable(sample_resource_def, mock_hooks):
    """Test that the resource input_schema is fully JSON-serializable.

    Regression test: MCP SDK returns AnyUrl objects for resource URIs.
    If str() conversion is removed from the client discovery code, the
    schema default field would contain a non-serializable object, crashing
    the session when the orchestrator sends tool definitions to the LLM.
    """
    client = MockMCPClient()
    wrapper = MCPResourceWrapper("test-server", sample_resource_def, client, mock_hooks)

    schema = wrapper.input_schema

    # This must not raise TypeError
    serialized = json.dumps(schema)
    assert isinstance(serialized, str)

    # The default URI value must be a plain string
    default_uri = schema["properties"]["uri"]["default"]
    assert isinstance(default_uri, str)
