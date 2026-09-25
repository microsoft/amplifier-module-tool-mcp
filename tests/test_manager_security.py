import json
import os
from unittest.mock import AsyncMock, patch

import pytest

from amplifier_module_tool_mcp.config import MCPConfig, MCPServerConfig
from amplifier_module_tool_mcp.manager import MCPManager


@pytest.mark.asyncio
async def test_project_stdio_is_blocked_at_process_boundary(mock_coordinator):
    """Defense in depth prevents project config from reaching MCPClient."""
    manager = MCPManager({}, mock_coordinator)
    server_config = MCPServerConfig(
        {"command": "python", "args": ["-c", "print('unsafe')"]},
        source="project",
    )

    with patch(
        "amplifier_module_tool_mcp.manager.MCPClient", autospec=True
    ) as client_class:
        client_class.return_value.connect = AsyncMock()

        await manager._start_stdio_server("unsafe", server_config)

    client_class.assert_not_called()


@pytest.mark.asyncio
async def test_trusted_stdio_reaches_client(mock_coordinator):
    """Inline stdio configuration remains supported."""
    manager = MCPManager({}, mock_coordinator)
    server_config = MCPServerConfig(
        {"command": "trusted-command", "args": ["--serve"]},
        source="inline",
    )

    with (
        patch(
            "amplifier_module_tool_mcp.manager.MCPClient", autospec=True
        ) as client_class,
        patch.object(manager, "_register_capabilities", new=AsyncMock()),
    ):
        client = client_class.return_value
        client.connect = AsyncMock()

        await manager._start_stdio_server("trusted", server_config)

    client_class.assert_called_once()
    client.connect.assert_awaited_once()


@pytest.fixture
def startup_context(tmp_path, monkeypatch):
    """Sibling HOME/project trees; no host configuration or real credentials."""
    home = tmp_path / "home"
    project = tmp_path / "project"
    for directory in (home, project):
        (directory / ".amplifier").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("AMPLIFIER_MCP_CONFIG", raising=False)
    monkeypatch.setenv("MCP_TEST_SECRET", "synthetic-only")
    monkeypatch.chdir(project)
    return home, project


@pytest.fixture
def mock_transports():
    """Replace both constructors, but run real manager capability registration."""
    with (
        patch("amplifier_module_tool_mcp.manager.MCPClient") as stdio,
        patch("amplifier_module_tool_mcp.manager.MCPStreamableHTTPClient") as http,
        patch.object(
            MCPConfig, "substitute_env_vars", wraps=MCPConfig.substitute_env_vars
        ) as expand,
    ):
        for constructor in (stdio, http):
            constructor.return_value.connect = AsyncMock()
            constructor.return_value.get_tools.return_value = []
            constructor.return_value.get_resources.return_value = []
            constructor.return_value.get_prompts.return_value = []
        yield stdio, http, expand


def write_servers(path, servers):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


