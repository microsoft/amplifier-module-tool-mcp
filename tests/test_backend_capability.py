"""Private port checks through real Core loading, coordinator and MCP SDK stdio.

The execution fixture journals requests independently of the connector. Run
with env -i and worktree-owned HOME/TMPDIR/basetemp, as for boundary integration.
"""

from pathlib import Path
import asyncio
import sys

import pytest
from amplifier_core import AmplifierSession, HookResult
from amplifier_core.loader import ModuleLoader

from amplifier_module_tool_mcp import mount
from amplifier_module_tool_mcp.manager import MCPManager, NamespaceCollisionError
from tests.test_boundary_integration import (
    ABSENT, BoundaryRun, assert_denied, boundary, isolated_environment,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(40)]
# Imported fixture reuses the isolation guard, not a mocked critical subject.


async def test_private_approved_handle_policy_and_cleanup(tmp_path: Path):
    async with boundary(
        tmp_path, "private", ["read", "metadata"], expose_tools=False,
        visibility={"enabled": True},
    ) as run:
        assert run.tools == {}
        port = run.coordinator.get_capability("mcp.backends.v1")
        assert port["exposed"] is False
        assert set(port["capabilities"]) == {"mcp_boundary_read", "mcp_boundary_metadata"}
        handle = port["capabilities"]["mcp_boundary_read"]
        assert handle.name == "mcp_boundary_read"
        assert isinstance(handle.description, str)
        assert isinstance(handle.input_schema, dict)
        result = await handle.execute({"source": "synthetic"})
        assert result.success, result.error
        assert run.executed("read") == 1
        metadata = await port["capabilities"]["mcp_boundary_metadata"].execute({})
        assert metadata.success
        assert metadata.output["mcp_envelope"]["_meta"]["provenance"]["source"] == "fixture"
        visibility = await run.coordinator.hooks.emit("provider:request", {})
        assert visibility.action == "continue"
        assert not visibility.context_injection
        for denied in ["write", "unknown"]:
            with pytest.raises(PermissionError):
                await handle.client.call_tool(denied, {})
        assert_denied(run, "write", "unknown")
        # Retaining an approved handle does not bypass a changed dispatch policy.
        approved_policy = handle.client.tool_policy
        handle.client.tool_policy = type(approved_policy)(names=frozenset())
        denied = await handle.execute({})
        assert not denied.success
        assert len(run.requests("tools/call", "read")) == 1
        handle.client.tool_policy = approved_policy
        received = len(run.events("received"))
        pids = run.pids()
        await run.close()
        stale = await handle.execute({})
        assert not stale.success
        assert run.pids() == pids
        assert len(run.events("received")) == received
        print("PRIVATE: visible=0 advertising=0 denied_received=0 stale_received=0 stale_spawned=0")


@pytest.mark.parametrize("exposure", [ABSENT, True, False], ids=["default", "explicit", "private"])
async def test_unrestricted_all_capability_exposure(tmp_path: Path, exposure):
    options = {"visibility": {"enabled": True}}
    if exposure is not ABSENT:
        options["expose_tools"] = exposure
    async with boundary(tmp_path, "all-capabilities", **options) as run:
        port = run.coordinator.get_capability("mcp.backends.v1")
        caps = port["capabilities"]
        assert len(caps) == 13  # 11 tools, one resource and one prompt.
        public = exposure is not False
        assert port["exposed"] is public
        assert set(run.tools) == (set(caps) if public else set())
        if public:
            assert all(run.tools[name] is handle for name, handle in caps.items())
        visibility = await run.coordinator.hooks.emit("provider:request", {})
        assert visibility.action == ("inject_context" if public else "continue")
        result = await caps["mcp_boundary_read"].execute({})
        assert result.success
        assert run.executed("read") == 1
        print(f"EXPOSURE: exposed={public} visible={len(run.tools)} handles={len(caps)}")


