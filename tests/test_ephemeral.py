"""Trusted, worker-lifetime MCP attachments through the canonical mount."""

import asyncio
import copy

import pytest
from amplifier_core import ModuleCoordinator

import amplifier_module_tool_mcp as module
from amplifier_module_tool_mcp.manager import MCPManager


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    home, project = tmp_path / "home", tmp_path / "project"
    home.mkdir()
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("AMPLIFIER_MCP_CONFIG", raising=False)
    monkeypatch.chdir(project)


@pytest.fixture
def clients(monkeypatch):
    created = []

    class Client:
        def __init__(self, name, *args, **kwargs):
            self.name = name
            self.args = args
            self.kwargs = kwargs
            self.disconnected = False
            self.definitions = [{"name": "read", "input_schema": {}}]
            self.failure = None
            created.append(self)

        async def connect(self):
            if self.failure:
                raise self.failure

        async def disconnect(self):
            self.disconnected = True

        def get_tools(self):
            return self.definitions

        def get_resources(self):
            return []

        def get_prompts(self):
            return []

    monkeypatch.setattr("amplifier_module_tool_mcp.manager.MCPClient", Client)
    monkeypatch.setattr("amplifier_module_tool_mcp.manager.MCPStreamableHTTPClient", Client)
    return created, Client


def supplement():
    return {"context": {"type": "http", "url": "https://context.invalid/mcp",
                        "headers": {"Authorization": "Bearer synthetic-private"}}}


@pytest.mark.asyncio
async def test_mount_uses_one_manager_without_persisting_supplement(clients, monkeypatch):
    created, _ = clients
    managers = []

    def manager_factory(config, coordinator):
        manager = MCPManager(config, coordinator)
        managers.append(manager)
        return manager

    monkeypatch.setattr(module, "MCPManager", manager_factory)
    coordinator = ModuleCoordinator()
    config = {"servers": {"existing": {"command": "synthetic"}},
              "visibility": {"enabled": False}}
    before = copy.deepcopy(config)
    coordinator.register_capability("mcp.ephemeral_servers", supplement())
    cleanup = await module.mount(coordinator, config)
    assert len(managers) == 1
    assert set(coordinator.get("tools")) == {"mcp_existing_read", "mcp_context_read"}
    assert coordinator.get_capability("mcp.ephemeral_ready") == ["context"]
    assert coordinator.get_capability("mcp.ephemeral_servers") is None
    assert managers[0].config.inline_config == before == config
    assert [c.kwargs.get("redact_connection_details", False) for c in created] == [False, True]
    await cleanup()
    assert all(c.disconnected for c in created)
    assert not coordinator.get("tools")
    assert coordinator.get_capability("mcp.ephemeral_ready") is None


@pytest.mark.asyncio
async def test_name_collision_refuses_before_any_connection_even_if_configured_is_invalid(clients):
    created, _ = clients
    coordinator = ModuleCoordinator()
    coordinator.register_capability("mcp.ephemeral_servers", supplement())
    with pytest.raises(ValueError, match="conflict"):
        await module.mount(coordinator, {"servers": {"context": {"command": ""}}})
    assert created == []
    assert coordinator.get_capability("mcp.ephemeral_ready") is None
    assert coordinator.get_capability("mcp.ephemeral_servers") is None


@pytest.mark.asyncio
async def test_failed_requested_connection_rolls_back_existing_connections(clients, monkeypatch):
    created, Client = clients
    connect = Client.connect

    async def fail_last(client):
        if client.name == "context":
            raise RuntimeError("https://private.invalid/?synthetic-private")
        await connect(client)

    monkeypatch.setattr(Client, "connect", fail_last)
    coordinator = ModuleCoordinator()
    coordinator.register_capability("mcp.ephemeral_servers", supplement())
    with pytest.raises(RuntimeError, match="Ephemeral MCP attachment failed") as error:
        await module.mount(coordinator, {"servers": {"existing": {"command": "synthetic"}}})
    assert "synthetic-private" not in str(error.value)
    assert len(created) == 2 and all(c.disconnected for c in created)
    assert not coordinator.get("tools")
    assert coordinator.get_capability("mcp.ephemeral_ready") is None


