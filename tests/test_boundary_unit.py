"""Focused boundary regressions; fake sessions here are explicitly unit-only."""

import hashlib
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolResult

from amplifier_module_tool_mcp.client import MCPClient, ConnectionState
from amplifier_module_tool_mcp.config import MCPConfig
from amplifier_module_tool_mcp.evidence import (
    ArtifactCreationError, canonical_json, present_evidence, save_envelope,
)
from amplifier_module_tool_mcp.manager import MCPManager
from amplifier_module_tool_mcp.policy import ToolPolicy, validate_server_policy
from amplifier_module_tool_mcp.sdk_compat import MCPProtocolError
from amplifier_module_tool_mcp.streamable_http_client import MCPStreamableHTTPClient
from amplifier_module_tool_mcp.wrapper import MCPToolWrapper


@pytest.mark.parametrize("transport", ["stdio", "http"])
@pytest.mark.asyncio
async def test_both_clients_enforce_listing_dispatch_and_auxiliary_policy(transport):
    policy = ToolPolicy(names=frozenset({"read"}))
    if transport == "stdio":
        client = MCPClient("unit", "unused", [], tool_policy=policy)
        client._state = ConnectionState.CONNECTED
    else:
        client = MCPStreamableHTTPClient("unit", "https://invalid.invalid", tool_policy=policy)
        client._connected = True
    client.session = SimpleNamespace(
        call_tool=AsyncMock(return_value="allowed"),
        read_resource=AsyncMock(), get_prompt=AsyncMock(),
    )
    client.tools = [{"name": "read"}, {"name": "write"}, {"name": "future_write"}]
    client.resources = [{"uri": "unit://write"}]
    client.prompts = [{"name": "write"}]
    assert client.get_tools() == [{"name": "read"}]
    assert client.get_resources() == client.get_prompts() == []
    arguments = {"nested": {"status": 403}, "unknown": [None, False]}
    assert await client.call_tool("read", arguments) == "allowed"
    client.session.call_tool.assert_awaited_once_with("read", arguments=arguments)
    for name in ("write", "future_write", "unknown"):
        with pytest.raises(PermissionError):
            await client.call_tool(name, {})
    with pytest.raises(PermissionError):
        await client.read_resource("unit://write")
    with pytest.raises(PermissionError):
        await client.get_prompt("write", {})
    client.session.read_resource.assert_not_awaited()
    client.session.get_prompt.assert_not_awaited()
    client.tool_policy = ToolPolicy(names=frozenset())
    assert client.get_tools() == []
    with pytest.raises(PermissionError):
        await client.call_tool("read", {})
    client.retire()
    client.tool_policy = ToolPolicy()
    with pytest.raises(PermissionError):
        await client.connect()
    with pytest.raises(PermissionError):
        await client.call_tool("read", {})
    assert client.session.call_tool.await_count == 1


@pytest.mark.parametrize("transport", ["stdio", "http"])
@pytest.mark.parametrize("operation", ["tool", "resource", "prompt"])
@pytest.mark.parametrize("revocation", ["retirement", "restriction"])
@pytest.mark.asyncio
async def test_revocation_during_connect_rechecked_before_rpc(transport, operation, revocation):
    if transport == "stdio":
        client = MCPClient("unit", "unused", [])
    else:
        client = MCPStreamableHTTPClient("unit", "https://invalid.invalid")
    session = SimpleNamespace(call_tool=AsyncMock(), read_resource=AsyncMock(), get_prompt=AsyncMock())

    async def paused_connect():
        # Equivalent to another task retiring or changing policy while awaiting
        # connection; critical SDK transport coverage is in integration tests.
        client.session = session
        if revocation == "retirement":
            client.retire()
        else:
            client.tool_policy = ToolPolicy(names=frozenset())

    client.connect = paused_connect
    with pytest.raises(PermissionError):
        if operation == "tool":
            await client.call_tool("read", {})
        elif operation == "resource":
            await client.read_resource("unit://read")
        else:
            await client.get_prompt("read", {})
    session.call_tool.assert_not_awaited()
    session.read_resource.assert_not_awaited()
    session.get_prompt.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_retry_entrypoint_never_repeats_protocol_or_policy_failure():
    client = MCPClient("unit", "unused", [])
    client.call_tool = AsyncMock(side_effect=MCPProtocolError(
        "rejected", code=-32602, message="rejected", data={"context": "unit"},
        explanation="invalid params",
    ))
    with pytest.raises(MCPProtocolError):
        await client.call_tool_with_retry("write", {})
    client.call_tool.assert_awaited_once()