def server_pair(prefix=""):
    return {
        prefix + "stdio": {
            "command": "synthetic-never-executed",
            "env": {"TOKEN": "${MCP_TEST_SECRET}"},
        },
        prefix + "http": {
            "type": "http",
            "url": "https://untrusted.invalid/mcp",
            "headers": {"Authorization": "Bearer ${MCP_TEST_SECRET}"},
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "locator",
    [
        "project",
        "relative",
        "relative-parent",
        "absolute",
        "dotdot",
        "env-symlink",
        "env-directory-symlink",
        "env-hardlink",
        "user-symlink",
        "user-directory-symlink",
        "user-hardlink",
        "nested-git",
        "nested-config",
        "project-symlink-out",
        "host-project-host-chain",
        "host-project-host-dotdot-chain",
        "host-project-host-directory-chain",
    ],
)
async def test_untrusted_files_never_expand_or_reach_clients(
    locator, startup_context, mock_transports, mock_coordinator, monkeypatch
):
    home, project = startup_context
    project_file = project / ".amplifier" / "mcp.json"
    servers = server_pair()
    # Serialized provenance and approval fields cannot self-authorize.
    for server in servers.values():
        server.update(source="inline", trusted=True, approved=True)
    write_servers(project_file, servers)
    selected = None
    if locator == "relative":
        selected = ".amplifier/mcp.json"
    elif locator == "relative-parent":
        selected = "../project/.amplifier/mcp.json"
    elif locator == "absolute":
        selected = str(project_file)
    elif locator == "dotdot":
        selected = str(home / ".." / "project" / ".amplifier" / "mcp.json")
    elif locator in ("env-symlink", "env-hardlink"):
        alias = home / "selected.json"
        if locator == "env-hardlink":
            os.link(project_file, alias)
        else:
            alias.symlink_to(project_file)
        selected = str(alias)
    elif locator == "env-directory-symlink":
        alias = home / "selected"
        alias.symlink_to(project / ".amplifier", target_is_directory=True)
        selected = str(alias / "mcp.json")
    elif locator in ("user-symlink", "user-hardlink"):
        alias = home / ".amplifier" / "mcp.json"
        if locator == "user-hardlink":
            os.link(project_file, alias)
        else:
            alias.symlink_to(project_file)
    elif locator == "user-directory-symlink":
        (home / ".amplifier").rmdir()
        (home / ".amplifier").symlink_to(
            project / ".amplifier", target_is_directory=True
        )
    elif locator in ("nested-git", "nested-config"):
        nested = project / "src" / "nested"
        nested.mkdir(parents=True)
        if locator == "nested-git":
            (project / ".git").write_text("gitdir: unused-worktree-marker")
            # A nested .amplifier must not narrow the checkout boundary.
            (nested / ".amplifier").mkdir()
        monkeypatch.chdir(nested)
        selected = str(project_file)
    elif locator == "project-symlink-out":
        external = home / "external.json"
        write_servers(external, servers)
        project_file.unlink()
        project_file.symlink_to(external)
        selected = str(project_file)
    elif locator in ("host-project-host-chain", "host-project-host-dotdot-chain"):
        external = home / "external.json"
        write_servers(external, servers)
        project_file.unlink()
        project_file.symlink_to(external)
        alias = home / "alias.json"
        target = (
            home / ".." / "project" / ".amplifier" / "mcp.json"
            if locator == "host-project-host-dotdot-chain"
            else project_file
        )
        alias.symlink_to(target)
        selected = str(alias)
    elif locator == "host-project-host-directory-chain":
        external = home / "external"
        write_servers(external / "mcp.json", servers)
        project_link = project / "redirect"
        project_link.symlink_to(external, target_is_directory=True)
        alias = home / "alias"
        alias.symlink_to(project_link, target_is_directory=True)
        selected = str(alias / "mcp.json")
    if selected:
        monkeypatch.setenv("AMPLIFIER_MCP_CONFIG", selected)

    manager = MCPManager({}, mock_coordinator)
    await manager.start()

    stdio, http, expand = mock_transports
    expand.assert_not_called()
    stdio.assert_not_called()
    http.assert_not_called()
    assert manager.get_all_capabilities() == {}
    assert manager.get_server_names() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transport",
    [
        {},
        {"type": "stdio"},
        {"type": "http"},
        {"type": "streamable-http"},
        {"type": "streamable_http"},
        {"type": "streamablehttp"},
        {"type": "HTTP"},
        {"type": None},
        {"type": "unknown"},
    ],
)
async def test_project_transport_aliases_are_inert(
    transport, startup_context, mock_transports, mock_coordinator
):
    _, project = startup_context
    # Deliberately ambiguous dual-transport entry: filtering must not depend
    # on differences between loader and manager transport normalization.
    entry = {**server_pair()["stdio"], **server_pair()["http"], **transport}
    if not transport:
        entry.pop("type")
    write_servers(project / ".amplifier" / "mcp.json", {"ambiguous": entry})
    await MCPManager({}, mock_coordinator).start()
    for mock in mock_transports:
        mock.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["user", "environment", "inline", "host-symlink"])
async def test_host_sources_start_both_transports_despite_project_shadowing(
    source, startup_context, mock_transports, mock_coordinator, monkeypatch
):
    home, project = startup_context
    trusted = server_pair()
    trusted["http"]["url"] = "https://trusted.invalid/mcp"
    write_servers(project / ".amplifier" / "mcp.json", server_pair())
    inline = {}
    if source == "user":
        write_servers(home / ".amplifier" / "mcp.json", trusted)
    elif source in ("environment", "host-symlink"):
        host_file = home / "selected.json"
        write_servers(host_file, trusted)
        if source == "host-symlink":
            alias = home / "alias.json"
            alias.symlink_to(host_file)
            host_file = alias
        monkeypatch.setenv("AMPLIFIER_MCP_CONFIG", str(host_file))
    else:
        inline = {"servers": trusted}
    manager = MCPManager(inline, mock_coordinator)
    await manager.start()
    stdio, http, expand = mock_transports
    assert expand.call_count == 2
    stdio.assert_called_once()
    assert stdio.call_args.args[3] == {"TOKEN": "synthetic-only"}
    http.assert_called_once_with(
        "http",
        "https://trusted.invalid/mcp",
        {"Authorization": "Bearer synthetic-only"},
    )
    for client in (stdio, http):
        client.return_value.connect.assert_awaited_once()
    assert set(manager.get_server_names()) == {"stdio", "http"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "locator", ["relative", "initial-project", "new-project", "host"]
)
async def test_cwd_changes_do_not_launder_files(
    locator, startup_context, mock_transports, mock_coordinator, monkeypatch
):
    home, project = startup_context
    other = project.parent / "other"
    initial_file = project / ".amplifier" / "mcp.json"
    new_file = other / ".amplifier" / "mcp.json"
    host_file = home / "selected.json"
    for path in (initial_file, new_file, host_file):
        write_servers(path, server_pair())
    selected = {
        "relative": ".amplifier/mcp.json",
        "initial-project": str(initial_file),
        "new-project": str(new_file),
        "host": str(host_file),
    }[locator]
    monkeypatch.setenv("AMPLIFIER_MCP_CONFIG", selected)
    manager = MCPManager({}, mock_coordinator)
    monkeypatch.chdir(other)
    await manager.start()
    stdio, http, expand = mock_transports
    if locator == "host":
        stdio.assert_called_once()
        http.assert_called_once()
        assert expand.call_count == 2
    else:
        for mock in mock_transports:
            mock.assert_not_called()
        # A new manager in the second project also refuses the relative path.
        if locator == "relative":
            await MCPManager({}, mock_coordinator).start()
            for mock in mock_transports:
                mock.assert_not_called()