@pytest.mark.parametrize("invalid", [None, 0, 1, "false", [], {}])
async def test_invalid_exposure_does_not_spawn(tmp_path: Path, invalid):
    run = BoundaryRun(tmp_path / "invalid-exposure", expose_tools=invalid)
    try:
        with pytest.raises(ValueError, match="expose_tools"):
            await mount(run.coordinator, run.config)
        assert run.pids() == []
        assert run.events() == []
        assert run.tools == {}
        assert run.coordinator.get_capability("mcp.backends.v1") is None
        print(f"INVALID expose_tools={invalid!r}: spawned=0 received=0")
    finally:
        await run.close()


@pytest.mark.parametrize("existing", [{"unrelated": True}, {"exposed": False, "capabilities": {}}])
async def test_existing_port_is_not_overwritten_or_started(tmp_path: Path, existing):
    run = BoundaryRun(tmp_path / "port-collision", ["read"], expose_tools=False)
    run.coordinator.register_capability("mcp.backends.v1", existing)
    try:
        with pytest.raises(NamespaceCollisionError, match="mcp.backends.v1"):
            await mount(run.coordinator, run.config)
        assert run.coordinator.get_capability("mcp.backends.v1") == existing
        assert run.tools == {}
        assert run.pids() == []
        print("PORT COLLISION: preserved=True visible=0 spawned=0")
    finally:
        await run.close()


async def test_private_mode_preserves_host_namespace_guard(tmp_path: Path):
    run = BoundaryRun(tmp_path / "name-collision", ["read"], expose_tools=False)

    class ExistingTool:
        name = "mcp_boundary_read"
        description = "Host-owned fixture"
        input_schema = {"type": "object"}

        async def execute(self, arguments):
            raise AssertionError("Host tool must not be invoked")

    original = ExistingTool()
    await run.coordinator.mount("tools", original, name=original.name)
    try:
        with pytest.raises(NamespaceCollisionError, match="already mounted"):
            await mount(run.coordinator, run.config)
        assert run.tools[original.name] is original
        assert run.coordinator.get_capability("mcp.backends.v1") is None
        assert run.requests("tools/call") == []
        assert run.pids()
    finally:
        await run.close()


@pytest.mark.parametrize("consumer", ["absent", "missing", "collision", "working"])
async def test_core_session_loader_failure_cannot_expose_backend(tmp_path: Path, monkeypatch, consumer):
    """Core really continues on consumer load/mount failure; MCP stays private."""
    run = BoundaryRun(tmp_path / f"session-{consumer}", ["read"], expose_tools=False,
                      visibility={"enabled": True})
    module_dir = tmp_path / "modules"
    module_dir.mkdir()
    scaffold = """
async def mount(coordinator, config):
    await coordinator.mount(config["slot"], object())
"""
    policy = """
class Facade:
    name = "policy_read"
    description = "Synthetic policy facade"
    input_schema = {"type": "object"}

    def __init__(self, backend):
        self.backend = backend

    async def execute(self, arguments):
        return await self.backend.execute(arguments)

async def mount(coordinator, config):
    port = coordinator.get_capability("mcp.backends.v1")
    assert port["exposed"] is False
    handle = port["capabilities"]["mcp_boundary_read"]
    if "policy_read" in (coordinator.get("tools") or {}):
        raise ValueError("Consumer namespace collision: policy_read")
    await coordinator.mount("tools", Facade(handle), name="policy_read")
"""
    conflict = """
async def mount(coordinator, config):
    coordinator.register_capability("fixture.conflict", "host-owned")
    await coordinator.mount("tools", object(), name="policy_read")
"""
    for name, source in [
        ("backend_scaffold", scaffold), ("backend_consumer", policy),
        ("backend_conflict", conflict),
    ]:
        (module_dir / f"amplifier_module_{name}.py").write_text(source)
        monkeypatch.delitem(sys.modules, f"amplifier_module_{name}", raising=False)
    monkeypatch.syspath_prepend(str(module_dir))
    loader = ModuleLoader()
    config = {
        "session": {
            "orchestrator": {"module": "backend_scaffold", "config": {"slot": "orchestrator"}},
            "context": {"module": "backend_scaffold", "config": {"slot": "context"}},
        },
        "tools": [{"module": "tool-mcp", "config": run.config}],
    }
    if consumer == "collision":
        config["tools"].append({"module": "backend_conflict"})
    if consumer != "absent":
        config["tools"].append({
            "module": "backend_missing_consumer" if consumer == "missing" else "backend_consumer",
        })
    session = AmplifierSession(config, loader=loader)
    run.coordinator = session.coordinator
    failures = []

    async def failure_event(event, data):
        failures.append(data)
        return HookResult(action="continue")

    run.coordinator.hooks.register("module:load_failed", failure_event, name="test-failures")
    try:
        await session.initialize()
        run.cleanup = session.cleanup
        assert run.pids(), "Real module loader did not start SDK fixture"
        assert run.coordinator.get_capability("mcp.backends.v1")["exposed"] is False
        assert not any(name.startswith("mcp_") for name in run.tools)
        assert len(failures) == (1 if consumer in {"missing", "collision"} else 0)
        if consumer == "working":
            assert set(run.tools) == {"policy_read"}
            result = await run.tools["policy_read"].execute({"grounded": "synthetic"})
            assert result.success
            assert run.executed("read") == 1
        elif consumer == "collision":
            assert set(run.tools) == {"policy_read"}
            assert run.coordinator.get_capability("fixture.conflict") == "host-owned"
            assert not hasattr(run.tools["policy_read"], "execute")
            assert run.requests("tools/call") == []
        else:
            assert run.tools == {}
            assert run.requests("tools/call") == []
        print(f"CORE SESSION: consumer={consumer} raw_visible=0 failures={len(failures)}")
    finally:
        # Register cleanup before assertions too, so failures cannot leak.
        run.cleanup = session.cleanup
        await run.close()


