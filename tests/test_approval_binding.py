"""Canonical connection lifecycle and invocation guards; no network or model."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from amplifier_module_tool_mcp import client as stdio
from amplifier_module_tool_mcp import streamable_http_client as http


class Session:
    def __init__(self):
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return SimpleNamespace(content=[])


@pytest_asyncio.fixture
async def connected(request, monkeypatch):
    session, opened, tasks = Session(), {}, []
    module = stdio
    client = stdio.MCPClient('fixture', 'fixture-command', ['argument'], env={'EXPLICIT': 'one'})
    monkeypatch.setattr(module, 'ClientSession', lambda *args, **kwargs: session)
    monkeypatch.setattr(module, 'negotiate', AsyncMock(return_value='2026-07-28'))
    client._discover_capabilities = AsyncMock()

    @asynccontextmanager
    async def stdio_transport(params, **kwargs):
        opened['params'] = params
        yield object(), object()

    client.verbose_servers = True
    monkeypatch.setattr(stdio, 'stdio_client', stdio_transport)
    task = asyncio.create_task(client._connection_task_impl())
    tasks.append(task)
    try:
        await asyncio.wait_for(client._ready_event.wait(), 2)
        assert client.is_connected
        yield SimpleNamespace(client=client, session=session, opened=opened)
    finally:
        client._shutdown_event.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 2)


@pytest.mark.asyncio
async def test_actual_lifecycle_captures_binding_without_secret_output(connected):
    client = connected.client
    binding = client.approval_binding()
    assert set(binding) == {'version', 'key'}
    assert len(binding['key']) == 64
    assert 'fixture-secret' not in repr(binding)
    with client.approval_scope(binding, 'write', {'value': 1}):
        await client.call_tool('write', {'value': 1})
        with pytest.raises(ValueError, match='changed'):
            await client.call_tool('write', {'value': 1})
    assert len(connected.session.calls) == 1


@pytest.mark.asyncio
async def test_configuration_mutation_cannot_authorize_current_connection(connected):
    client = connected.client
    with client.approval_scope(client.approval_binding(), 'write', {'value': 1}):
        client.env['EXPLICIT'] = 'changed'
        with pytest.raises(ValueError, match='no longer current'):
            await client.call_tool('write', {'value': 1})
    assert not connected.session.calls


@pytest.mark.asyncio
async def test_disconnect_refuses_before_reconnect_or_effect(connected):
    client = connected.client
    reconnect = client.connect = AsyncMock()
    with client.approval_scope(client.approval_binding(), 'write', {'value': 1}):
        client.session = None
        with pytest.raises(ValueError, match='changed'):
            await client.call_tool('write', {'value': 1})
    reconnect.assert_not_awaited()
    assert not connected.session.calls


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['tool', 'arguments'])
async def test_bound_invocation_is_exact(connected, change):
    client = connected.client
    with client.approval_scope(client.approval_binding(), 'write', {'value': 1}):
        with pytest.raises(ValueError, match='changed'):
            await client.call_tool('other' if change == 'tool' else 'write',
                                   {'value': 2 if change == 'arguments' else 1})
    assert not connected.session.calls


@pytest.mark.asyncio
@pytest.mark.parametrize('arguments', [{'value': (1, 2)}, {1: 'not-a-string-key'}])
async def test_non_json_arguments_cannot_create_a_scope(connected, arguments):
    client = connected.client
    with pytest.raises(ValueError, match='exact JSON'):
        with client.approval_scope(client.approval_binding(), 'write', arguments):
            pytest.fail('Non-JSON arguments acquired an approval scope')
    assert not connected.session.calls


@pytest.mark.asyncio
async def test_scope_expiry_reaches_inherited_task_context(connected):
    client = connected.client
    release = asyncio.Event()
    async def deferred():
        await release.wait()
        await client.call_tool('write', {'value': 1})
    with client.approval_scope(client.approval_binding(), 'write', {'value': 1}):
        task = asyncio.create_task(deferred())
    release.set()
    with pytest.raises(ValueError, match='changed'):
        await task
    assert not connected.session.calls


@pytest.mark.asyncio
async def test_parent_environment_change_does_not_relabel_open_connection(connected, monkeypatch):
    binding = connected.client.approval_binding()
    monkeypatch.setenv('MCP_APPROVAL_FIXTURE', 'different-after-connect')
    assert connected.client.approval_binding() == binding


@pytest.mark.asyncio
async def test_http_has_no_reusable_api_and_retains_ordinary_calls():
    client = http.MCPStreamableHTTPClient('fixture', 'https://fixture.invalid/mcp')
    client.session, client._connected = Session(), True
    with pytest.raises(ValueError, match='no send authorization guard'):
        client.approval_binding()
    await client.call_tool('read', {})
    assert client.session.calls == [('read', {})]


@pytest.mark.asyncio
async def test_ordinary_calls_do_not_require_approval_policy(connected):
    await connected.client.call_tool('read', {})
    assert connected.session.calls == [('read', {})]