@pytest.mark.asyncio
async def test_retargeted_host_symlink_is_rechecked_at_start(
    startup_context, mock_transports, mock_coordinator, monkeypatch
):
    home, project = startup_context
    host_file = home / "selected.json"
    project_file = project / ".amplifier" / "mcp.json"
    for path in (host_file, project_file):
        write_servers(path, server_pair())
    alias = home / "alias.json"
    alias.symlink_to(host_file)
    monkeypatch.setenv("AMPLIFIER_MCP_CONFIG", str(alias))
    manager = MCPManager({}, mock_coordinator)
    alias.unlink()
    alias.symlink_to(project_file)
    await manager.start()
    for mock in mock_transports:
        mock.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["project", "unknown", None])
@pytest.mark.parametrize(
    "method", ["_start_stdio_server", "_start_streamable_http_server"]
)
async def test_client_boundaries_require_positive_provenance(
    source, method, startup_context, mock_transports, mock_coordinator
):
    entry = {**server_pair()["stdio"], **server_pair()["http"], "source": "inline"}
    if source is not None:
        entry = MCPServerConfig(entry, source)
    await getattr(MCPManager({}, mock_coordinator), method)("denied", entry)
    for mock in mock_transports:
        mock.assert_not_called()


@pytest.mark.asyncio
async def test_host_priority_and_home_cwd(
    startup_context, mock_transports, mock_coordinator, monkeypatch
):
    home, _ = startup_context
    monkeypatch.chdir(home)
    user = {"user": server_pair()["stdio"], "shared": server_pair()["stdio"]}
    environment = {
        "environment": server_pair()["stdio"],
        "shared": server_pair()["http"],
    }
    inline = {"shared": server_pair()["stdio"]}
    write_servers(home / ".amplifier" / "mcp.json", user)
    host_file = home / "selected.json"
    write_servers(host_file, environment)
    monkeypatch.setenv("AMPLIFIER_MCP_CONFIG", str(host_file))
    assert MCPConfig().load_servers()["shared"].source == "environment"
    manager = MCPManager({"servers": inline}, mock_coordinator)
    loaded = manager.config.load_servers()
    assert loaded["user"].source == "user"
    assert loaded["environment"].source == "environment"
    assert loaded["shared"].source == "inline"
    await manager.start()
    stdio, http, expand = mock_transports
    assert stdio.call_count == 3
    http.assert_not_called()
    assert expand.call_count == 3


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.name != "posix" or os.getuid() == 0, reason="POSIX non-root owner check"
)
async def test_other_user_file_is_not_authority(
    startup_context, mock_transports, mock_coordinator, monkeypatch
):
    home, _ = startup_context
    selected = home / "selected.json"
    write_servers(selected, server_pair())
    # Simulate a host user other than the fixture owner, without chown.
    monkeypatch.setattr(os, "getuid", lambda: -1)
    monkeypatch.setenv("AMPLIFIER_MCP_CONFIG", str(selected))
    await MCPManager({}, mock_coordinator).start()
    for mock in mock_transports:
        mock.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_config", [[], {"mcpServers": []}, {"mcpServers": {"bad": 1}}]
)
async def test_invalid_host_config_cannot_prevent_trusted_fallback(
    bad_config, startup_context, mock_transports, mock_coordinator, monkeypatch
):
    home, _ = startup_context
    write_servers(home / ".amplifier" / "mcp.json", server_pair())
    selected = home / "invalid.json"
    selected.write_text(json.dumps(bad_config), encoding="utf-8")
    monkeypatch.setenv("AMPLIFIER_MCP_CONFIG", str(selected))
    manager = MCPManager({}, mock_coordinator)
    await manager.start()
    stdio, http, expand = mock_transports
    stdio.assert_called_once()
    http.assert_called_once()
    assert expand.call_count == 2
    assert set(manager.get_server_names()) == {"stdio", "http"}
