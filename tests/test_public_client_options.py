"""Optional host APIs: inert local peers and direct SDK-admission controls."""

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from amplifier_module_tool_mcp import client as stdio
from amplifier_module_tool_mcp import streamable_http_client as http
from amplifier_module_tool_mcp.approval_binding import ConnectionBinding, digest

pytestmark = pytest.mark.timeout(20)


def make_client(transport, **kwargs):
    if transport == "stdio":
        return stdio.MCPClient("fixture", "inert", [], verbose_servers=True, **kwargs)
    return http.MCPStreamableHTTPClient(
        "fixture", "http://fixture.invalid/mcp", **kwargs
    )


def set_connected(client, session):
    client.session = session
    if isinstance(client, stdio.MCPClient):
        client._state = stdio.ConnectionState.CONNECTED
    else:
        client._connected = True


@pytest.mark.parametrize("transport", ["stdio", "http"])
@pytest.mark.parametrize(
    "options",
    [
        {"discovery": "other"},
        {"discovery": []},
        *[
            {"read_timeout_seconds": value}
            for value in (0, -1, True, "1", float("inf"), float("nan"))
        ],
    ],
)
def test_invalid_options_refused_at_construction(transport, options):
    with pytest.raises(ValueError):
        make_client(transport, **options)


def test_cwd_and_inherit_validation():
    with pytest.raises(ValueError, match="absolute"):
        make_client("stdio", cwd="relative")
    with pytest.raises(ValueError, match="boolean"):
        make_client("stdio", inherit_environment=0)


@pytest.mark.parametrize("transport", ["stdio", "http"])
@pytest.mark.asyncio
async def test_no_reconnect_unscoped_drop_and_stable_result(transport):
    c = make_client(transport)
    result = object()
    session = SimpleNamespace(call_tool=AsyncMock(return_value=result))
    set_connected(c, session)
    c.connect = AsyncMock()
    assert await c.call_tool("report", {}, reconnect=False) is result
    assert session.call_tool.await_count == 1
    c.session = None  # Drop after a caller's earlier is_connected check.
    with pytest.raises(ValueError, match="connection changed"):
        await c.call_tool("report", {}, reconnect=False)
    c.connect.assert_not_awaited()
    assert session.call_tool.await_count == 1


@pytest.mark.parametrize("transport", ["stdio", "http"])
@pytest.mark.asyncio
async def test_no_reconnect_rechecks_session_after_owner_predicate(transport):
    c = make_client(transport)
    original = SimpleNamespace(call_tool=AsyncMock())
    replacement = SimpleNamespace(call_tool=AsyncMock())
    set_connected(c, original)
    c.connect = AsyncMock()
    c.approval_binding = lambda: {"version": 1, "key": "fixture"}
    checks = 0

    def owner():
        nonlocal checks
        checks += 1
        if checks == 3:  # The final consume check, with no intervening await.
            c.session = replacement
        return True

    with c.approval_scope(c.approval_binding(), "report", {}, is_current=owner):
        with pytest.raises(ValueError, match="changed"):
            await c.call_tool("report", {}, reconnect=False)
    c.connect.assert_not_awaited()
    original.call_tool.assert_not_awaited()
    replacement.call_tool.assert_not_awaited()


@pytest.mark.parametrize("transport", ["stdio", "http"])
@pytest.mark.asyncio
async def test_default_call_still_connects_and_returns_raw_result(transport):
    c = make_client(transport)
    result = object()
    session = SimpleNamespace(call_tool=AsyncMock(return_value=result))

    async def connect():
        set_connected(c, session)

    c.connect = AsyncMock(side_effect=connect)
    assert await c.call_tool("report", {}) is result
    c.connect.assert_awaited_once()
    session.call_tool.assert_awaited_once()


@pytest.mark.parametrize("transport", ["stdio", "http"])
@pytest.mark.parametrize("discovery", ["all", "tools"])
@pytest.mark.parametrize("timeout", [None, 120.0])
@pytest.mark.asyncio
async def test_connection_settings_reach_sdk_without_default_override(
    transport,
    discovery,
    timeout,
    monkeypatch,
):
    module = stdio if transport == "stdio" else http
    captured, calls = {}, []
    c = make_client(transport, discovery=discovery, read_timeout_seconds=timeout)

    @asynccontextmanager
    async def session(*args, **kwargs):
        captured.update(kwargs)
        yield SimpleNamespace()

    @asynccontextmanager
    async def streams(*args, **kwargs):
        yield object(), object()

    @asynccontextmanager
    async def http_client(**kwargs):
        yield SimpleNamespace(event_hooks={"request": []})

    async def discover(name):
        calls.append(name)
        return []

    monkeypatch.setattr(module, "ClientSession", session)
    monkeypatch.setattr(module, "negotiate", AsyncMock(return_value="2026-07-28"))
    for name in ("tools", "resources", "prompts"):

        async def discover_one(*args, _name=name, **kwargs):
            return await discover(_name)

        monkeypatch.setattr(module, "discover_" + name, discover_one)
    if transport == "stdio":
        monkeypatch.setattr(module, "stdio_client", streams)
    else:
        monkeypatch.setattr(module, "create_mcp_http_client", http_client)
        monkeypatch.setattr(module, "streamable_http_client", streams)
    await asyncio.wait_for(c.connect(), 2)
    try:
        assert calls == (
            ["tools"] if discovery == "tools" else ["tools", "resources", "prompts"]
        )
        if timeout is None:
            assert "read_timeout_seconds" not in captured
        else:
            from importlib.metadata import version

            expected = (
                timedelta(seconds=timeout)
                if version("mcp").startswith("1.")
                else timeout
            )
            assert captured["read_timeout_seconds"] == expected
    finally:
        await asyncio.wait_for(c.disconnect(), 2)


