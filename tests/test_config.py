"""Tests for MCP configuration loading."""

import json
import os

from amplifier_module_tool_mcp.config import MCPConfig


def test_load_from_inline():
    """Test loading configuration from inline config."""
    inline_config = {
        "servers": {
            "test-server": {"command": "npx", "args": ["-y", "test-server"]},
        }
    }

    config = MCPConfig(inline_config)
    servers = config.load_servers()

    assert "test-server" in servers
    assert servers["test-server"]["command"] == "npx"


def test_load_from_project_config(temp_config_dir, sample_mcp_config, monkeypatch):
    """Project-local stdio commands are not loaded."""
    # Change to temp directory
    monkeypatch.chdir(temp_config_dir.parent)

    # Write config file
    config_file = temp_config_dir / "mcp.json"
    with open(config_file, "w") as f:
        json.dump(sample_mcp_config, f)

    config = MCPConfig()
    servers = config.load_servers()

    assert "test-server" not in servers


def test_load_http_server_from_project_config(temp_config_dir, monkeypatch):
    """Project-local HTTP servers cannot initiate automatic connections."""
    monkeypatch.chdir(temp_config_dir.parent)
    project_config = {
        "mcpServers": {
            "test-server": {
                "type": "streamable-http",
                "url": "https://example.test/mcp",
            }
        }
    }
    with open(temp_config_dir / "mcp.json", "w") as f:
        json.dump(project_config, f)

    servers = MCPConfig().load_servers()

    assert "test-server" not in servers


def test_project_config_cannot_self_approve_stdio(temp_config_dir, monkeypatch):
    """Trust-like fields in project config cannot authorize execution."""
    monkeypatch.chdir(temp_config_dir.parent)
    project_config = {
        "mcpServers": {
            "test-server": {
                "command": "npx",
                "args": ["-y", "test-server"],
                "trusted": True,
                "approved": True,
            }
        }
    }
    with open(temp_config_dir / "mcp.json", "w") as f:
        json.dump(project_config, f)

    servers = MCPConfig().load_servers()

    assert "test-server" not in servers


def test_load_from_env(tmp_path, sample_mcp_config, monkeypatch):
    """Test loading configuration from environment variable."""
    # Create config file
    config_file = tmp_path / "mcp.json"
    with open(config_file, "w") as f:
        json.dump(sample_mcp_config, f)

    # Set environment variable
    monkeypatch.setenv("AMPLIFIER_MCP_CONFIG", str(config_file))

    config = MCPConfig()
    servers = config.load_servers()

    assert "test-server" in servers
    assert servers["test-server"].source == "environment"


def test_config_priority(temp_config_dir, monkeypatch):
    """Test that inline config overrides project config."""
    # Change to temp directory
    monkeypatch.chdir(temp_config_dir.parent)

    # Create project config
    project_config = {
        "mcpServers": {
            "test-server": {"command": "project-command", "args": []},
        }
    }
    with open(temp_config_dir / "mcp.json", "w") as f:
        json.dump(project_config, f)

    # Inline config (higher priority)
    inline_config = {
        "servers": {
            "test-server": {"command": "inline-command", "args": []},
        }
    }

    config = MCPConfig(inline_config)
    servers = config.load_servers()

    # Inline should override project
    assert servers["test-server"]["command"] == "inline-command"
    assert servers["test-server"].source == "inline"


def test_blocked_project_stdio_does_not_shadow_user_config(monkeypatch, tmp_path):
    """A blocked project entry cannot replace a trusted user server."""
    project = tmp_path / "project"
    project.mkdir()
    (project / ".amplifier").mkdir()
    monkeypatch.chdir(project)
    user_home = tmp_path / "home"
    user_config_dir = user_home / ".amplifier"
    user_config_dir.mkdir(parents=True)
    monkeypatch.setattr("pathlib.Path.home", lambda: user_home)

    with open(user_config_dir / "mcp.json", "w") as f:
        json.dump(
            {"mcpServers": {"test-server": {"command": "trusted-command", "args": []}}},
            f,
        )
    with open(project / ".amplifier" / "mcp.json", "w") as f:
        json.dump(
            {"mcpServers": {"test-server": {"command": "project-command", "args": []}}},
            f,
        )

    servers = MCPConfig().load_servers()

    assert servers["test-server"]["command"] == "trusted-command"
    assert servers["test-server"].source == "user"


def test_env_var_substitution():
    """Test environment variable substitution."""
    # Set environment variable
    os.environ["TEST_VAR"] = "test-value"

    # Test ${VAR} syntax
    result = MCPConfig.substitute_env_vars("Value is ${TEST_VAR}")
    assert result == "Value is test-value"

    # Test ${VAR:-default} syntax with existing var
    result = MCPConfig.substitute_env_vars("Value is ${TEST_VAR:-default}")
    assert result == "Value is test-value"

    # Test ${VAR:-default} syntax with missing var
    result = MCPConfig.substitute_env_vars("Value is ${MISSING_VAR:-default}")
    assert result == "Value is default"

    # Clean up
    del os.environ["TEST_VAR"]


def test_empty_config(monkeypatch, tmp_path):
    """Test handling of no configuration."""
    # Change to temp directory to avoid loading project config
    monkeypatch.chdir(tmp_path)

    # Clear environment variable if set
    monkeypatch.delenv("AMPLIFIER_MCP_CONFIG", raising=False)

    config = MCPConfig()
    servers = config.load_servers()

    # Should return dict (may be empty or may have user-level config)
    # The important thing is it doesn't crash with no config
    assert isinstance(servers, dict)


def test_load_config_with_utf8_bom(tmp_path, sample_mcp_config, monkeypatch):
    """A BOM-prefixed mcp.json still loads.

    Windows editors (Notepad's default "UTF-8" save, PowerShell redirection)
    prefix the file with a UTF-8 BOM. Read as plain utf-8, the leading U+FEFF
    makes json.load raise, and the broad except in _load_json_file swallows it
    and returns None -- silently dropping every configured server.
    """
    config_file = tmp_path / "mcp.json"
    # encoding="utf-8-sig" on write emits the BOM, reproducing what a Windows
    # editor produces.
    with open(config_file, "w", encoding="utf-8-sig") as f:
        json.dump(sample_mcp_config, f)

    # Confirm the fixture actually has a BOM -- otherwise this test proves nothing.
    assert config_file.read_bytes().startswith(b"\xef\xbb\xbf")

    monkeypatch.setenv("AMPLIFIER_MCP_CONFIG", str(config_file))

    config = MCPConfig()
    servers = config.load_servers()

    assert "test-server" in servers
    assert servers["test-server"]["command"] == "npx"


def test_load_config_without_bom_unaffected(tmp_path, sample_mcp_config, monkeypatch):
    """Reading as utf-8-sig is a no-op for BOM-less files."""
    config_file = tmp_path / "mcp.json"
    with open(config_file, "w", encoding="utf-8") as f:
        json.dump(sample_mcp_config, f)

    assert not config_file.read_bytes().startswith(b"\xef\xbb\xbf")

    monkeypatch.setenv("AMPLIFIER_MCP_CONFIG", str(config_file))

    config = MCPConfig()
    servers = config.load_servers()

    assert "test-server" in servers
    assert servers["test-server"]["command"] == "npx"
