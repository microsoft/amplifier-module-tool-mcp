"""Integration tests with real MCP servers.

These tests require MCP servers to be available and will be skipped
if the required commands (npx, uvx) are not found.
"""

import shutil

import pytest
from amplifier_module_tool_mcp.client import MCPClient
from amplifier_module_tool_mcp.manager import MCPManager


@pytest.fixture
def has_npx():
    """Check if npx is available."""
    return shutil.which("npx") is not None


@pytest.fixture
def has_uvx():
    """Check if uvx is available."""
    return shutil.which("uvx") is not None


@pytest.fixture
def test_mcp_config(tmp_path):
    """Create test MCP configuration file."""
    config_dir = tmp_path / ".amplifier"
    config_dir.mkdir()

    # Use a simple test server configuration
    import json

    config = {"mcpServers": {"repomix": {"command": "npx", "args": ["-y", "repomix", "--mcp"]}}}

    config_file = config_dir / "mcp.json"
    with open(config_file, "w") as f:
        json.dump(config, f)

    return config_dir.parent


@pytest.mark.asyncio
@pytest.mark.skipif(not shutil.which("npx"), reason="npx not available")
async def test_repomix_connection():
    """Test connection to repomix MCP server."""
    client = MCPClient(
        server_name="repomix",
        command="npx",
        args=["-y", "repomix", "--mcp"],
    )

    try:
        # Connect and discover tools
        await client.connect()

        # Verify connection
        assert client.is_connected
        assert client.session is not None

        # Verify tool discovery
        tools = client.get_tools()
        assert len(tools) > 0

        # Check for expected repomix tools
        tool_names = [tool["name"] for tool in tools]
        assert any("repomix" in name.lower() for name in tool_names)

        print(f"\n✅ Connected to repomix - discovered {len(tools)} tools:")
        for tool in tools:
            print(f"  - {tool['name']}: {tool['description'][:60]}...")

    finally:
        # Always disconnect
        await client.disconnect()
        assert not client.is_connected


@pytest.mark.asyncio
@pytest.mark.skipif(not shutil.which("npx"), reason="npx not available")
async def test_manager_with_real_server(mock_coordinator):
    """Test MCPManager with real server configuration."""
    manager = MCPManager(
        {
            "servers": {
                "repomix": {
                    "command": "npx",
                    "args": ["-y", "repomix", "--mcp"],
                }
            }
        },
        mock_coordinator,
    )

    try:
        # Start manager
        await manager.start()

        # Verify servers started
        servers = manager.get_server_names()
        assert "repomix" in servers

        # Verify tools registered
        tools = manager.get_tools()
        assert len(tools) > 0

        # Check tool naming convention (tools should start with mcp_{server}_)
        for tool_name in tools.keys():
            assert tool_name.startswith("mcp_")
            # Should contain server name
            assert any(server in tool_name for server in servers)

        print(f"\n✅ Manager started with {len(servers)} servers and {len(tools)} tools")

    finally:
        # Always stop manager
        await manager.stop()


@pytest.mark.asyncio
@pytest.mark.skipif(not shutil.which("npx"), reason="npx not available")
async def test_tool_execution():
    """Test actual tool execution through wrapper."""
    client = MCPClient(
        server_name="repomix",
        command="npx",
        args=["-y", "repomix", "--mcp"],
    )

    try:
        await client.connect()

        # Get first tool
        tools = client.get_tools()
        if not tools:
            pytest.skip("No tools discovered")

        first_tool = tools[0]
        print(f"\n✅ Testing tool: {first_tool['name']}")
        print(f"   Schema: {first_tool['input_schema']}")

        # Note: We don't execute the tool here as we'd need valid arguments
        # This test just verifies we can discover and inspect tools

    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_connection_lifecycle():
    """Test connection lifecycle management."""
    from amplifier_module_tool_mcp.client import ConnectionState

    client = MCPClient(
        server_name="test",
        command="npx",
        args=["-y", "repomix", "--mcp"],
    )

    # Initial state
    assert client.state == ConnectionState.DISCONNECTED
    assert not client.is_connected
    assert client.session is None
    assert client._connection_task is None

    # After connect (only if npx available)
    if shutil.which("npx"):
        await client.connect()
        assert client.state == ConnectionState.CONNECTED
        assert client.is_connected
        assert client.session is not None
        assert client._connection_task is not None

        # After disconnect
        await client.disconnect()
        assert client.state == ConnectionState.DISCONNECTED
        assert not client.is_connected
        assert client.session is None
        assert client._connection_task is None


@pytest.mark.asyncio
@pytest.mark.skipif(not shutil.which("npx"), reason="npx not available")
async def test_multiple_connections():
    """Test that connect is idempotent."""
    client = MCPClient(
        server_name="repomix",
        command="npx",
        args=["-y", "repomix", "--mcp"],
    )

    try:
        # First connect
        await client.connect()
        first_tools = client.get_tools()

        # Second connect (should be no-op)
        await client.connect()
        second_tools = client.get_tools()

        # Should have same tools
        assert len(first_tools) == len(second_tools)

    finally:
        await client.disconnect()
