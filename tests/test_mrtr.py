"""Regression tests for the MRTR (Multi Round-Trip Requests) honest-failure fix.

On `mcp>=2.0`, a server that returns `InputRequiredResult` from `tools/call`
causes `ClientSession.call_tool` to raise a bare `RuntimeError` instructing
the caller to "pass allow_input_required=True ... and retry call_tool(...,
input_responses=..., request_state=...)". This module exposes no such
parameters anywhere in its public surface, so that instruction reached the
calling Amplifier agent verbatim and was un-actionable.

These tests exercise `MCPClient.call_tool` and `MCPStreamableHTTPClient.call_tool`
directly (bypassing the real connect()/session machinery by installing a fake
session), proving the SDK's message is replaced with an honest one.
"""

import pytest

from amplifier_module_tool_mcp.client import ConnectionState, MCPClient
from amplifier_module_tool_mcp.sdk_compat import MRTRNotSupportedError
from amplifier_module_tool_mcp.streamable_http_client import MCPStreamableHTTPClient


def _mrtr_runtime_error() -> RuntimeError:
    """The exact shape of RuntimeError mcp>=2.0's ClientSession raises."""
    return RuntimeError(
        "Server returned InputRequiredResult; pass allow_input_required=True "
        "to receive it and retry call_tool(..., input_responses=..., "
        "request_state=result.request_state)."
    )


class _MRTRSession:
    """A fake session whose call_tool always raises the SDK's MRTR guard."""

    async def call_tool(self, tool_name: str, arguments: dict):
        raise _mrtr_runtime_error()


@pytest.mark.asyncio
async def test_stdio_client_call_tool_replaces_mrtr_message_with_honest_one():
    client = MCPClient(server_name="test-server", command="true", args=[])
    client.session = _MRTRSession()  # type: ignore[assignment]
    client._state = ConnectionState.CONNECTED

    with pytest.raises(MRTRNotSupportedError) as exc_info:
        await client.call_tool("some_tool", {})

    message = str(exc_info.value)
    # The honest message must explain what happened...
    assert "requires interactive input" in message
    assert "MRTR" in message
    assert "does not yet support" in message
    # ...and must NOT tell the caller to do something this module doesn't
    # let it do.
    assert "allow_input_required" not in message
    assert "input_responses" not in message


@pytest.mark.asyncio
async def test_http_client_call_tool_replaces_mrtr_message_with_honest_one():
    client = MCPStreamableHTTPClient(server_name="test-server", url="http://x/mcp")
    client.session = _MRTRSession()  # type: ignore[assignment]
    client._connected = True

    with pytest.raises(MRTRNotSupportedError) as exc_info:
        await client.call_tool("some_tool", {})

    message = str(exc_info.value)
    assert "requires interactive input" in message
    assert "MRTR" in message
    assert "does not yet support" in message
    assert "allow_input_required" not in message
    assert "input_responses" not in message


@pytest.mark.asyncio
async def test_stdio_client_call_tool_unrelated_runtime_error_unaffected():
    """A RuntimeError that isn't the MRTR guard is wrapped as before -- the
    MRTR detection must not change behavior for ordinary failures.
    """

    class _FailingSession:
        async def call_tool(self, tool_name: str, arguments: dict):
            raise RuntimeError("connection reset by peer")

    client = MCPClient(server_name="test-server", command="true", args=[])
    client.session = _FailingSession()  # type: ignore[assignment]
    client._state = ConnectionState.CONNECTED

    with pytest.raises(RuntimeError) as exc_info:
        await client.call_tool("some_tool", {})

    assert not isinstance(exc_info.value, MRTRNotSupportedError)
    assert "connection reset by peer" in str(exc_info.value)