@pytest.mark.asyncio
async def test_configured_failure_remains_best_effort(clients, monkeypatch):
    created, Client = clients

    async def connect(client):
        if client.name == "existing":
            raise RuntimeError("ordinary configured failure")

    monkeypatch.setattr(Client, "connect", connect)
    coordinator = ModuleCoordinator()
    coordinator.register_capability("mcp.ephemeral_servers", supplement())
    cleanup = await module.mount(coordinator, {
        "servers": {"existing": {"command": "synthetic"}},
        "visibility": {"enabled": False},
    })
    assert created[0].disconnected
    assert set(coordinator.get("tools")) == {"mcp_context_read"}
    assert coordinator.get_capability("mcp.ephemeral_ready") == ["context"]
    await cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("collision", ["sanitized", "server_prefix", "primitive"])
async def test_final_names_cannot_overwrite_capabilities(clients, monkeypatch, collision):
    created, Client = clients

    async def connect(client):
        if collision == "sanitized":
            client.definitions = [{"name": "read.path"}, {"name": "read/path"}]
        elif collision == "server_prefix":
            client.definitions = [{"name": "b_read" if client.name == "a" else "read"}]
        else:
            client.definitions = [{"name": "prompt_read"}]

    monkeypatch.setattr(Client, "connect", connect)
    if collision == "primitive":
        monkeypatch.setattr(Client, "get_prompts", lambda self: [{"name": "read"}])
    coordinator = ModuleCoordinator()
    configs = {name: {"command": "synthetic"} for name in ("a", "a_b")}
    coordinator.register_capability("mcp.ephemeral_servers", configs)
    with pytest.raises(RuntimeError, match="attachment failed"):
        await module.mount(coordinator)
    assert created and all(c.disconnected for c in created)
    assert not coordinator.get("tools")
    assert coordinator.get_capability("mcp.ephemeral_ready") is None


@pytest.mark.asyncio
async def test_existing_tool_is_preserved_when_attachment_conflicts(clients):
    created, Client = clients
    coordinator = ModuleCoordinator()
    from amplifier_module_tool_mcp.wrapper import MCPToolWrapper
    original = MCPToolWrapper("context", {"name": "read"}, Client("prior"), coordinator.hooks)
    await coordinator.mount("tools", original, name=original.name)
    coordinator.register_capability("mcp.ephemeral_servers", supplement())
    with pytest.raises(ValueError, match="mounted tools"):
        await module.mount(coordinator)
    assert coordinator.get("tools", original.name) is original
    assert created[-1].disconnected
    assert not created[0].disconnected


@pytest.mark.asyncio
async def test_partial_coordinator_mount_is_rolled_back(clients):
    created, _ = clients
    real = ModuleCoordinator()

    class FailingCoordinator:
        def __getattr__(self, name):
            return getattr(real, name)

        async def mount(self, point, value, *, name):
            await real.mount(point, value, name=name)
            if name == "mcp_context_read":
                raise RuntimeError("coordinator mount failed")

    real.register_capability("mcp.ephemeral_servers", supplement())
    with pytest.raises(RuntimeError, match="coordinator mount failed"):
        await module.mount(FailingCoordinator(), {
            "servers": {"existing": {"command": "synthetic"}}})
    assert all(c.disconnected for c in created)
    assert not real.get("tools")
    assert real.get_capability("mcp.ephemeral_ready") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("ephemeral", [False, True])
async def test_manager_propagates_cancel_and_disposes_partial_start(clients, monkeypatch, ephemeral):
    created, Client = clients
    entered = asyncio.Event()

    async def connect(client):
        if client.name == "context":
            entered.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(Client, "connect", connect)
    servers = {"existing": {"command": "synthetic"}}
    if not ephemeral:
        servers.update(supplement())
    manager = MCPManager({"servers": servers}, ModuleCoordinator())
    task = asyncio.create_task(manager.start(
        ephemeral_servers=supplement() if ephemeral else None))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert all(c.disconnected for c in created)
    assert not manager.clients
    assert not manager.get_all_capabilities()
