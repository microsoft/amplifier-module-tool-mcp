"""Offline tests for capability discovery across MCP SDK majors.

These cover the actual regression sites -- ``_discover_capabilities()`` in both
transports -- rather than only the compatibility helpers. The production break
was a hard-coded ``tool.inputSchema`` here, so a test that never executes these
methods would not have caught it. Everything is driven by a fake session, so
these run without an MCP server or network.
"""

from types import SimpleNamespace

import pytest

from amplifier_module_tool_mcp.client import MCPClient
from amplifier_module_tool_mcp.streamable_http_client import MCPStreamableHTTPClient

SCHEMA = {"type": "object", "properties": {"q": {"type": "string"}}}


def _legacy_tool():
    """An ``mcp`` 1.x-shaped tool: camelCase attribute."""
    return SimpleNamespace(name="search", description="Search", inputSchema=SCHEMA)


def _modern_tool():
    """An ``mcp`` >= 2.0-shaped tool: snake_case attribute."""
    return SimpleNamespace(name="search", description="Search", input_schema=SCHEMA)


def _legacy_resource():
    return SimpleNamespace(
        uri="file:///notes.md",
        name="notes",
        description="Notes",
        mimeType="text/markdown",
    )


def _modern_resource():
    return SimpleNamespace(
        uri="file:///notes.md",
        name="notes",
        description="Notes",
        mime_type="text/markdown",
    )


class FakeSession:
    """Minimal stand-in for ``mcp.ClientSession`` during discovery."""

    def __init__(self, tool, resource):
        self._tool = tool
        self._resource = resource

    async def list_tools(self):
        return SimpleNamespace(tools=[self._tool])

    async def list_resources(self):
        return SimpleNamespace(resources=[self._resource])

    async def list_prompts(self):
        argument = SimpleNamespace(name="topic", description="Topic", required=True)
        prompt = SimpleNamespace(
            name="summarize", description="Summarize", arguments=[argument]
        )
        return SimpleNamespace(prompts=[prompt])


def _clients():
    """One instance of each transport, neither of which will be connected."""
    return {
        "stdio": MCPClient(server_name="fake", command="true", args=[]),
        "streamable-http": MCPStreamableHTTPClient(
            server_name="fake", url="http://localhost:1/mcp"
        ),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
@pytest.mark.parametrize(
    ("sdk_shape", "tool", "resource"),
    [
        ("mcp 1.x", _legacy_tool, _legacy_resource),
        ("mcp >= 2.0", _modern_tool, _modern_resource),
    ],
)
async def test_discovery_reads_renamed_fields(transport, sdk_shape, tool, resource):
    """Discovery must produce the same output on either SDK major."""
    client = _clients()[transport]
    client.session = FakeSession(tool(), resource())

    await client._discover_capabilities()

    assert client.tools == [
        {"name": "search", "description": "Search", "input_schema": SCHEMA}
    ], f"{transport} lost the input schema on {sdk_shape}"
    assert client.resources == [
        {
            "uri": "file:///notes.md",
            "name": "notes",
            "description": "Notes",
            "mime_type": "text/markdown",
        }
    ], f"{transport} lost the MIME type on {sdk_shape}"
    assert client.prompts == [
        {
            "name": "summarize",
            "description": "Summarize",
            "arguments": [{"name": "topic", "description": "Topic", "required": True}],
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
async def test_discovery_is_a_noop_without_a_session(transport):
    client = _clients()[transport]

    await client._discover_capabilities()

    assert client.tools == []
    assert client.resources == []
    assert client.prompts == []