@pytest.mark.parametrize("config", [
    {"tools": ["read"], "toolz": ["write"]},
    {"tools": ["read"], "unexpected": True},
    {"tool_allowlist": ["read"]},
    {"permissions": {"tools": ["read"]}},
])
def test_unknown_restriction_keys_do_not_downgrade_to_unrestricted(config):
    with pytest.raises(ValueError, match="Unsupported"):
        validate_server_policy(config)


@pytest.mark.asyncio
async def test_all_policies_validated_before_any_connection(mock_coordinator):
    manager = MCPManager({
        "servers": {
            "first": {"command": "unused", "tools": ["read"]},
            "second": {"command": "unused", "tools": None},
        },
    }, mock_coordinator)
    manager._start_server = AsyncMock()
    with pytest.raises(ValueError):
        await manager.start()
    manager._start_server.assert_not_awaited()


def test_malformed_priority_file_cannot_enable_legacy_fallback(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = tmp_path / "project"
    (home / ".amplifier").mkdir(parents=True)
    (project / ".amplifier").mkdir(parents=True)
    (home / ".amplifier/mcp.json").write_text(
        '{"mcpServers":{"same":{"command":"unused"}}}', encoding="utf-8"
    )
    (project / ".amplifier/mcp.json").write_text(
        '{"mcpServers":{"same":{"command":"unused","tools":["read"]}', encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(home))
    if os.name == "nt":
        monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.chdir(project)
    with pytest.raises(ValueError, match="project MCP config"):
        MCPConfig().load_servers()


@pytest.mark.parametrize("source", ["project", "environment"])
def test_json_null_file_cannot_enable_legacy_fallback(tmp_path, monkeypatch, source):
    home = tmp_path / "home"
    project = tmp_path / "project"
    (home / ".amplifier").mkdir(parents=True)
    (project / ".amplifier").mkdir(parents=True)
    (home / ".amplifier/mcp.json").write_text(
        '{"mcpServers":{"same":{"command":"unused"}}}', encoding="utf-8"
    )
    path = project / ".amplifier/mcp.json" if source == "project" else tmp_path / "env.json"
    path.write_text("null", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    if os.name == "nt":
        monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.chdir(project)
    if source == "environment":
        monkeypatch.setenv("AMPLIFIER_MCP_CONFIG", str(path))
    with pytest.raises(ValueError, match=f"{source} MCP config"):
        MCPConfig().load_servers()


@pytest.mark.parametrize("config", [
    [], {"mcpServers": []}, {"mcpServers": {"server": None}},
])
def test_invalid_config_container_fails_closed(config):
    loader = MCPConfig()
    loader._load_from_user_config = lambda: config
    with pytest.raises(ValueError):
        loader.load_servers()


def test_artifact_collision_does_not_overwrite(tmp_path, monkeypatch):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    existing = directory / ("0" * 48 + ".json")
    existing.write_bytes(b"untouched")
    monkeypatch.setattr("amplifier_module_tool_mcp.evidence.secrets.token_hex", lambda size: "0" * 48)
    with pytest.raises(ArtifactCreationError):
        save_envelope({"text": "new evidence"}, directory)
    assert existing.read_bytes() == b"untouched"
    assert list(directory.iterdir()) == [existing]


def test_windows_missing_dependency_fails_explicitly(tmp_path, monkeypatch):
    # Missing platform dependency is explicit, not a POSIX chmod privacy claim.
    import sys
    monkeypatch.setitem(sys.modules, "win32security", None)
    with monkeypatch.context() as patch:
        patch.setattr("amplifier_module_tool_mcp.evidence.os.name", "nt")
        with pytest.raises(ArtifactCreationError, match="pywin32 is required"):
            save_envelope({"text": "evidence"}, tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX symlink and mode enforcement")
@pytest.mark.parametrize("unsafe", ["symlink", "public-directory"])
def test_artifact_location_must_be_private_not_symlink(tmp_path, unsafe):
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    directory = tmp_path / "artifacts"
    if unsafe == "symlink":
        directory.symlink_to(target, target_is_directory=True)
    else:
        directory.mkdir(mode=0o755)
        directory.chmod(0o755)
    with pytest.raises(ArtifactCreationError):
        save_envelope({"text": "evidence"}, directory)
    assert list(target.iterdir()) == []
    assert list(directory.iterdir()) == []


@pytest.mark.asyncio
async def test_large_error_flag_and_metadata_stay_failed_bounded_recoverable(tmp_path):
    raw = {
        "content": [{"type": "text", "text": "error context",
                     "_meta": {"source": "x" * 90000}}],
        "isError": True, "_meta": {"code": "DOMAIN_CODE", "context": "y" * 90000},
        "structuredContent": {"status": 403, "tail": "kept"},
    }
    result = CallToolResult.model_validate(raw)
    client = SimpleNamespace(call_tool=AsyncMock(return_value=result))
    wrapper = MCPToolWrapper(
        "unit", {"name": "read", "input_schema": {"type": "object"}},
        client, None, max_content_size=1024, artifact_dir=tmp_path / "private",
    )
    adapted = await wrapper.execute({})
    assert not adapted.success
    assert adapted.error["mcp_is_error"] is True
    assert adapted.output["mcp_envelope"] is None
    assert not adapted.output["evidence_complete"]
    recovered = Path(adapted.output["recovery"]["path"]).read_bytes()
    expected = canonical_json(result.model_dump(mode="json", by_alias=True, exclude_none=True)).encode("utf-8")
    assert recovered == expected
    assert adapted.output["recovery"]["sha256"] == hashlib.sha256(expected).hexdigest()
    assert len(adapted.get_serialized_output()) < 2048
    client.call_tool.assert_awaited_once()


def test_multi_content_envelope_retains_annotations_meta_and_nontext(tmp_path):
    raw = {
        "content": [
            {"type": "text", "text": "first", "_meta": {"source": 1}},
            {"type": "text", "text": "second", "annotations": {"priority": 1.0}},
            {"type": "image", "data": "c3ludGhldGlj", "mimeType": "image/png",
             "_meta": {"source": "synthetic"}},
        ], "isError": False, "structuredContent": {"status": 500},
    }
    result = CallToolResult.model_validate(raw)
    expected = result.model_dump(mode="json", by_alias=True, exclude_none=True)
    output = present_evidence(result, "first\nsecond", {"status": 500}, 50000, tmp_path)
    assert output["mcp_envelope"] == expected
    assert output["evidence_complete"]
    assert output["structured_content"] == {"status": 500}
    json.dumps(output)  # No un-serializable SDK model escapes.


@pytest.mark.parametrize("transport", ["stdio", "http"])
@pytest.mark.parametrize("failure_kind", ["early-cancel", "late-cancel", "schema", "transport"])
@pytest.mark.asyncio
async def test_manager_failure_retirement_and_continuation(
    mock_coordinator, monkeypatch, transport, failure_kind,
):
    # Unit-only clients: the real stdio/SDK failure paths are exercised in
    # test_backend_capability. This also pins HTTP manager tracking semantics.
    server = {"command": "unused"} if transport == "stdio" else {"url": "https://invalid.invalid"}
    manager = MCPManager({"servers": {"first": dict(server), "second": dict(server)}}, mock_coordinator)
    clients = []
    failure = (
        asyncio.CancelledError("synthetic cancellation")
        if "cancel" in failure_kind else RuntimeError("synthetic transport failure")
    )

    class PartialClient:
        def __init__(self, name, *args, **kwargs):
            self.name = name
            self.retired = self.closed = False
            clients.append(self)

        async def connect(self):
            assert manager.clients[self.name] is self  # Tracked BEFORE await.
            if self.name == "first" and failure_kind in {"early-cancel", "transport"}:
                raise failure

        def get_tools(self):
            if self.name == "first" and failure_kind == "late-cancel":
                raise failure
            tools = [{"name": "valid", "input_schema": {}}]
            if self.name == "first" and failure_kind == "schema":
                tools.append({"name": "invalid", "input_schema": []})
            return tools

        def get_resources(self):
            return []

        def get_prompts(self):
            return []

        def retire(self):
            self.retired = True

        async def disconnect(self):
            assert self.retired
            self.closed = True

    name = "MCPClient" if transport == "stdio" else "MCPStreamableHTTPClient"
    monkeypatch.setattr(f"amplifier_module_tool_mcp.manager.{name}", PartialClient)
    try:
        if failure_kind == "transport":
            await manager.start()
            assert [client.name for client in clients] == ["first", "second"]
            assert manager.clients == {"second": clients[1]}
            assert set(manager.tools) == {"mcp_second_valid"}
            assert not clients[1].retired
        elif failure_kind == "schema":
            with pytest.raises(ValueError, match="input schema"):
                await manager.start()
        else:
            with pytest.raises(asyncio.CancelledError) as error:
                await manager.start()
            assert error.value is failure
        assert clients[0].retired and clients[0].closed
        if failure_kind != "transport":
            assert len(clients) == 1
            assert manager.clients == {}
            assert manager.get_all_capabilities() == {}
    finally:
        await manager.stop()


@pytest.mark.parametrize("transport", ["stdio", "http"])
@pytest.mark.parametrize("error_type", [asyncio.CancelledError, ValueError])
@pytest.mark.asyncio
async def test_connect_preserves_background_startup_failure_type(transport, error_type):
    client = (
        MCPClient("unit", "unused", []) if transport == "stdio"
        else MCPStreamableHTTPClient("unit", "https://invalid.invalid")
    )
    failure = error_type("synthetic background failure")

    async def failed_background():
        client._connection_error = failure
        client._ready_event.set()

    client._connection_task_impl = failed_background
    with pytest.raises(error_type) as error:
        await client.connect()
    assert error.value is failure
    assert client._connection_task is None


@pytest.mark.parametrize("transport", ["stdio", "http"])
@pytest.mark.asyncio
async def test_stop_drains_partial_client_background_task(mock_coordinator, transport):
    client = (
        MCPClient("unit", "unused", []) if transport == "stdio"
        else MCPStreamableHTTPClient("unit", "https://invalid.invalid")
    )
    started, closed = asyncio.Event(), asyncio.Event()

    async def pending_background():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    client._connection_task_impl = pending_background
    manager = MCPManager({}, mock_coordinator)
    manager.clients["unit"] = client
    connecting = asyncio.create_task(client.connect())
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        background = client._connection_task
        connecting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await connecting
        await asyncio.wait_for(manager.stop(), timeout=2)
        assert closed.is_set() and background.done()
        assert client._retired and client._connection_task is None
        assert manager.clients == {}
        with pytest.raises(PermissionError):
            await client.connect()
    finally:
        connecting.cancel()
        await asyncio.gather(connecting, return_exceptions=True)
        await manager.stop()


@pytest.mark.parametrize("error_type", [asyncio.CancelledError, ValueError])
@pytest.mark.asyncio
async def test_unwinding_cleanup_failure_does_not_replace_original_cause(
    mock_coordinator, monkeypatch, error_type,
):
    from amplifier_module_tool_mcp import mount

    failure = error_type("original failure")
    manager = MCPManager({"servers": {"first": {"command": "unused"}}}, mock_coordinator)
    manager._start_server = AsyncMock(side_effect=failure)
    manager.stop = AsyncMock(side_effect=RuntimeError("cleanup failure"))
    with pytest.raises(error_type) as error:
        await manager.start()
    assert error.value is failure
    manager.stop.assert_awaited_once()

    coordinator = SimpleNamespace(get_capability=lambda name: None)
    monkeypatch.setattr("amplifier_module_tool_mcp.MCPManager", lambda *args: manager)
    with pytest.raises(error_type) as error:
        await mount(coordinator, {})
    assert error.value is failure
    assert manager.stop.await_count == 3  # start unwind, then mount unwind.