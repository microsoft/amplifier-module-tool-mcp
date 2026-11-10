"""Cancelling startup must finish the connection task's transport cleanup."""

import asyncio
import json
import traceback
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from amplifier_module_tool_mcp import client as stdio
from amplifier_module_tool_mcp import streamable_http_client as http
from amplifier_module_tool_mcp.manager import MCPManager
from amplifier_module_tool_mcp.wrapper import MCPToolWrapper


def private_client(transport, redact):
    if transport == "stdio":
        return stdio.MCPClient(
            "transient",
            "unused",
            [],
            verbose_servers=True,
            redact_connection_details=redact,
        )
    return http.MCPStreamableHTTPClient(
        "transient",
        "https://example.invalid/mcp?lease=private-sentinel",
        {"Authorization": "Bearer private-sentinel"},
        redact_connection_details=redact,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["stdio", "http"])
async def test_cancel_stalled_handshake_joins_transport_cleanup(monkeypatch, transport):
    module = stdio if transport == "stdio" else http
    client = (
        stdio.MCPClient("stalled", "unused", [], verbose_servers=True)
        if transport == "stdio"
        else http.MCPStreamableHTTPClient("stalled", "http://unused.invalid/mcp")
    )
    negotiating = asyncio.Event()
    cleaned = []

    @asynccontextmanager
    async def connection(*args, **kwargs):
        try:
            yield (object(), object())
        finally:
            await asyncio.sleep(0)
            cleaned.append("transport")

    @asynccontextmanager
    async def session(*args, **kwargs):
        try:
            yield object()
        finally:
            await asyncio.sleep(0)
            cleaned.append("session")

    async def negotiate(*args, **kwargs):
        negotiating.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(module, "ClientSession", session)
    monkeypatch.setattr(module, "negotiate", negotiate)
    if transport == "stdio":
        monkeypatch.setattr(module, "stdio_client", connection)
    else:
        monkeypatch.setattr(module, "create_mcp_http_client", session)
        monkeypatch.setattr(module, "streamable_http_client", connection)

    previous_tasks = asyncio.all_tasks()
    pending = asyncio.create_task(client.connect())
    connection_task = None
    try:
        await asyncio.wait_for(negotiating.wait(), 2)
        connection_task = client._connection_task
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 2)
        assert connection_task.done()
        assert client._connection_task is None
        assert cleaned == (
            ["session", "transport"]
            if transport == "stdio"
            else ["session", "transport", "session"]
        )
        assert not client.is_connected
        assert client.session is None
        assert not (asyncio.all_tasks() - previous_tasks)
    finally:
        # Keep the predecessor regression safe: it leaves this task running.
        pending.cancel()
        connection_task = connection_task or client._connection_task
        if connection_task is not None:
            connection_task.cancel()
        await asyncio.gather(
            pending,
            *([connection_task] if connection_task else []),
            return_exceptions=True,
        )


@pytest.mark.asyncio
async def test_cancel_actual_http_handshake_closes_connection():
    requested = asyncio.Event()
    closed = asyncio.Event()
    handlers = set()

    async def stall(reader, writer):
        handlers.add(asyncio.current_task())
        try:
            if await reader.read(65536):
                requested.set()
            while await reader.read(65536):
                pass
        finally:
            writer.close()
            await writer.wait_closed()
            closed.set()

    server = await asyncio.start_server(stall, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = http.MCPStreamableHTTPClient("stalled", f"http://127.0.0.1:{port}/mcp")
    pending = asyncio.create_task(client.connect())
    connection_task = None
    try:
        await asyncio.wait_for(requested.wait(), 5)
        connection_task = client._connection_task
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 5)
        assert connection_task.done()
        assert client._connection_task is None
        await asyncio.wait_for(closed.wait(), 5)
    finally:
        pending.cancel()
        connection_task = connection_task or client._connection_task
        if connection_task is not None:
            connection_task.cancel()
        await asyncio.gather(
            pending,
            *([connection_task] if connection_task else []),
            return_exceptions=True,
        )
        server.close()
        await server.wait_closed()
        for handler in handlers:
            handler.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["stdio", "http"])