@pytest.mark.parametrize("invalid", [None, [], "enabled"])
@pytest.mark.parametrize("exposure", [True, False])
async def test_invalid_visibility_fails_before_spawn(tmp_path: Path, invalid, exposure):
    run = BoundaryRun(tmp_path / "invalid-visibility", ["read"],
                      visibility=invalid, expose_tools=exposure)
    try:
        with pytest.raises(ValueError, match="visibility"):
            await mount(run.coordinator, run.config)
        assert run.pids() == []
        assert run.events() == []
        assert run.tools == {}
        assert run.coordinator.get_capability("mcp.backends.v1") is None
    finally:
        await run.close()


async def assert_retired_without_rpc(run, handles):
    pids, received = run.pids(), len(run.events("received"))
    for handle in handles:
        assert handle.client._retired
        assert not handle.client.is_connected
        assert handle.client._connection_task is None
        assert handle.client.session is None
        result = await handle.execute({})
        assert not result.success
        with pytest.raises(PermissionError):
            await handle.client.connect()
    assert run.pids() == pids
    assert len(run.events("received")) == received
    assert run.requests("tools/call") == []
    assert all(not run.pid_present(pid) for pid in pids)
    assert run.coordinator.get_capability("mcp.backends.v1") is None


async def test_real_late_hook_registration_failure_retires_mounted_handles(tmp_path: Path):
    # Rust Core's register rejects the non-integer priority after real SDK
    # startup/tool mounting. No mocked server, transport or hook registry.
    run = BoundaryRun(tmp_path / "late-hook-failure", ["read"],
                      visibility={"enabled": True, "priority": "not-an-integer"})
    try:
        with pytest.raises(TypeError):
            await mount(run.coordinator, run.config)
        assert len(run.pids()) == 1
        assert len(run.requests("tools/list")) == 1
        assert set(run.tools) == {"mcp_boundary_read"}  # Not a rollback promise.
        await assert_retired_without_rpc(run, list(run.tools.values()))
        visibility = await run.coordinator.hooks.emit("provider:request", {})
        assert visibility.action == "continue"
    finally:
        await run.close()


