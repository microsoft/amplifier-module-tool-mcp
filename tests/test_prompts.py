"""Tests for MCP prompt wrapper."""

import pytest
from amplifier_core import ToolResult
from amplifier_module_tool_mcp.prompt_wrapper import MCPPromptWrapper


class MockMCPClient:
    """Mock MCP client for testing."""

    def __init__(self):
        self.call_count = 0
        self.last_prompt_name = None
        self.last_arguments = None

    async def get_prompt(self, name: str, arguments: dict):
        """Mock get_prompt method."""
        self.call_count += 1
        self.last_prompt_name = name
        self.last_arguments = arguments

        # Return mock result
        class MockMessage:
            role = "user"

            class Content:
                text = f"Filled prompt: {name} with args {arguments}"

            content = Content()

        class MockResult:
            messages = [MockMessage()]

        return MockResult()


@pytest.fixture
def sample_prompt_def():
    """Sample MCP prompt definition."""
    return {
        "name": "code_review",
        "description": "Perform systematic code review",
        "arguments": [
            {"name": "file_path", "description": "Path to file", "required": True},
            {
                "name": "focus_area",
                "description": "Area to focus on",
                "required": False,
            },
        ],
    }


@pytest.mark.asyncio
async def test_prompt_wrapper_initialization(sample_prompt_def, mock_hooks):
    """Test prompt wrapper initialization."""
    client = MockMCPClient()
    wrapper = MCPPromptWrapper("test-server", sample_prompt_def, client, mock_hooks)

    assert wrapper.server_name == "test-server"
    assert wrapper.prompt_name == "code_review"
    assert wrapper.name == "mcp_test-server_prompt_code_review"
    assert "code review" in wrapper.description.lower()
    assert len(wrapper.prompt_arguments) == 2


@pytest.mark.asyncio
async def test_prompt_wrapper_properties(sample_prompt_def, mock_hooks):
    """Test prompt wrapper properties."""
    client = MockMCPClient()
    wrapper = MCPPromptWrapper("test-server", sample_prompt_def, client, mock_hooks)

    # Check properties
    assert wrapper.name.startswith("mcp_test-server_prompt_")
    assert isinstance(wrapper.description, str)
    assert isinstance(wrapper.input_schema, dict)

    # Check schema includes arguments
    schema = wrapper.input_schema
    assert "properties" in schema
    assert "file_path" in schema["properties"]
    assert "focus_area" in schema["properties"]

    # Check required fields
    assert "required" in schema
    assert "file_path" in schema["required"]
    assert "focus_area" not in schema["required"]  # Optional


@pytest.mark.asyncio
async def test_prompt_wrapper_execute(sample_prompt_def, mock_hooks):
    """Test prompt execution through wrapper."""
    client = MockMCPClient()
    wrapper = MCPPromptWrapper("test-server", sample_prompt_def, client, mock_hooks)

    # Execute prompt
    result = await wrapper.execute(
        {"file_path": "src/app.py", "focus_area": "security"}
    )

    # Verify result is ToolResult
    assert isinstance(result, ToolResult)
    assert result.success is True
    assert isinstance(result.output, dict)
    assert "messages" in result.output
    assert "code_review" in result.output["messages"]
    assert client.call_count == 1
    assert client.last_prompt_name == "code_review"
    assert client.last_arguments == {
        "file_path": "src/app.py",
        "focus_area": "security",
    }


@pytest.mark.asyncio
async def test_prompt_wrapper_execute_minimal_args(sample_prompt_def, mock_hooks):
    """Test prompt execution with only required arguments."""
    client = MockMCPClient()
    wrapper = MCPPromptWrapper("test-server", sample_prompt_def, client, mock_hooks)

    # Execute with only required arg
    result = await wrapper.execute({"file_path": "src/app.py"})

    assert result.success is True
    assert client.last_arguments == {"file_path": "src/app.py"}


@pytest.mark.asyncio
async def test_prompt_wrapper_error_handling(sample_prompt_def, mock_hooks):
    """Test prompt wrapper error handling."""

    class FailingClient:
        async def get_prompt(self, name: str, arguments: dict):
            raise RuntimeError("Prompt retrieval failed")

    client = FailingClient()
    wrapper = MCPPromptWrapper("test-server", sample_prompt_def, client, mock_hooks)

    # Execute (should handle error gracefully)
    result = await wrapper.execute({"file_path": "src/app.py"})

    # Verify result is ToolResult with error
    assert isinstance(result, ToolResult)
    assert result.success is False
    assert result.error is not None
    assert "message" in result.error


@pytest.mark.asyncio
async def test_prompt_no_arguments(mock_hooks):
    """Test prompt without arguments."""
    client = MockMCPClient()

    prompt_def = {
        "name": "simple_prompt",
        "description": "A simple prompt",
        "arguments": [],  # No arguments
    }

    wrapper = MCPPromptWrapper("test-server", prompt_def, client, mock_hooks)

    # Schema should have no required fields
    assert wrapper.input_schema["required"] == []
    assert wrapper.input_schema["properties"] == {}

    # Execute without arguments
    result = await wrapper.execute({})
    assert result.success is True


@pytest.mark.asyncio
async def test_prompt_message_extraction(mock_hooks):
    """Test extraction of prompt messages."""

    class ComplexMessageClient:
        async def get_prompt(self, name: str, arguments: dict):
            class Message1:
                role = "system"

                class Content:
                    text = "System message"

                content = Content()

            class Message2:
                role = "user"

                class Content:
                    text = "User message"

                content = Content()

            class MockResult:
                messages = [Message1(), Message2()]

            return MockResult()

    client = ComplexMessageClient()
    prompt_def = {"name": "test", "description": "Test", "arguments": []}

    wrapper = MCPPromptWrapper("test-server", prompt_def, client, mock_hooks)
    result = await wrapper.execute({})

    # Should extract both messages
    assert result.success
    assert isinstance(result.output, dict)
    assert "messages" in result.output
    assert "[system]" in result.output["messages"]
    assert "System message" in result.output["messages"]
    assert "[user]" in result.output["messages"]
    assert "User message" in result.output["messages"]


@pytest.mark.asyncio
async def test_prompt_name_sanitization(mock_hooks):
    """Test that prompt names with special characters are sanitized."""
    client = MockMCPClient()

    prompt_def = {
        "name": "review code (v2.0)",
        "description": "Code review prompt",
        "arguments": [],
    }

    wrapper = MCPPromptWrapper("test-server", prompt_def, client, mock_hooks)

    # Only a-zA-Z0-9_- should remain in the name
    assert " " not in wrapper.name
    assert "." not in wrapper.name
    assert "(" not in wrapper.name
    assert ")" not in wrapper.name
    assert wrapper.name == "mcp_test-server_prompt_review_code__v2_0_"
