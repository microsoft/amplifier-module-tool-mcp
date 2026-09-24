from unittest.mock import AsyncMock, patch

import pytest

from amplifier_module_tool_mcp.config import MCPServerConfig
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
