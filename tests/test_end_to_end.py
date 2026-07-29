"""End-to-end test against a REAL MCP server -- no mocks, no monkeypatching.

The existing test suite (test_wrapper.py, test_resources.py, etc.) mocks the
`mcp` SDK objects it exercises, so it did not catch the total connect
failure introduced by the `mcp` 2.0.0 field renames (e.g.
``AttributeError: 'Tool' object has no attribute 'inputSchema'`` during
capability discovery). This test spawns a genuine MCP server subprocess
over stdio and drives it through the real ``MCPClient``, so a regression in
either the transport, the discovery/schema handling, or the sdk_compat
shim will fail this test for real -- it cannot pass vacuously.

The server-side API differs between `mcp` SDK majors:
    - mcp 1.x: ``mcp.server.fastmcp.FastMCP``
    - mcp 2.x: ``mcp.server.MCPServer`` (``mcp.server.fastmcp`` does not exist)

Both expose the same ``@mcp.tool()`` decorator and a synchronous ``.run()``
that serves over stdio by default, so a single client-side test can drive
whichever server script matches the installed SDK.
"""

import sys
import textwrap
from pathlib import Path

import pytest

from amplifier_module_tool_mcp.client import MCPClient
from amplifier_module_tool_mcp.content_utils import extract_text_from_mcp_content

# Generous but bounded: covers subprocess spawn + MCP handshake + one tool call
# without letting a hung server block the suite indefinitely.
TEST_TIMEOUT = 30

_FASTMCP_SERVER_SOURCE = textwrap.dedent(
    '''\
    """Minimal real MCP server for end-to-end testing (mcp 1.x FastMCP API)."""

    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("e2e-test-server")


    @mcp.tool()
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b


    if __name__ == "__main__":
        mcp.run()
    '''
)

_MCPSERVER_SERVER_SOURCE = textwrap.dedent(
    '''\
    """Minimal real MCP server for end-to-end testing (mcp 2.x MCPServer API)."""

    from mcp.server import MCPServer

    mcp = MCPServer("e2e-test-server")


    @mcp.tool()
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b


    if __name__ == "__main__":
        mcp.run()
    '''
)


def _detect_server_source() -> str | None:
    """Return server source code matching whichever `mcp` server API is
    importable in the current environment, or None if neither is available.

    Detection must happen via real imports of the actual SDK modules -- not
    version-string parsing -- since that is the only way to know which API
    shape is genuinely present.
    """
    try:
        import mcp.server.fastmcp  # noqa: F401
    except ImportError:
        pass
    else:
        return _FASTMCP_SERVER_SOURCE

    try:
        from mcp.server import MCPServer  # type: ignore[attr-defined]  # noqa: F401
    except ImportError:
        pass
    else:
        return _MCPSERVER_SERVER_SOURCE

    return None


@pytest.mark.asyncio
@pytest.mark.timeout(TEST_TIMEOUT)
async def test_real_server_connect_discover_call_disconnect(tmp_path: Path) -> None:
    """Full real-server round trip: connect -> discover -> call_tool -> disconnect.

    No mocks anywhere in this test. If the installed `mcp` SDK's server API
    cannot be found, the test is skipped with an explicit reason rather than
    silently passing.
    """
    server_source = _detect_server_source()
    if server_source is None:
        pytest.skip(
            "Neither mcp.server.fastmcp.FastMCP (mcp 1.x) nor "
            "mcp.server.MCPServer (mcp 2.x) is importable in this "
            "environment -- cannot run a real end-to-end MCP server test."
        )

    server_file = tmp_path / "e2e_server.py"
    server_file.write_text(server_source, encoding="utf-8")

    client = MCPClient(
        server_name="e2e-test",
        command=sys.executable,
        args=[str(server_file)],
    )

    try:
        await client.connect()
        assert client.is_connected

        tools = client.get_tools()
        assert len(tools) == 1, f"Expected exactly one tool, got: {tools}"

        tool = tools[0]
        assert tool["name"] == "add"

        input_schema = tool["input_schema"]
        assert isinstance(input_schema, dict)
        assert input_schema, "input_schema must be a non-empty dict"
        properties = input_schema.get("properties", {})
        assert "a" in properties
        assert "b" in properties

        result = await client.call_tool("add", {"a": 2, "b": 3})
        result_text = extract_text_from_mcp_content(result.content)
        assert "5" in result_text, (
            f"Expected tool result to contain '5', got: {result_text!r}"
        )

    finally:
        await client.disconnect()
        assert not client.is_connected