async def test_core_mount_cancellation_preserves_cause_and_retires_handles(tmp_path: Path):
    run = BoundaryRun(tmp_path / "cancel-core-mount", ["read"])
    cancelled = asyncio.CancelledError("synthetic registration cancellation")

    class CancelAfterCoreMount:
        def __getattr__(self, name):
            return getattr(run.coordinator, name)

        async def mount(self, *args, **kwargs):
            await run.coordinator.mount(*args, **kwargs)
            raise cancelled

    try:
        with pytest.raises(asyncio.CancelledError) as error:
            await mount(CancelAfterCoreMount(), run.config)
        assert error.value is cancelled
        assert len(run.pids()) == 1
        assert set(run.tools) == {"mcp_boundary_read"}
        await assert_retired_without_rpc(run, list(run.tools.values()))
    finally:
        await run.close()


@pytest.mark.parametrize("phase", ["startup", "registration"])
async def test_real_stdio_cancellation_stops_before_next_server(tmp_path: Path, monkeypatch, phase):
    run = BoundaryRun(tmp_path / f"cancel-{phase}", ["read"])
    run.config["servers"]["never"] = dict(run.config["servers"]["boundary"])
    manager = MCPManager(run.config, run.coordinator)
    # Capture the real manager/clients; only the late-registration cancellation
    # seam is synthetic. All subprocess/SDK lifecycle operations remain real.
    monkeypatch.setattr("amplifier_module_tool_mcp.MCPManager", lambda *args: manager)
    retained = []
    clients = []
    cancelled = asyncio.CancelledError("synthetic catalogue cancellation")
    if phase == "startup":
        run.state.write_text('{"revision":1,"pause_startup":true}', encoding="utf-8")
    else:
        original = manager._register_capabilities

        async def cancel_registration(name, client):
            clients.append(client)
            await original(name, client)
            retained.extend(manager.get_all_capabilities().values())
            raise cancelled

        monkeypatch.setattr(manager, "_register_capabilities", cancel_registration)
    task = asyncio.create_task(mount(run.coordinator, run.config))
    try:
        if phase == "startup":
            for _ in range(500):
                if run.events("startup_paused"):
                    break
                assert not task.done(), "Mount ended before the startup cancellation gate"
                await asyncio.sleep(0.01)
            assert run.events("startup_paused"), "Fixture never reached the pending handshake"
            clients.append(manager.clients["boundary"])
            connection_task = clients[0]._connection_task
            assert connection_task is not None
            task.cancel("synthetic startup cancellation")
        with pytest.raises(asyncio.CancelledError) as error:
            await asyncio.wait_for(task, timeout=10)
        if phase == "registration":
            assert error.value is cancelled
        else:
            assert error.value.args == ("synthetic startup cancellation",)
            assert connection_task.done()
        assert len(run.pids()) == 1  # The following configured server never starts.
        assert manager.clients == {}
        assert manager.get_all_capabilities() == {}
        assert run.tools == {}
        for client in clients:
            assert client._retired and client.session is None
            assert client._connection_task is None
            with pytest.raises(PermissionError):
                await client.connect()
        await assert_retired_without_rpc(run, retained)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await run.close()


async def test_real_nonobject_input_schema_fails_mount_without_next_server(tmp_path: Path, monkeypatch):
    run = BoundaryRun(tmp_path / "invalid-schema", ["read"])
    run.config["servers"]["never"] = dict(run.config["servers"]["boundary"])
    run.state.write_text('{"revision":1,"invalid_input_schema":true}', encoding="utf-8")
    manager = MCPManager(run.config, run.coordinator)
    monkeypatch.setattr("amplifier_module_tool_mcp.MCPManager", lambda *args: manager)
    clients = []
    original_start = manager._start_server

    async def capture_client(name, config):
        try:
            await original_start(name, config)
        finally:
            if name in manager.clients:
                clients.append(manager.clients[name])

    monkeypatch.setattr(manager, "_start_server", capture_client)
    try:
        with pytest.raises(ValueError):
            await mount(run.coordinator, run.config)
        assert len(run.pids()) == 1
        assert len(clients) == 1
        assert clients[0]._retired
        assert clients[0]._connection_task is None and clients[0].session is None
        assert manager.clients == {}
        assert manager.get_all_capabilities() == {}
        assert run.tools == {}
        await assert_retired_without_rpc(run, [])
    finally:
        await run.close()