@pytest.mark.parametrize("redact", [False, True])
async def test_connection_failure_diagnostics_respect_transient_setting(
    monkeypatch, caplog, transport, redact
):
    module = stdio if transport == "stdio" else http
    client = private_client(transport, redact)

    @asynccontextmanager
    async def failed_connection(*args, **kwargs):
        raise ConnectionError("https://example.invalid private-sentinel")
        yield  # pragma: no cover -- make this a transport context manager

    monkeypatch.setattr(
        module,
        "stdio_client" if transport == "stdio" else "create_mcp_http_client",
        failed_connection,
    )
    with pytest.raises(RuntimeError) as failure:
        await client.connect()
    diagnostics = (
        caplog.text + json.dumps(client.health_status)
        + "".join(traceback.format_exception(failure.value))
    )
    assert ("private-sentinel" in diagnostics) is not redact
    if redact:
        assert "ConnectionError" in diagnostics
    assert client._connection_task is None


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["stdio", "http"])
@pytest.mark.parametrize("redact", [False, True])
async def test_tool_transport_failure_redacted_but_success_content_preserved(
    caplog, transport, redact
):
    client = private_client(transport, redact)
    client._state = stdio.ConnectionState.CONNECTED
    client._connected = True
    client.session = SimpleNamespace(
        call_tool=AsyncMock(
            side_effect=ConnectionError("Authorization: Bearer private-sentinel")
        )
    )
    with pytest.raises(RuntimeError) as failure:
        await client.call_tool("read_context", {})
    diagnostics = (
        caplog.text + json.dumps(client.health_status)
        + "".join(traceback.format_exception(failure.value))
    )
    assert ("private-sentinel" in diagnostics) is not redact
    if redact:
        assert "ConnectionError" in diagnostics
    content = {"content": "Source owns private-sentinel as ordinary content"}
    client.session.call_tool = AsyncMock(return_value=content)
    assert await client.call_tool("read_context", {}) is content


@pytest.mark.asyncio
@pytest.mark.parametrize("redact", [False, True])
async def test_http_timeout_hides_only_transient_endpoint(monkeypatch, redact):
    client = private_client("http", redact)
    closed = asyncio.Event()

    @asynccontextmanager
    async def stalled_connection(*args, **kwargs):
        try:
            await asyncio.Event().wait()
            yield  # pragma: no cover -- handshake never finishes
        finally:
            closed.set()

    monkeypatch.setattr(http, "create_mcp_http_client", stalled_connection)
    monkeypatch.setattr(http, "CONNECTION_TIMEOUT", 0.01)
    with pytest.raises(RuntimeError, match="timed out") as failure:
        await client.connect()
    assert ("private-sentinel" in str(failure.value)) is not redact
    assert closed.is_set()
    assert client._connection_task is None


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["stdio", "http"])
async def test_retained_wrapper_cannot_reopen_disposed_manager_client(monkeypatch, transport):
    client = private_client(transport, True)
    session = SimpleNamespace(call_tool=AsyncMock(return_value=SimpleNamespace(content=[])))
    starts = []

    async def connection():
        starts.append(asyncio.current_task())
        client.session = session
        client._state = stdio.ConnectionState.CONNECTED
        client._connected = True
        client._ready_event.set()
        try:
            await client._shutdown_event.wait()
        finally:
            client.session = None
            client._state = stdio.ConnectionState.DISCONNECTED
            client._connected = False

    monkeypatch.setattr(client, "_connection_task_impl", connection)
    manager = MCPManager({}, SimpleNamespace(hooks=None))
    wrapper = MCPToolWrapper("transient", {"name": "read"}, client, None)
    manager.clients["transient"] = client
    try:
        await client.connect()
        assert (await wrapper.execute({})).success
        # Explicit disconnect remains reusable when its owner has not disposed it.
        await client.disconnect()
        await client.connect()
        assert len(starts) == 2
        await manager.stop()
        result = await wrapper.execute({})
        assert not result.success and "retired" in str(result.error)
        for method, args in (
            (client.connect, ()),
            (client.read_resource, ("context://history",)),
            (client.get_prompt, ("orientation",)),
            (client.set_logging_level, ("info",)),
        ):
            with pytest.raises(RuntimeError, match="retired"):
                await method(*args)
        if transport == "stdio":
            with pytest.raises(RuntimeError, match="retired"):
                await client.connect_with_retry()
            with pytest.raises(RuntimeError, match="retired"):
                await client.call_tool_with_retry("read", {})
        session.call_tool.assert_awaited_once()
        assert len(starts) == 2
        assert all(task.done() for task in starts)
    finally:
        await client.disconnect()