@pytest.mark.asyncio
async def test_actual_stdio_isolated_cwd_environment_and_paginated_tools(
    tmp_path, monkeypatch
):
    peer = Path(__file__).parent / "fixtures" / "stdio_host_options.py"
    original_cwd = os.getcwd()
    monkeypatch.setenv("MCP_TEST_LATE", "parent-after-construction")
    monkeypatch.setenv("TERM", "synthetic-parent-term")
    clients = []
    try:
        for name in ("one", "two"):
            cwd = tmp_path / name
            cwd.mkdir()
            supplied = {"MCP_TEST_SENTINEL": name}
            c = stdio.MCPClient(
                name,
                sys.executable,
                ["-I", str(peer)],
                env=supplied,
                cwd=cwd,
                inherit_environment=False,
                discovery="tools",
                read_timeout_seconds=2,
                verbose_servers=True,
            )
            supplied["MCP_TEST_SENTINEL"] = "mutated-caller-map"
            clients.append((c, cwd, name))
        await asyncio.wait_for(asyncio.gather(*(c.connect() for c, _, _ in clients)), 5)
        for c, cwd, name in clients:
            with c.approval_scope(
                c.approval_binding(), "report", {}, is_current=lambda: True
            ):
                result = await c.call_tool("report", {}, reconnect=False)
            report = json.loads(result.content[0].text)
            assert report["cwd"] == str(cwd)
            assert report["sentinel"] == name
            assert report["late"] is None
            assert report["term"] == ""
            assert report["calls"].count("tools/list") == 2
            assert (
                "resources/list" not in report["calls"]
                and "prompts/list" not in report["calls"]
            )
            assert [tool["name"] for tool in c.get_tools()] == ["report", "second"]
        assert os.getcwd() == original_cwd
        assert os.environ["TERM"] == "synthetic-parent-term"
    finally:
        await asyncio.wait_for(
            asyncio.gather(*(c.disconnect() for c, _, _ in clients)), 5
        )


def test_default_configuration_and_binding_fingerprint_unchanged():
    c = make_client("stdio", env={"SYNTHETIC": "one"})
    configuration = {
        "server": "fixture",
        "command": "inert",
        "arguments": [],
        "environment": {"SYNTHETIC": "one"},
    }
    assert c._approval_configuration() == configuration
    session = object()
    set_connected(c, session)
    c._protocol_version = "2026-07-28"
    facts = {
        "transport": "stdio",
        "server": "fixture",
        "command": "inert",
        "arguments": [],
        "environment": {"SYNTHETIC": "one"},
        "cwd": "/synthetic",
        "protocol": "2026-07-28",
    }
    c._approval_connection = ConnectionBinding(c, session, configuration, facts)
    assert c.approval_binding() == {"version": 1, "key": digest(facts)}
    h = make_client("http")
    assert h._approval_configuration() == {
        "server": "fixture",
        "url": "http://fixture.invalid/mcp",
        "headers": {},
    }


@pytest.mark.asyncio
async def test_default_stdio_uses_connection_time_environment_and_old_facts(
    monkeypatch,
):
    captured = {}
    # Synthetic maps only: a failed assertion can never dump the host environment.
    parent = {"MCP_TEST_PARENT": "before"}
    monkeypatch.setattr(stdio.os, "environ", parent)
    monkeypatch.setattr(stdio.os, "getcwd", lambda: "/synthetic/cwd")
    c = make_client("stdio", env={"MCP_TEST_EXPLICIT": "fixed"})
    parent["MCP_TEST_PARENT"] = "at-connect"

    @asynccontextmanager
    async def streams(params, **kwargs):
        captured["params"] = params
        yield object(), object()

    @asynccontextmanager
    async def session(*args, **kwargs):
        yield object()

    monkeypatch.setattr(stdio, "stdio_client", streams)
    monkeypatch.setattr(stdio, "ClientSession", session)
    monkeypatch.setattr(stdio, "negotiate", AsyncMock(return_value="2026-07-28"))
    c._discover_capabilities = AsyncMock()
    await asyncio.wait_for(c.connect(), 2)
    try:
        expected_env = {"MCP_TEST_PARENT": "at-connect", "MCP_TEST_EXPLICIT": "fixed"}
        assert captured["params"].env == expected_env
        assert captured["params"].cwd == "/synthetic/cwd"
        assert c.approval_binding() == {
            "version": 1,
            "key": digest(
                {
                    "transport": "stdio",
                    "server": "fixture",
                    "command": "inert",
                    "arguments": [],
                    "environment": expected_env,
                    "cwd": "/synthetic/cwd",
                    "protocol": "2026-07-28",
                }
            ),
        }
    finally:
        await asyncio.wait_for(c.disconnect(), 2)


@pytest.mark.asyncio
async def test_actual_stdio_read_timeout_does_not_resend(tmp_path):
    peer = Path(__file__).parent / "fixtures" / "stdio_host_options.py"
    c = stdio.MCPClient(
        "timeout",
        sys.executable,
        ["-I", str(peer)],
        cwd=tmp_path,
        inherit_environment=False,
        discovery="tools",
        read_timeout_seconds=0.1,
        verbose_servers=True,
    )
    await asyncio.wait_for(c.connect(), 3)
    try:
        with pytest.raises(RuntimeError, match="(?i)timed? ?out|timeout"):
            await asyncio.wait_for(c.call_tool("hang", {}, reconnect=False), 2)
        result = await c.call_tool("report", {}, reconnect=False)
        report = json.loads(result.content[0].text)
        assert report["calls"].count("tools/call") == 2  # One hang, one report.
    finally:
        await asyncio.wait_for(c.disconnect(), 3)
